'''
Integration tests for the agent rotation coordinator, driving the real
``PathCoordinator`` against the real in-process server over a loopback socket.

A rotation is simulated the way lh-logrotate performs it: the old inode is
renamed aside (the agent's open fd follows it), a fresh file is created under the
original name (a new inode), and the marker siblings appear in the directory.
'''

import asyncio
from functools import partial
import socket
from socket import getfqdn
import sys
from time import monotonic
from types import SimpleNamespace

from logline_agent.client import ClientError, connect_to_server
from logline_agent.coordinator import PathCoordinator
from logline_server.main import handle_client, sha1_hex
from pytest import mark, raises


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
        prefix_length_bytes=50, min_prefix_length_bytes=20,
        tail_read_interval_seconds=0.02, scan_new_files_interval_seconds=0.02,
        rotated_files_inactivity_threshold_seconds=0.2,
        seal_marker_grace_seconds=0.15, seal_idle_seconds=0.2,
        deleted_path_idle_seconds=5, reply_timeout_seconds=60,
        ephemeral_globs=[])


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
            # a non-ephemeral source keeps the timestamped name, not the fixed one
            assert orphan.name != 'foo.log.orphan'
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_ephemeral_orphan_uses_fixed_name_and_overwrites(tmp_path):
    async def scenario(src, dst, conf):
        conf.ephemeral_globs = [str(src.resolve() / '*.log')]
        foo = src / 'foo.log'
        foo.write_bytes(b'body one\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'body one\n')

            # first markerless rotation -> fixed foo.log.orphan holds 'body one'
            rotate_aside(src, 'foo.log', ISO)
            foo.write_bytes(b'body two\n')
            orphan = d / 'foo.log.orphan'
            await wait_until(lambda: orphan.read_bytes() == b'body one\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'body two\n')

            # second markerless rotation -> the same fixed name is overwritten
            rotate_aside(src, 'foo.log', ISO + '-2')
            foo.write_bytes(b'body three\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'body three\n')
            await wait_until(lambda: orphan.read_bytes() == b'body two\n')

            # exactly the live file plus a single fixed-name orphan remain
            assert sorted(p.name for p in d.iterdir()) == ['foo.log', 'foo.log.orphan']
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_ephemeral_orphan_closing_failure_does_not_resume(tmp_path):
    async def scenario(src, dst, conf):
        conf.ephemeral_globs = [str(src.resolve() / '*.log')]
        # Keep the closing orphan segment alive long enough to break its
        # connection mid-drain, instead of letting it idle-close first.
        conf.seal_idle_seconds = 5
        foo = src / 'foo.log'
        foo.write_bytes(b'body one\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'body one\n')

            # markerless rotation -> the fixed foo.log.orphan is sealed
            old = rotate_aside(src, 'foo.log', ISO)
            foo.write_bytes(b'body two\n')
            orphan = d / 'foo.log.orphan'
            await wait_until(lambda: orphan.read_bytes() == b'body one\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'body two\n')

            # grab the closing orphan segment once it is in fail-closed mode
            await wait_until(lambda: bool(coord.closing))
            seg = next(iter(coord.closing))
            await wait_until(lambda: seg._no_resume)

            # Break its connection, then feed the old inode fresh bytes. The
            # closing drain must fail and END the segment rather than reconnect
            # and resume: a fixed-name orphan could be a newer file by now, so
            # resuming stale bytes onto it would corrupt it.
            seg.conn.close()
            with old.open('ab') as f:
                f.write(b'STALE trailing that must never reach the orphan\n')

            await wait_until(lambda: seg.task.done())
            # the sealed orphan was not appended to after the failure
            assert orphan.read_bytes() == b'body one\n'
            # and the live segment is unaffected -- it keeps streaming
            with foo.open('ab') as f:
                f.write(b'body two continues\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes()
                             == b'body two\nbody two continues\n')
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


def orphans_in(d):
    return [p for p in d.iterdir() if p.name.endswith('.orphan')]


def test_empty_new_inode_does_not_resume_onto_stale_target(tmp_path):
    async def scenario(src, dst, conf):
        # The server still holds the previous inode's content under the live
        # target (e.g. the agent restarted across a rotation), while the new
        # inode is still empty when the agent connects.
        d = dst_dir_for(dst, src)
        d.mkdir(parents=True)
        (d / 'foo.log').write_bytes(b'OLD content of the previous inode\n')

        foo = src / 'foo.log'
        foo.write_bytes(b'')
        coord, task = start_coordinator(conf, foo)
        try:
            await wait_until(lambda: coord.live is not None and coord.live.conn is not None)
            with foo.open('ab') as f:
                f.write(b'NEW\n')
            # An empty local prefix must not count as a match: the stale target is
            # sealed aside instead of being appended to at its old length.
            await wait_until(lambda: bool(orphans_in(d)))
            assert orphans_in(d)[0].read_bytes() == b'OLD content of the previous inode\n'
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'NEW\n')
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_local_file_shorter_than_target_is_a_different_file(tmp_path):
    async def scenario(src, dst, conf):
        # Both files start with the same bytes, but the local file is shorter
        # than the server target, so it cannot be the inode the target mirrors.
        d = dst_dir_for(dst, src)
        d.mkdir(parents=True)
        (d / 'foo.log').write_bytes(b'same start\nmore lines the new inode never had\n')

        foo = src / 'foo.log'
        foo.write_bytes(b'same start\n')
        coord, task = start_coordinator(conf, foo)
        try:
            await wait_until(lambda: bool(orphans_in(d)))
            assert orphans_in(d)[0].read_bytes() == b'same start\nmore lines the new inode never had\n'
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'same start\n')
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_resume_when_target_is_shorter_than_prefix(tmp_path):
    async def scenario(src, dst, conf):
        # The server target holds fewer bytes than the agent's prefix length, and
        # they are the beginning of the local file: this is a plain resume. Both
        # sides must hash the same number of bytes, or the match is missed and the
        # target is needlessly sealed aside as an orphan.
        d = dst_dir_for(dst, src)
        d.mkdir(parents=True)
        (d / 'foo.log').write_bytes(b'line one\n')

        content = b'line one\n' + b'line two is long enough to exceed the prefix window\n'
        foo = src / 'foo.log'
        foo.write_bytes(content)
        coord, task = start_coordinator(conf, foo)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == content)
            assert orphans_in(d) == []
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_truncated_live_file_is_sealed_aside_and_restreamed(tmp_path):
    async def scenario(src, dst, conf):
        foo = src / 'foo.log'
        foo.write_bytes(b'first generation, long enough to be truncated\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'first generation, long enough to be truncated\n')
            # copytruncate: the same inode shrinks below the shipped offset
            with foo.open('r+b') as f:
                f.truncate(0)
            with foo.open('ab') as f:
                f.write(b'second gen\n')
            # The old mirror is sealed aside as an orphan and the inode is
            # streamed again from offset 0 into a fresh live target.
            await wait_until(lambda: bool(orphans_in(d)))
            assert orphans_in(d)[0].read_bytes() == b'first generation, long enough to be truncated\n'
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'second gen\n')
            # bytes written past the old offset later must not land in the orphan
            with foo.open('ab') as f:
                f.write(b'and the rest of the second generation after the old offset\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes()
                             == b'second gen\nand the rest of the second generation after the old offset\n')
            assert orphans_in(d)[0].read_bytes() == b'first generation, long enough to be truncated\n'
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_deleted_path_is_drained_then_released(tmp_path):
    async def scenario(src, dst, conf):
        conf.deleted_path_idle_seconds = 0.3
        foo = src / 'foo.log'
        foo.write_bytes(b'line one\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'line one\n')
            seg = coord.live
            # The producer still holds the inode open after the path is deleted
            # and writes a last line: it must be shipped before giving up.
            with foo.open('ab') as producer:
                foo.unlink()
                producer.write(b'written after unlink\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'line one\nwritten after unlink\n')
            # Once drained and idle, the coordinator ends and releases the fd and
            # the server connection instead of holding them forever.
            await wait_until(lambda: task.done())
            assert task.exception() is None
            assert seg.task.done()
            assert seg.file_stream.closed
            assert seg.conn.writer.is_closing()
            # nothing is renamed for a plain deletion
            assert sorted(p.name for p in d.iterdir()) == ['foo.log']
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_rotated_away_path_never_recreated_completes_marker_seal(tmp_path):
    async def scenario(src, dst, conf):
        conf.deleted_path_idle_seconds = 0.3
        foo = src / 'foo.log'
        foo.write_bytes(b'only segment\n')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'only segment\n')
            # lh-logrotate rotates the file away, but nothing recreates foo.log
            rotate_aside(src, 'foo.log', ISO)
            (src / 'foo.log.{}.lh-logrotate-waiting'.format(ISO)).write_bytes(b'')
            # the pending marker-based seal is honoured, not dropped
            sealed = d / 'foo.log.{}'.format(ISO)
            await wait_until(lambda: sealed.exists())
            assert not task.done()
            (src / 'foo.log.{}.{}.xz.gpg.lh-logrotate-compressed'.format(ISO, SHA13)).write_bytes(b'')
            final = d / 'foo.log.{}.{}'.format(ISO, SHA13)
            await wait_until(lambda: final.exists())
            assert final.read_bytes() == b'only segment\n'
            await wait_until(lambda: task.done())
            assert task.exception() is None
            assert coord.live is None and not coord.closing
        finally:
            await cancel(task)

    run(tmp_path, scenario)


@mark.parametrize('dropped', ['seal', 'finalize'])
def test_dropped_rename_is_reapplied(tmp_path, dropped):
    async def scenario(src, dst, conf):
        # The first rename frame towards the chosen name is lost: the connection
        # drops before the server sees it. The agent must reapply the rename after
        # reconnecting instead of assuming it happened.
        drop_suffix = ISO if dropped == 'seal' else SHA13
        drops = []

        async def flaky_factory(**kwargs):
            conn = await connect_to_server(conf=conf, **kwargs)
            send_rename = conn.send_rename

            async def maybe_drop(src_name, dst_name):
                if not drops and dst_name.endswith(drop_suffix):
                    drops.append(dst_name)
                    conn.close()
                    raise ConnectionError('injected: rename frame lost')
                return await send_rename(src_name, dst_name)
            conn.send_rename = maybe_drop
            return conn

        foo = src / 'foo.log'
        foo.write_bytes(b'2026-01-01 12:00:00 line one\n')
        coord = PathCoordinator(conf, foo.resolve(), flaky_factory)
        task = asyncio.create_task(coord.run())
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: (d / 'foo.log').exists())
            old = rotate_aside(src, 'foo.log', ISO)
            (src / 'foo.log.{}.lh-logrotate-waiting'.format(ISO)).write_bytes(b'')
            foo.write_bytes(b'2026-01-01 12:05:00 line two\n')
            with old.open('ab') as f:
                f.write(b'2026-01-01 12:00:01 trailing\n')
            sealed = d / 'foo.log.{}'.format(ISO)
            await wait_until(lambda: sealed.exists()
                             and sealed.read_bytes() == b'2026-01-01 12:00:00 line one\n2026-01-01 12:00:01 trailing\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'2026-01-01 12:05:00 line two\n')

            (src / 'foo.log.{}.{}.xz.gpg.lh-logrotate-compressed'.format(ISO, SHA13)).write_bytes(b'')
            final = d / 'foo.log.{}.{}'.format(ISO, SHA13)
            await wait_until(lambda: final.exists())
            await wait_until(lambda: not coord.closing)
            assert drops, 'the rename drop was not injected'
            assert final.read_bytes() == b'2026-01-01 12:00:00 line one\n2026-01-01 12:00:01 trailing\n'
            assert sorted(p.name for p in d.iterdir()) == ['foo.log', final.name]
        finally:
            await cancel(task)

    run(tmp_path, scenario)


def test_rotation_of_never_shipped_empty_file(tmp_path):
    async def scenario(src, dst, conf):
        # The live target was never created on the server (empty source), so the
        # seal and finalize renames have nothing to rename; the old inode's late
        # bytes must still end up under the finalized name.
        foo = src / 'foo.log'
        foo.write_bytes(b'')
        coord, task = start_coordinator(conf, foo)
        d = dst_dir_for(dst, src)
        try:
            await wait_until(lambda: coord.live is not None and coord.live.conn is not None)
            old = rotate_aside(src, 'foo.log', ISO)
            (src / 'foo.log.{}.lh-logrotate-waiting'.format(ISO)).write_bytes(b'')
            foo.write_bytes(b'new inode\n')
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'new inode\n')
            with old.open('ab') as f:
                f.write(b'late line in the old inode\n')
            await wait_until(lambda: (d / 'foo.log.{}'.format(ISO)).exists())
            (src / 'foo.log.{}.{}.xz.gpg.lh-logrotate-compressed'.format(ISO, SHA13)).write_bytes(b'')
            final = d / 'foo.log.{}.{}'.format(ISO, SHA13)
            await wait_until(lambda: final.exists())
            assert final.read_bytes() == b'late line in the old inode\n'
            assert sorted(p.name for p in d.iterdir()) == ['foo.log', final.name]
        finally:
            await cancel(task)

    run(tmp_path, scenario)


@mark.skipif(not sys.platform.startswith('linux'), reason='keepalive tuning options are Linux-specific')
def test_agent_connection_has_keepalive(tmp_path):
    async def scenario(src, dst, conf):
        (src / 'foo.log').write_bytes(b'x')
        conn = await connect_to_server(conf=conf, log_path=src / 'foo.log', target='foo.log', log_prefix=b'x')
        try:
            sock = conn.writer.get_extra_info('socket')
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
        finally:
            conn.close()

    run(tmp_path, scenario)


def test_unresponsive_server_times_out_per_reply_timeout(tmp_path):
    async def scenario():
        # A server that accepts the connection but never replies, like the far
        # end of a half-open connection.
        async def silent(reader, writer):
            await reader.read()

        server = await asyncio.start_server(silent, '127.0.0.1', 0)
        conf = make_agent_conf(server.sockets[0].getsockname()[1])
        conf.reply_timeout_seconds = 0.3
        try:
            t0 = monotonic()
            with raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    connect_to_server(conf=conf, log_path=tmp_path / 'foo.log', target='foo.log', log_prefix=b''), 5)
            assert monotonic() - t0 < 2
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_initial_connect_failure_backs_off_and_recovers(tmp_path):
    async def scenario():
        src = tmp_path / 'src'
        src.mkdir()
        dst = tmp_path / 'dst'
        dst.mkdir()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        conf = make_agent_conf(port)
        attempts = []

        async def counting_factory(**kwargs):
            attempts.append(monotonic())
            return await connect_to_server(conf=conf, **kwargs)

        foo = src / 'foo.log'
        foo.write_bytes(b'shipped once the server is up\n')
        coord = PathCoordinator(conf, foo.resolve(), counting_factory)
        task = asyncio.create_task(coord.run())
        server = None
        try:
            # The server is down: the first connect must be retried with backoff
            # by the same segment, not fail the segment and get respawned on
            # every coordinator tick.
            await asyncio.sleep(0.4)
            assert 1 <= len(attempts) <= 2
            seg = coord.live
            assert seg is not None and not seg.task.done()

            server = await asyncio.start_server(partial(handle_client, make_server_conf(dst)), '127.0.0.1', port)
            d = dst_dir_for(dst, src)
            await wait_until(lambda: (d / 'foo.log').read_bytes() == b'shipped once the server is up\n')
            assert coord.live is seg
        finally:
            await cancel(task)
            if server is not None:
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())


def test_agent_receives_explicit_error_reply(tmp_path):
    async def scenario(src, dst, conf):
        # The server answers a rejected handshake with an explicit `error` reply,
        # which the agent surfaces as a ClientError (and treats as a failed
        # connection), instead of a bare connection close.
        conf.client_token = 'not the right token'
        with raises(ClientError, match='Error reply'):
            await connect_to_server(conf=conf, log_path=src / 'foo.log', target='foo.log', log_prefix=b'')

    run(tmp_path, scenario)
