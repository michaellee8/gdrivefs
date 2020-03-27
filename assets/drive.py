# Copyright 2018 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Colab-specific Google Drive integration."""

from __future__ import absolute_import as _
from __future__ import division as _
from __future__ import print_function as _

import collections as _collections
import getpass as _getpass
import os as _os
import signal as _signal
import socket as _socket
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import uuid as _uuid

import pexpect.popen_spawn as _popen_spawn

__all__ = ['flush_and_unmount', 'mount']


_Environment = _collections.namedtuple(
    '_Environment',
    ('home', 'root_dir', 'inet_family', 'dev', 'path', 'config_dir'))


def _env():
  """Create and return an _Environment to use."""
  home = _os.environ['HOME']
  root_dir = _os.path.realpath(
      _os.path.join(_os.environ['CLOUDSDK_CONFIG'], '../..'))
  inet_family = 'IPV4_ONLY'
  dev = '/dev/fuse'
  path = '/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:.'
  if len(root_dir) > 1 and not root_dir.startswith('/usr/local/google/'):
    home = _os.path.join(root_dir, home)
    inet_family = 'IPV6_ONLY'
    fum = _os.environ['HOME'].split('mount')[0] + '/mount/alloc/fusermount'
    dev = fum + '/dev/fuse'
    path = path + ':' + fum + '/bin'
  config_dir = _os.path.join(home, '.config', 'Google')
  return _Environment(
      home=home,
      root_dir=root_dir,
      inet_family=inet_family,
      dev=dev,
      path=path,
      config_dir=config_dir)


def _logs_dir():
  return _os.path.join(_env().config_dir, 'DriveFS/Logs/')


def _timeouts_path():
  return _os.path.join(_logs_dir(), 'timeouts.txt')


def flush_and_unmount(timeout_ms=24 * 60 * 60 * 1000):
  """Unmount Google Drive and flush any outstanding writes to it."""
  env = _env()
  if b'type fuse.drive' not in _subprocess.check_output(['/bin/mount']):
    print('Drive not mounted, so nothing to flush and unmount.')
    return
  drive_bin = _os.path.join(env.root_dir, 'opt/google/drive/drive')
  p = _subprocess.Popen([
      drive_bin, '--push_changes_and_quit', '--single_process',
      '--timeout_sec={}'.format(int(timeout_ms / 1000))
  ],
                        stdout=_subprocess.PIPE,
                        stderr=_subprocess.PIPE)
  out, err = p.communicate()
  if mount._DEBUG:  # pylint:disable=protected-access
    print('flush_and_unmount: out: {}\nerr: {}'.format(out, err))
  if p.returncode:
    raise ValueError('flush_and_unmount failed')


