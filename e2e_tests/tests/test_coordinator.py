'''
Integration tests for the agent rotation coordinator (PLAN_v2), driving the real
``PathCoordinator`` against the real in-process v2 server over a loopback socket.

A rotation is simulated the way lh-logrotate performs it: the old inode is
renamed aside (the agent's open fd follows it), a fresh file is created under the
original name (a new inode), and the marker siblings appear in the directory.
'''

import asyncio
from functools import partial
from socket import getfqdn
from time import monotonic
from types import SimpleNamespace

from logline_agent.client import connect_to_server
from logline_agent.coordinator import PathCoordinator
from logline_server.main import handle_client, sha1_hex


CLIENT_TOKEN = 'topsecret'
ISO = '20260101T120000Z'
SHA13 = 'abcdef0123456'


def make_server_conf(dest_dir):
    return SimpleNamespace(
        destination_directory=dest_dir,
        client_token_hashes={sha1_hex(CLIENT_TOKEN.encode('utf-8'))},
    )


def make_agent_conf(port):
    return SimpleNamespace(
        server_host='127.0.0.1', server_port=port,
        client_token=CLIENT_TOKEN, use_tls=False, tls_cert_file=None,
        prefix_length=50, min_prefix_length=20,
        tail_read_interval=0.02, scan_new_files_interval=0.02,
        rotated_files_inactivity_threshold=0.2,
        seal_marker_grace=0.15, seal_idle=0.2)


def dst_dir_for(dst, src):
    mangled = str(src.resolve()).strip('/').replace('/', '~')
    return dst / getfqdn() / mangled


async def wait_until(pred, timeout=5.0):
    t0 = monotonic()
    while True:
        try:
            ok = pred()
        except FileNotFoundError:
            ok = False  # a file the predicate reads has not appeared yet
        if ok:
            return
        if monotonic() - t0 > timeout:
            raise AssertionError('timed out waiting for condition')
        await asyncio.sleep(0.02)


async def cancel(task):
    task.cancel()
    try:
        await task
    except BaseException:
        pass


async def harness(tmp_path, scenario):
    src = tmp_path / 'src'
    src.mkdir()
    dst = tmp_path / 'dst'
    dst.mkdir()
    server = await asyncio.start_server(
        partial(handle_client, make_server_conf(dst)), '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    agent_conf = make_agent_conf(port)
    try:
        await scenario(src, dst, agent_conf)
    finally:
        server.close()
        await server.wait_closed()


def start_coordinator(agent_conf, file_path):
    factory = partial(connect_to_server, conf=agent_conf)
    coord = PathCoordinator(agent_conf, file_path.resolve(), factory)
    return coord, asyncio.create_task(coord.run())


def rotate_aside(src, basename, iso):
    '''Simulate lh-logrotate moving the current inode aside under a dated name.'''
    (src / basename).rename(src / '{}.{}'.format(basename, iso))
    return src / '{}.{}'.format(basename, iso)


def run(tmp_path, scenario):
    asyncio.run(harness(tmp_path, scenario))


# --- tests ----------------------------------------------------------------


def test_clean_rotation_seal_and_finalize(tmp_path):
    async def scenario(src, dst, conf):
        foo = src / 'foo.log'
        foo.write_bytes(b'2026-01-01 12:00:00 line one\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').exists()
                             and (d / 'foo.log').read_bytes() == b'2026-01-01 12:00:00 line one\n')

            # rotate: old inode aside, marker appears, new inode under foo.log
            old = rotate_aside(src, 'foo.log', ISO)
            (src / 'foo.log.{}.lh-logrotate-waiting'.format(ISO)).write_bytes(b'')
            foo.write_bytes(b'2026-01-01 12:05:00 line two\n')
            # the producer keeps appending to the old inode after the rename
            with old.open('ab') as f:
                f.write(b'2026-01-01 12:00:01 trailing\n')

            sealed = d / 'foo.log.{}'.format(ISO)
            await wait_until(lambda: sealed.exists())
            # distinct names, concurrent streams: new live has only the new inode
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'2026-01-01 12:05:00 line two\n')
            await wait_until(lambda: sealed.read_bytes()
                             == b'2026-01-01 12:00:00 line one\n2026-01-01 12:00:01 trailing\n')

            # completion marker -> finalize to the hashed name
            (src / 'foo.log.{}.{}.xz.gpg.lh-logrotate-compressed'.format(ISO, SHA13)).write_bytes(b'')
            final = d / 'foo.log.{}.{}'.format(ISO, SHA13)
            await wait_until(lambda: final.exists())
            assert final.read_bytes() == b'2026-01-01 12:00:00 line one\n2026-01-01 12:00:01 trailing\n'
            assert not sealed.exists()
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_uploaded_marker_is_finalize_fallback(tmp_path):
    async def scenario(src, dst, conf):
        foo = src / 'foo.log'
        foo.write_bytes(b'aaaa first segment body\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').exists())
            rotate_aside(src, 'foo.log', ISO)
            (src / 'foo.log.{}.lh-logrotate-waiting'.format(ISO)).write_bytes(b'')
            foo.write_bytes(b'bbbb second segment body\n')
            await wait_until(lambda: (d / 'foo.log.{}'.format(ISO)).exists())
            # only the uploaded marker exists (agent missed the compressed one)
            (src / 'foo.log.{}.{}.xz.gpg.lh-logrotate-uploaded'.format(ISO, SHA13)).write_bytes(b'')
            await wait_until(lambda: (d / 'foo.log.{}.{}'.format(ISO, SHA13)).exists())
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_orphan_rotation_without_markers(tmp_path):
    async def scenario(src, dst, conf):
        foo = src / 'foo.log'
        foo.write_bytes(b'orphan first body\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').exists())
            # rotate with NO markers at all
            rotate_aside(src, 'foo.log', ISO)
            foo.write_bytes(b'orphan second body\n')

            def orphan_sealed():
                return [p for p in d.iterdir() if p.name.endswith('.orphan')]
            await wait_until(lambda: bool(orphan_sealed()))
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'orphan second body\n')
            orphan = orphan_sealed()[0]
            assert orphan.read_bytes() == b'orphan first body\n'
            # trailing component is the literal 'orphan'
            assert orphan.name.startswith('foo.log.')
            assert orphan.name.endswith('.orphan')
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_no_write_before_seal_recovery(tmp_path):
    async def scenario(src, dst, conf):
        # server already holds a different (older) file under the live target
        d = dst_dir_for(dst, src)
        d.mkdir(parents=True)
        (d / 'foo.log').write_bytes(b'OLD stale content from a missed segment\n')

        foo = src / 'foo.log'
        foo.write_bytes(b'NEW current inode content\n')
        coord, task = start_coordinator(conf, foo)
        try:
            # the stale remote file is sealed aside (orphan), not appended to
            def orphan_sealed():
                return [p for p in d.iterdir() if p.name.endswith('.orphan')]
            await wait_until(lambda: bool(orphan_sealed()))
            assert orphan_sealed()[0].read_bytes() == b'OLD stale content from a missed segment\n'
            # and the new content is streamed cleanly into the live target
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'NEW current inode content\n')
        finally:
            await cancel(task)

    run(tmp_path, scenario)
