from contextlib import contextmanager
from hashlib import sha256
from logging import DEBUG, basicConfig, getLogger
import os
from pathlib import Path
import socket
from socket import getfqdn
from subprocess import Popen
import sys
from time import monotonic as monotime
from time import sleep


logger = getLogger(__name__)

CLIENT_TOKEN = 'topsecret'
CLIENT_TOKEN_HASH = sha256(CLIENT_TOKEN.encode()).hexdigest()


def pytest_configure(config):
    # Make sure the venv/bin directory is in PATH - in case this is running
    # from venv/bin/pytest without activating the venv first.
    bin_path = str(Path(sys.executable).parent)
    if bin_path not in os.environ['PATH'].split(':'):
        os.environ['PATH'] = f"{bin_path}:{os.environ['PATH']}"
        print('PATH modified to:', os.environ['PATH'])

    basicConfig(
        format='%(asctime)s [pytest %(process)d] %(name)s %(levelname)5s: %(message)s',
        level=DEBUG)


def free_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


@contextmanager
def running_process(cmd, env=None):
    full_env = {**os.environ, **(env or {})}
    logger.info('Starting: %s', ' '.join(cmd))
    p = Popen(cmd, env=full_env)
    try:
        yield p
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def run_server(dest, port, extra=()):
    cmd = [
        'logline-server',
        '--bind', f'127.0.0.1:{port}',
        '--dest', str(dest),
        '--client-token-hash', CLIENT_TOKEN_HASH,
        *extra,
    ]
    return running_process(cmd)


def run_agent(scan, port, extra=()):
    cmd = [
        'logline-agent',
        '--scan', scan,
        '--server', f'127.0.0.1:{port}',
        '-v',
        *extra,
    ]
    return running_process(cmd, env={'CLIENT_TOKEN': CLIENT_TOKEN})


def wait_until(predicate, timeout=5, interval=0.05, what='condition'):
    deadline = monotime() + timeout
    while monotime() < deadline:
        result = predicate()
        if result:
            return result
        sleep(interval)
    raise AssertionError(f'Timed out after {timeout}s waiting for {what}')


def wait_for_bytes(path, expected, timeout=5):
    def matches():
        return path.exists() and path.read_bytes() == expected
    wait_until(matches, timeout=timeout, what=f'{path} to contain {len(expected)} bytes')


def dst_path(dst_root, src_dir, name):
    '''Where the server stores the log for a given source dir and filename.'''
    mangled = str(Path(src_dir).resolve()).strip('/').replace('/', '~')
    return Path(dst_root) / getfqdn() / mangled / name
