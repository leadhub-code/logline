from os import chdir
from pathlib import Path

from conftest import (
    CLIENT_TOKEN,
    dst_path,
    free_port,
    run_server,
    running_process,
    wait_for_bytes,
)


def write_agent_conf(conf_path, src, port):
    conf_path.write_text(
        f'server: 127.0.0.1:{port}\n'
        f'client_token: {CLIENT_TOKEN}\n'
        f'scan:\n'
        f'  - {src}/*.log\n'
        f'tuning:\n'
        f'  reconnect_interval: 0.5\n'
        f'  scan_interval: 0.3\n')


def test_resume_after_server_restart(tmp_path):
    chdir(tmp_path)
    src = Path('src')
    src.mkdir()
    dst = Path('dst')
    dst.mkdir()
    log = src / 'a.log'
    first = b'first batch line\n' * 8
    second = b'second batch line\n' * 8
    log.write_bytes(first)
    port = free_port()
    conf = tmp_path / 'agent.yaml'
    write_agent_conf(conf, src, port)
    target = dst_path(dst, src, 'a.log')

    with running_process(['logline-agent', '--conf', str(conf), '-v']):
        with run_server(dst, port):
            wait_for_bytes(target, first, timeout=10)
        # The server is down now; the agent keeps running and retrying.
        with log.open('ab') as f:
            f.write(second)
        # Bring the server back on the same port. The agent reconnects, re-OPENs
        # the stream, resumes from the acked offset and ships the appended bytes,
        # with no gaps and no duplicates.
        with run_server(dst, port):
            wait_for_bytes(target, first + second, timeout=15)


def test_resume_from_nonzero_offset(tmp_path):
    chdir(tmp_path)
    src = Path('src')
    src.mkdir()
    dst = Path('dst')
    dst.mkdir()
    log = src / 'big.log'
    # Larger than the 256 B prefix, so on reconnect the prefix still matches and
    # the server resumes at a NON-ZERO offset without rotating. This is the case
    # that requires the agent to seed sent/acked offsets to the resume point;
    # otherwise the appended bytes would be sent with offset 0 and dropped as
    # duplicates by the server, and never persisted.
    first = b'A' * 600 + b'\n'
    second = b'B' * 600 + b'\n'
    log.write_bytes(first)
    port = free_port()
    conf = tmp_path / 'agent.yaml'
    write_agent_conf(conf, src, port)
    target = dst_path(dst, src, 'big.log')

    with running_process(['logline-agent', '--conf', str(conf), '-v']):
        with run_server(dst, port):
            wait_for_bytes(target, first, timeout=10)
        with log.open('ab') as f:
            f.write(second)
        with run_server(dst, port):
            wait_for_bytes(target, first + second, timeout=15)
