'''
Behavioral tests that actually start the server as a subprocess and verify
that --reuse-port allows two instances to listen on the same address, while
without it the second instance fails to bind.

Readiness is detected from the server's "Listening on ..." log line (emitted
right after the socket is bound), so the tests react to events instead of
sleeping for a fixed amount of time.
'''

from logging import getLogger
from queue import Queue, Empty
import socket
from subprocess import Popen, PIPE, TimeoutExpired
import sys
import threading
from time import monotonic as monotime

import pytest


logger = getLogger(__name__)

client_token_hash = 'dummyhash'

HAS_SO_REUSEPORT = hasattr(socket, 'SO_REUSEPORT')

# Substring of the log line the server emits once it has bound its socket.
LISTENING_LOG_MARKER = 'Listening on'


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


class ServerProcess:
    '''
    Wraps a server subprocess and continuously drains its stderr in a
    background thread, so that readiness can be detected from the log output
    without ever blocking the server on a full pipe buffer.
    '''

    def __init__(self, popen):
        self.popen = popen
        self._lines = Queue()
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()

    def _read_stderr(self):
        for line in self.popen.stderr:
            self._lines.put(line)
        self._lines.put(None)  # sentinel: stderr closed (process is exiting)

    def wait_until_listening(self, timeout=5):
        '''Return True once the server logs that it is listening.'''
        deadline = monotime() + timeout
        while True:
            remaining = deadline - monotime()
            if remaining <= 0:
                return False
            try:
                line = self._lines.get(timeout=remaining)
            except Empty:
                return False
            if line is None:  # process exited before it started listening
                return False
            if LISTENING_LOG_MARKER in line:
                return True

    def wait_for_exit(self, timeout=5):
        '''Return the exit code if the process exits within timeout, else None.'''
        try:
            return self.popen.wait(timeout=timeout)
        except TimeoutExpired:
            return None

    def poll(self):
        return self.popen.poll()

    def close(self):
        if self.popen.poll() is None:
            logger.info('Terminating server pid %s', self.popen.pid)
            self.popen.terminate()
        try:
            self.popen.wait(timeout=5)
        except TimeoutExpired:
            self.popen.kill()
            self.popen.wait()


@pytest.fixture
def start_server(tmp_path):
    '''
    Factory fixture that starts server subprocesses and makes sure all of
    them are terminated when the test finishes.
    '''
    dest = tmp_path / 'dst'
    dest.mkdir()
    servers = []

    def _start(port, reuse_port):
        popen = Popen(server_command(port, dest, reuse_port=reuse_port), stderr=PIPE, text=True)
        server = ServerProcess(popen)
        servers.append(server)
        return server

    yield _start

    for server in servers:
        server.close()


@pytest.mark.skipif(not HAS_SO_REUSEPORT, reason='SO_REUSEPORT not supported on this platform')
def test_two_instances_can_run_with_reuse_port(start_server):
    port = free_tcp_port()
    first = start_server(port, reuse_port=True)
    assert first.wait_until_listening(), 'first instance failed to bind'

    # With --reuse-port the second instance binds its own socket on the same
    # address and reports that it is listening too.
    second = start_server(port, reuse_port=True)
    assert second.wait_until_listening(), 'second instance failed to bind with --reuse-port'
    assert first.poll() is None, 'first instance exited unexpectedly'


def test_second_instance_fails_without_reuse_port(start_server):
    port = free_tcp_port()
    first = start_server(port, reuse_port=False)
    assert first.wait_until_listening(), 'first instance failed to bind'

    # Without --reuse-port the second instance cannot bind to the already used
    # address: it never reports listening and exits with a non-zero status.
    second = start_server(port, reuse_port=False)
    assert not second.wait_until_listening(timeout=2), 'second instance should not have started listening'
    rc = second.wait_for_exit(timeout=5)
    assert rc is not None, 'second instance kept running but should have failed to bind'
    assert rc != 0, f'second instance exited with {rc}, expected a non-zero status'
    assert first.poll() is None, 'first instance exited unexpectedly'
