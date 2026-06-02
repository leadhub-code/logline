from contextlib import ExitStack
from hashlib import sha1
from logging import getLogger
import os
from os import chdir
from pathlib import Path
from socket import getfqdn
from subprocess import Popen, check_call
from time import monotonic as monotime
from time import sleep


logger = getLogger(__name__)


client_token = 'topsecret'
client_token_hash = sha1(client_token.encode()).hexdigest()


def test_run_agent_help():
    check_call(['logline-agent', '--help'])


def test_run_server_help():
    check_call(['logline-server', '--help'])


def test_existing_log_file_gets_copied(tmp_path):
    chdir(tmp_path)
    Path('agent-src').mkdir()
    Path('server-dst').mkdir()
    Path('agent-src/sample.log').write_text('2021-02-22 Hello world!\n')
    mangled_src_path = str(Path('agent-src').resolve()).strip('/').replace('/', '~')
    expected_dst_file = Path('server-dst') / getfqdn() / mangled_src_path / 'sample.log'
    port = 9999
    with ExitStack() as stack:
        agent_cmd = [
            'logline-agent',
            '--scan', 'agent-src/*.log',
            '--server', f'127.0.0.1:{port}',
        ]
        server_cmd = [
            'logline-server',
            '--bind', f'127.0.0.1:{port}',
            '--dest', 'server-dst',
            '--client-token-hash', client_token_hash,
        ]
        server_process = stack.enter_context(Popen(server_cmd))
        stack.callback(terminate_process, server_process)
        sleep(.1)
        agent_process = stack.enter_context(Popen(agent_cmd, env={**os.environ, 'CLIENT_TOKEN': client_token}))
        stack.callback(terminate_process, agent_process)
        t0 = monotime()
        sleep(.1)
        while True:
            logger.debug('Checking after %.2f s...', monotime() - t0)
            assert agent_process.poll() is None
            assert server_process.poll() is None
            check_call(['find', str(tmp_path)], stdout=2)
            if not expected_dst_file.exists():
                logger.debug('Still no file in %s', expected_dst_file)
            else:
                sleep(.1)
                # ^^^ sometimes the file exists, but is still empty, so sleep a little more
                assert expected_dst_file.read_text() == '2021-02-22 Hello world!\n'
                logger.debug('Destination file created! %s', expected_dst_file)
                break
            if monotime() - t0 > 2:
                raise Exception('Deadline exceeded')
            sleep(.2)


def test_log_file_update_gets_copied(tmp_path):
    chdir(tmp_path)
    Path('agent-src').mkdir()
    Path('server-dst').mkdir()
    Path('agent-src/sample.log').write_text('2021-02-22 Hello world!\n')
    mangled_src_path = str(Path('agent-src').resolve()).strip('/').replace('/', '~')
    expected_dst_file = Path('server-dst') / getfqdn() / mangled_src_path / 'sample.log'
    port = 9999
    with ExitStack() as stack:
        agent_cmd = [
            'logline-agent',
            '--scan', 'agent-src/*.log',
            '--server', f'127.0.0.1:{port}',
        ]
        server_cmd = [
            'logline-server',
            '--bind', f'127.0.0.1:{port}',
            '--dest', 'server-dst',
            '--client-token-hash', client_token_hash,
        ]
        server_process = stack.enter_context(Popen(server_cmd))
        stack.callback(terminate_process, server_process)
        sleep(.1)
        agent_process = stack.enter_context(Popen(agent_cmd, env={**os.environ, 'CLIENT_TOKEN': client_token}))
        stack.callback(terminate_process, agent_process)
        t0 = monotime()
        sleep(.1)
        while True:
            logger.debug('Checking after %.2f s...', monotime() - t0)
            assert agent_process.poll() is None
            assert server_process.poll() is None
            check_call(['find', str(tmp_path)], stdout=2)
            if not expected_dst_file.exists():
                logger.debug('Still no file in %s', expected_dst_file)
            else:
                sleep(.1)
                # ^^^ sometimes the file exists, but is still empty, so sleep a little more
                assert expected_dst_file.read_text() == '2021-02-22 Hello world!\n'
                logger.debug('Destination file created! %s', expected_dst_file)
                break
            if monotime() - t0 > 2:
                raise Exception('Deadline exceeded')
            sleep(.1)
        with Path('agent-src/sample.log').open(mode='a') as f:
            f.write('Second line\n')
        logger.info('File agent-src/sample.log was appended')
        t0 = monotime()
        sleep(.1)
        while True:
            logger.debug('Checking after %.2f s...', monotime() - t0)
            assert agent_process.poll() is None
            assert server_process.poll() is None
            #check_call(['find', str(tmp_path)], stdout=2)
            assert expected_dst_file.exists()
            logger.debug('File %s contains: %r', expected_dst_file, expected_dst_file.read_text())
            if expected_dst_file.read_text() == '2021-02-22 Hello world!\nSecond line\n':
                logger.debug('Destination file updated! %s', expected_dst_file)
                break
            elif expected_dst_file.read_text() == '2021-02-22 Hello world!\n':
                logger.debug('Destination file not updated yet: %s', expected_dst_file)
            else:
                raise Exception(f"Unknown dst file {expected_dst_file} content: {expected_dst_file.read_text()!r}")
            if monotime() - t0 > 2:
                raise Exception('Deadline exceeded')
            sleep(.1)