def mount(mountpoint,
          force_remount=False,
          timeout_ms=60000,
          use_metadata_server=False):
  """Mount your Google Drive at the specified mountpoint path."""

  if ' ' in mountpoint:
    raise ValueError('Mountpoint must not contain a space.')

  mountpoint = _os.path.expanduser(mountpoint)
  # If we've already mounted drive at the specified mountpoint, exit now.
  already_mounted = _os.path.isdir(_os.path.join(mountpoint, 'My Drive'))
  if not force_remount and already_mounted:
    print('Drive already mounted at {}; to attempt to forcibly remount, '
          'call drive.mount("{}", force_remount=True).'.format(
              mountpoint, mountpoint))
    return

  env = _env()
  home = env.home
  root_dir = env.root_dir
  inet_family = env.inet_family
  dev = env.dev
  path = env.path
  config_dir = env.config_dir

  try:
    _os.makedirs(config_dir)
  except OSError:
    if not _os.path.isdir(config_dir):
      raise ValueError('{} must be a directory if present'.format(config_dir))

  # Launch an intermediate bash to manage DriveFS' I/O (b/141747058#comment6).
  prompt = u'root@{}-{}: '.format(_socket.gethostname(), _uuid.uuid4().hex)
  logfile = None
  if mount._DEBUG:  # pylint:disable=protected-access
    logfile = _sys.stdout
  d = _popen_spawn.PopenSpawn(
      '/usr/bin/setsid /bin/bash --noediting -i',  # Need -i to get prompt echo.
      timeout=120,
      maxread=int(1e6),
      encoding='utf-8',
      logfile=logfile,
      env={
          'HOME': home,
          'FUSE_DEV_NAME': dev,
          'PATH': path
      })
  d.sendline('export PS1="{}"'.format(prompt))
  d.expect(prompt)  # The new prompt.
  # Robustify to previously-running copies of drive. Don't only [pkill -9]
  # because that leaves enough cruft behind in the mount table that future
  # operations fail with "Transport endpoint is not connected".
  d.sendline('umount -f {mnt} || umount {mnt}; pkill -9 -x drive'.format(
      mnt=mountpoint))
  # Wait for above to be received, using the next prompt.
  d.expect(prompt)
  # Only check the mountpoint after potentially unmounting/pkill'ing above.
  try:
    if _os.path.islink(mountpoint):
      raise ValueError('Mountpoint must not be a symlink')
    if _os.path.isdir(mountpoint) and _os.listdir(mountpoint):
      raise ValueError('Mountpoint must not already contain files')
    if not _os.path.isdir(mountpoint) and _os.path.exists(mountpoint):
      raise ValueError('Mountpoint must either be a directory or not exist')
    normed = _os.path.normpath(mountpoint)
    if '/' in normed and not _os.path.exists(_os.path.dirname(normed)):
      raise ValueError('Mountpoint must be in a directory that exists')
  except:
    d.kill(_signal.SIGKILL)
    raise

  # Watch for success.
  success = u'google.colab.drive MOUNTED'
  success_watcher = (
      '( while `sleep 0.5`; do if [[ -d "{m}" && "$(ls -A {m})" != "" ]]; '
      'then echo "{s}"; break; fi; done ) &').format(
          m=mountpoint, s=success)
  d.sendline(success_watcher)
  d.expect(prompt)
  drive_dir = _os.path.join(root_dir, 'opt/google/drive')

  oauth_prompt = u'(Go to this URL in a browser: https://.*)$'
  problem_and_stopped = (
      u'Drive File Stream encountered a problem and has stopped')
  drive_exited = u'drive EXITED'
  metadata_auth_arg = (
      '--metadata_server_auth_uri={metadata_server}/computeMetadata/v1 '.format(
          metadata_server=_os.environ['TBE_CREDS_ADDR'])
      if use_metadata_server else '')

  # Create a pipe for sending the oauth code to a backgrounded drive binary.
  # (popen -> no pty -> no bash job control -> can't background post-launch).
  fifo_dir = _tempfile.mkdtemp()
  fifo = _os.path.join(fifo_dir, 'drive.fifo')
  _os.mkfifo(fifo)
  # cat is needed below since the FIFO isn't opened for writing yet.
  d.sendline(
      ('cat {fifo} | head -1 | ( {d}/drive '
       '--features=max_parallel_push_task_instances:10,'
       'max_operation_batch_size:15,opendir_timeout_ms:{timeout_ms},'
       'virtual_folders:true '
       '--inet_family=' + inet_family + ' ' + metadata_auth_arg +
       '--preferences=trusted_root_certs_file_path:'
       '{d}/roots.pem,mount_point_path:{mnt} --console_auth 2>&1 '
       '| grep --line-buffered -E "{oauth_prompt}|{problem_and_stopped}"; '
       'echo "{drive_exited}"; ) &').format(
           d=drive_dir,
           timeout_ms=timeout_ms,
           mnt=mountpoint,
           fifo=fifo,
           oauth_prompt=oauth_prompt,
           problem_and_stopped=problem_and_stopped,
           drive_exited=drive_exited))
  d.expect(prompt)

  # LINT.IfChange(drivetimedout)
  timeout_pattern = 'QueryManager timed out'
  # LINT.ThenChange()
  dfs_log = _os.path.join(config_dir, 'DriveFS/Logs/drive_fs.txt')

  wrote_to_fifo = False
  while True:
    case = d.expect([
        success,
        prompt,
        oauth_prompt,
        problem_and_stopped,
        drive_exited,
    ])
    if case == 0:
      break
    elif (case == 1 or case == 3 or case == 4):
      # Prompt appearing here means something went wrong with the drive binary.
      d.kill(_signal.SIGKILL)
      extra_reason = ''
      if 0 == _subprocess.call(
          'grep -q "{}" "{}"'.format(timeout_pattern, dfs_log), shell=True):
        extra_reason = (
            ': timeout during initial read of root folder; for more info: '
            'https://research.google.com/colaboratory/faq.html#drive-timeout')
      raise ValueError('mount failed' + extra_reason)
    elif case == 2:
      # Not already authorized, so do the authorization dance.
      auth_prompt = d.match.group(1) + '\nEnter your authorization code:\n'
      with open(fifo, 'w') as fifo_file:
        fifo_file.write(_getpass.getpass(auth_prompt) + '\n')
      wrote_to_fifo = True
  if not wrote_to_fifo:
    with open(fifo, 'w') as fifo_file:
      fifo_file.write('ignored\n')
  filtered_logfile = _timeouts_path()
  d.sendline('rm -rf "{}"'.format(filtered_logfile))
  d.expect(prompt)
  d.sendline(
      ("""nohup bash -c 'tail -n +0 -F "{}" | """
       """grep --line-buffered "{}" > "{}" ' < /dev/null > /dev/null 2>&1 &"""
      ).format(dfs_log, timeout_pattern, filtered_logfile))
  d.expect(prompt)
  d.sendline('disown -a')
  d.expect(prompt)
  d.sendline('exit')
  assert d.wait() == 0
  print('Mounted at {}'.format(mountpoint))


mount._DEBUG = False  # pylint:disable=protected-access
