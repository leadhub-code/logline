'''
Behavioral tests that actually start the server as a subprocess and verify
that --reuse-port allows two instances to listen on the same address, while
without it the second instance fails to bind.
'''

from logging import getLogger
import socket
from subprocess import Popen, TimeoutExpired
import sys
from time import sleep
from time import monotonic as monotime

import pytest


logger = getLogger(__name__)

client_token_hash = 'dummyhash'

HAS_SO_REUSEPORT = hasattr(socket, 'SO_REUSEPORT')


def free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def server_command(port, dest, reuse_port):
    cmd = [
        sys.executable, '-m', 'logline_server',
        '--bind', f'127.0.0.1:{port}',
        '--dest', str(dest),
        '--client-token-hash', client_token_hash,
    ]
    if reuse_port:
        cmd.append('--reuse-port')
    return cmd


@pytest.fixture
def start_server(tmp_path):
    '''
    Factory fixture that starts server subprocesses and makes sure all of
    them are terminated when the test finishes.
    '''
    dest = tmp_path / 'dst'
    dest.mkdir()
    processes = []

    def _start(port, reuse_port):
        process = Popen(server_command(port, dest, reuse_port=reuse_port))
        processes.append(process)
        return process

    yield _start

    for process in processes:
        if process.poll() is None:
            logger.info('Terminating process %s args: %s', process.pid, ' '.join(process.args))
            process.terminate()
        try:
            process.wait(timeout=5)
        except TimeoutExpired:
            process.kill()
            process.wait()


def wait_until_listening(port, process, timeout=5):
    '''Return True once something is accepting connections on the port.'''
    t0 = monotime()
    while monotime() - t0 < timeout:
        if process.poll() is not None:
            return False  # the process died before it started listening
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(('127.0.0.1', port))
                return True
            except OSError:
                sleep(0.05)
    return False


def wait_for_exit(process, timeout=5):
    '''Return the exit code if the process exits within timeout, else None.'''
    t0 = monotime()
    while monotime() - t0 < timeout:
        rc = process.poll()
        if rc is not None:
            return rc
        sleep(0.05)
    return None


@pytest.mark.skipif(not HAS_SO_REUSEPORT, reason='SO_REUSEPORT not supported on this platform')
def test_two_instances_can_run_with_reuse_port(start_server):
    port = free_tcp_port()
    first = start_server(port, reuse_port=True)
    assert wait_until_listening(port, first), 'first instance failed to start listening'

    second = start_server(port, reuse_port=True)
    # With --reuse-port the second instance binds successfully and keeps
    # running; if it had failed to bind it would have exited immediately.
    assert wait_for_exit(second, timeout=2) is None, 'second instance exited unexpectedly with --reuse-port'
    assert first.poll() is None, 'first instance exited unexpectedly'


def test_second_instance_fails_without_reuse_port(start_server):
    port = free_tcp_port()
    first = start_server(port, reuse_port=False)
    assert wait_until_listening(port, first), 'first instance failed to start listening'

    second = start_server(port, reuse_port=False)
    # Without --reuse-port the second instance cannot bind to the already used
    # address and must exit with a non-zero status.
    rc = wait_for_exit(second, timeout=5)
    assert rc is not None, 'second instance kept running but should have failed to bind'
    assert rc != 0, f'second instance exited with {rc}, expected a non-zero status'
    assert first.poll() is None, 'first instance exited unexpectedly'
