# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module used by CI tools in order to interact with fuzzers.
This module helps CI tools do the following:
  1. Build fuzzers.
  2. Run fuzzers.
Eventually it will be used to help CI tools determine which fuzzers to run.
"""

import datetime
import io
import logging
import os
import shutil
import sys
import urllib.request
import zipfile

import fuzz_target

# pylint: disable=wrong-import-position
# pylint: disable=import-error
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_specified_commit
import helper
import repo_manager
import utils

# From clusterfuzz: src/python/crash_analysis/crash_analyzer.py
# Used to get the beginning of the stack trace.
STACKTRACE_TOOL_MARKERS = [
    'AddressSanitizer',
    'ASAN:',
    'CFI: Most likely a control flow integrity violation;',
    'ERROR: libFuzzer',
    'KASAN:',
    'LeakSanitizer',
    'MemorySanitizer',
    'ThreadSanitizer',
    'UndefinedBehaviorSanitizer',
    'UndefinedSanitizer',
]

# From clusterfuzz: src/python/crash_analysis/crash_analyzer.py
# Used to get the end of the stack trace.
STACKTRACE_END_MARKERS = [
    'ABORTING',
    'END MEMORY TOOL REPORT',
    'End of process memory map.',
    'END_KASAN_OUTPUT',
    'SUMMARY:',
    'Shadow byte and word',
    '[end of stack trace]',
    '\nExiting',
    'minidump has been written',
]

# TODO: Turn default logging to WARNING when CIFuzz is stable
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG)


def build_fuzzers(project_name,
                  project_repo_name,
                  workspace,
                  pr_ref=None,
                  commit_sha=None):
  """Builds all of the fuzzers for a specific OSS-Fuzz project.

  Args:
    project_name: The name of the OSS-Fuzz project being built.
    project_repo_name: The name of the projects repo.
    workspace: The location in a shared volume to store a git repo and build
      artifacts.
    pr_ref: The pull request reference to be built.
    commit_sha: The commit sha for the project to be built at.

  Returns:
    True if build succeeded or False on failure.
  """
  # Validate inputs.
  assert pr_ref or commit_sha
  if not os.path.exists(workspace):
    logging.error('Invalid workspace: %s.', workspace)
    return False

  git_workspace = os.path.join(workspace, 'storage')
  os.makedirs(git_workspace, exist_ok=True)
  out_dir = os.path.join(workspace, 'out')
  os.makedirs(out_dir, exist_ok=True)

  # Detect repo information.
  inferred_url, oss_fuzz_repo_path = build_specified_commit.detect_main_repo(
      project_name, repo_name=project_repo_name)
  if not inferred_url or not oss_fuzz_repo_path:
    logging.error('Could not detect repo from project %s.', project_name)
    return False
  src_in_docker = os.path.dirname(oss_fuzz_repo_path)
  oss_fuzz_repo_name = os.path.basename(oss_fuzz_repo_path)

  # Checkout projects repo in the shared volume.
  build_repo_manager = repo_manager.RepoManager(inferred_url,
                                                git_workspace,
                                                repo_name=oss_fuzz_repo_name)
  try:
    if pr_ref:
      build_repo_manager.checkout_pr(pr_ref)
    else:
      build_repo_manager.checkout_commit(commit_sha)
  except RuntimeError:
    logging.error('Can not check out requested state.')
    return False
  except ValueError:
    logging.error('Invalid commit SHA requested %s.', commit_sha)
    return False

  # Build Fuzzers using docker run.
  command = [
      '--cap-add', 'SYS_PTRACE', '-e', 'FUZZING_ENGINE=libfuzzer', '-e',
      'SANITIZER=address', '-e', 'ARCHITECTURE=x86_64'
  ]
  container = utils.get_container_name()
  if container:
    command += ['-e', 'OUT=' + out_dir, '--volumes-from', container]
    bash_command = 'rm -rf {0} && cp -r {1} {2} && compile'.format(
        os.path.join(src_in_docker, oss_fuzz_repo_name, '*'),
        os.path.join(git_workspace, oss_fuzz_repo_name), src_in_docker)
  else:
    command += [
        '-e', 'OUT=' + '/out', '-v',
        '%s:%s' % (os.path.join(git_workspace, oss_fuzz_repo_name),
                   os.path.join(src_in_docker, oss_fuzz_repo_name)), '-v',
        '%s:%s' % (out_dir, '/out')
    ]
    bash_command = 'compile'

  command.extend([
      'gcr.io/oss-fuzz/' + project_name,
      '/bin/bash',
      '-c',
  ])
  command.append(bash_command)
  if helper.docker_run(command):
    logging.error('Building fuzzers failed.')
    return False
  return True


def run_fuzzers(fuzz_seconds, workspace, project_name=None):
  """Runs all fuzzers for a specific OSS-Fuzz project.

  Args:
    fuzz_seconds: The total time allotted for fuzzing.
    workspace: The location in a shared volume to store a git repo and build
      artifacts.
    project_name: The name of the OSS-Fuzz project the run relates to.

  Returns:
    (True if run was successful, True if bug was found).
  """
  # Validate inputs.
  if not os.path.exists(workspace):
    logging.error('Invalid workspace: %s.', workspace)
    return False, False
  out_dir = os.path.join(workspace, 'out')
  artifacts_dir = os.path.join(out_dir, 'artifacts')
  os.makedirs(artifacts_dir, exist_ok=True)
  if not fuzz_seconds or fuzz_seconds < 1:
    logging.error('Fuzz_seconds argument must be greater than 1, but was: %s.',
                  format(fuzz_seconds))
    return False, False

  # Get fuzzer information.
  fuzzer_paths = utils.get_fuzz_targets(out_dir)
  if not fuzzer_paths:
    logging.error('No fuzzers were found in out directory: %s.',
                  format(out_dir))
    return False, False
  fuzz_seconds_per_target = fuzz_seconds // len(fuzzer_paths)

  # Run fuzzers for alotted time.
  for fuzzer_path in fuzzer_paths:

    # OSS-Fuzz specific project setup.
    if project_name:
      corpus_dir = download_latest_corpus(project_name, out_dir,
                                          os.path.basename(fuzzer_path))
      if not corpus_dir:
        logging.warning('The backup corpus is not being used for fuzzing.')
      else:
        logging.info('Using corpus found at %s.', corpus_dir)
      target = fuzz_target.FuzzTarget(fuzzer_path,
                                      fuzz_seconds_per_target,
                                      out_dir,
                                      corpus_dir=corpus_dir)
    else:
      target = fuzz_target.FuzzTarget(fuzzer_path, fuzz_seconds_per_target,
                                      out_dir)
    test_case, stack_trace = target.fuzz()
    if not test_case or not stack_trace:
      logging.info('Fuzzer %s, finished running.', target.target_name)
    else:
      logging.info('Fuzzer %s, detected error: %s.', target.target_name,
                   stack_trace)
      shutil.move(test_case, os.path.join(artifacts_dir, 'test_case'))
      parse_fuzzer_output(stack_trace, artifacts_dir)
      return True, True
  return True, False


def download_latest_corpus(project_name, out_dir, target):
  """Downloads the newest OSS-Fuzz backup corpus from google cloud.

  Args:
    project_name: The name of the projects backup to download.
    out_dir: The location to place the download.
    fuzz_target: The fuzz_target's corpus to be downloaded.

  Returns:
    The local path to to corpus or None if download failed.
  """
  if not helper.check_project_exists(project_name):
    logging.error('Project %s is not a valid OSS-Fuzz project.', project_name)
    return None
  if not os.path.exists(out_dir):
    logging.error('Out directory %s does not exist.', out_dir)
    return None
  corpus_dir = os.path.join(out_dir, 'corpus', target)
  os.makedirs(corpus_dir, exist_ok=True)

  http_link = 'https://storage.googleapis.com/{0}-backup.clusterfuzz-external.' \
    'appspot.com/corpus/libFuzzer/{0}_{1}/{2}.zip'
  current_date = datetime.datetime.now()
  for day_diff in range(90, 100):
    date_to_check = current_date - datetime.timedelta(days=day_diff)
    date_str = date_to_check.strftime('%Y-%m-%d')
    corpus_link = http_link.format(project_name, target, date_str)
    logging.info("Trying corpus: %s", corpus_link)
    try:
      response = urllib.request.urlopen(corpus_link)
      with zipfile.ZipFile(io.BytesIO(response.read())) as zf:
          zf.extractall(corpus_dir)
    except urllib.error.HTTPError:
      logging.error('Unable to download corpus from: %s', corpus_link)
      return None
    logging.info('Downloading corpus from date %s.', date_str)
    return corpus_dir
  return None


def parse_fuzzer_output(fuzzer_output, out_dir):
  """Parses the fuzzer output from a fuzz target binary.

  Args:
    fuzzer_output: A fuzz target binary output string to be parsed.
    out_dir: The location to store the parsed output files.
  """
  # Get index of key file points.
  for marker in STACKTRACE_TOOL_MARKERS:
    marker_index = fuzzer_output.find(marker)
    if marker_index:
      begin_summary = marker_index
      break

  end_summary = -1
  for marker in STACKTRACE_END_MARKERS:
    marker_index = fuzzer_output.find(marker)
    if marker_index:
      end_summary = marker_index + len(marker)
      break

  if begin_summary is None or end_summary is None:
    return

  summary_str = fuzzer_output[begin_summary:end_summary]
  if not summary_str:
    return

  # Write sections of fuzzer output to specific files.
  summary_file_path = os.path.join(out_dir, 'bug_summary.txt')
  with open(summary_file_path, 'a') as summary_handle:
    summary_handle.write(summary_str)