def test_new_log_file_gets_copied(tmp_path):
    chdir(tmp_path)
    Path('agent-src').mkdir()
    Path('server-dst').mkdir()
    mangled_src_path = str(Path('agent-src').resolve()).strip('/').replace('/', '~')
    Path('agent-src/first.log').write_text('2021-02-22 17:00:00 First file\n')
    expected_dst_first_file = Path('server-dst') / getfqdn() / mangled_src_path / 'first.log'
    expected_dst_second_file = Path('server-dst') / getfqdn() / mangled_src_path / 'second.log'
    port = 9999
    with ExitStack() as stack:
        agent_cmd = [
            'logline-agent',
            '--scan', 'agent-src/*.log',
            '--server', f'127.0.0.1:{port}',
        ]
        server_cmd = [
            'logline-server',
            '--bind', f'127.0.0.1:{port}',
            '--dest', 'server-dst',
            '--client-token-hash', client_token_hash,
        ]
        server_process = stack.enter_context(Popen(server_cmd))
        stack.callback(terminate_process, server_process)
        sleep(.1)
        agent_process = stack.enter_context(Popen(agent_cmd, env={**os.environ, 'CLIENT_TOKEN': client_token}))
        stack.callback(terminate_process, agent_process)
        sleep(1)
        assert agent_process.poll() is None
        assert server_process.poll() is None
        assert expected_dst_first_file.exists()
        assert not expected_dst_second_file.exists()
        Path('agent-src/second.log').write_text('2021-02-22 17:00:10 Second file\n')
        t0 = monotime()
        while True:
            logger.debug('Checking after %.2f s...', monotime() - t0)
            assert agent_process.poll() is None
            assert server_process.poll() is None
            check_call(['find', str(tmp_path)], stdout=2)
            if not expected_dst_second_file.exists():
                logger.debug('Still no file in %s', expected_dst_second_file)
            else:
                assert expected_dst_second_file.read_text() == '2021-02-22 17:00:10 Second file\n'
                logger.debug('Second destination file created! %s', expected_dst_second_file)
                break
            if monotime() - t0 > 2:
                raise Exception('Deadline exceeded')
            sleep(.2)


def poll_until(condition, processes, timeout=15, what='condition'):
    '''Poll ``condition`` until true, asserting the given processes stay alive.'''
    t0 = monotime()
    while True:
        for p in processes:
            assert p.poll() is None, 'process died: {}'.format(' '.join(p.args))
        try:
            ok = condition()
        except FileNotFoundError:
            ok = False  # a file the condition reads has not appeared yet
        if ok:
            return
        if monotime() - t0 > timeout:
            check_call(['find', str(Path.cwd())], stdout=2)
            raise Exception('Deadline exceeded waiting for {}'.format(what))
        sleep(.1)


def test_rotate_log_file(tmp_path):
    '''
    Full agent-driven rotation against the v2 server, lh-logrotate style: the old
    inode is moved aside, the ``.lh-logrotate-waiting`` marker appears, and a new
    inode is created under the original name. The agent must seal the old segment
    under its dated name, stream the new inode into the live name, and finalize
    the sealed segment to ``<iso_dt>.<sha13>`` once the completion marker lands.
    '''
    chdir(tmp_path)
    Path('agent-src').mkdir()
    Path('server-dst').mkdir()
    mangled_src_path = str(Path('agent-src').resolve()).strip('/').replace('/', '~')
    first = '2021-02-22 17:10:00 First file\n'
    second = '2021-02-22 17:20:00 Second file\n'
    Path('agent-src/sample.log').write_text(first)
    dst_dir = Path('server-dst') / getfqdn() / mangled_src_path
    live = dst_dir / 'sample.log'
    iso = '20260101T120000Z'
    sha13 = 'abcdef0123456'
    sealed = dst_dir / 'sample.log.{}'.format(iso)
    final = dst_dir / 'sample.log.{}.{}'.format(iso, sha13)
    port = 9999
    with ExitStack() as stack:
        agent_cmd = [
            'logline-agent',
            '--scan', 'agent-src/*.log',
            '--server', f'127.0.0.1:{port}',
        ]
        server_cmd = [
            'logline-server',
            '--bind', f'127.0.0.1:{port}',
            '--dest', 'server-dst',
            '--client-token-hash', client_token_hash,
        ]
        server_process = stack.enter_context(Popen(server_cmd))
        stack.callback(terminate_process, server_process)
        sleep(.1)
        agent_process = stack.enter_context(Popen(agent_cmd, env={**os.environ, 'CLIENT_TOKEN': client_token}))
        stack.callback(terminate_process, agent_process)
        procs = [agent_process, server_process]

        poll_until(lambda: live.read_text() == first, procs, what='initial copy')

        # rotate lh-logrotate style: move old inode aside, drop the waiting
        # marker, then create a fresh inode under the original name.
        Path('agent-src/sample.log').rename('agent-src/sample.log.{}'.format(iso))
        Path('agent-src/sample.log.{}.lh-logrotate-waiting'.format(iso)).write_text('')
        Path('agent-src/sample.log').write_text(second)

        # the new live segment streams the new inode into the live name
        poll_until(lambda: live.read_text() == second, procs, what='new live segment')
        # the old segment is sealed under its dated name
        poll_until(lambda: sealed.read_text() == first, procs, what='sealed segment')

        # completion marker -> finalize the sealed segment to <iso_dt>.<sha13>
        Path('agent-src/sample.log.{}.{}.xz.gpg.lh-logrotate-compressed'.format(iso, sha13)).write_text('')
        poll_until(lambda: final.read_text() == first, procs, what='finalized segment')
        assert not sealed.exists()


def terminate_process(p):
    if p.poll() is None:
        logger.info('Terminating process %s args: %s', p.pid, ' '.join(p.args))
        p.terminate()
