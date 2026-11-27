'''
Protocol-level tests for the server: explicit agent-named target, length +
prefix_sha1 reporting (never rotate), idempotent in-band rename (the open fd
survives the rename), and the explicit offset check.

Uses the same in-process ``handle_client`` harness as ``test_handle_client``: a
whole scripted message sequence is queued up front and replayed in one go.
'''

from asyncio import IncompleteReadError, run
import json
from types import SimpleNamespace

from pytest import raises

from logline_server.main import ProtocolError, apply_data, handle_client, handle_rename, sha1_b64, sha1_hex


CLIENT_TOKEN = 'secret'


def frame(command, metadata, data=None):
    metadata_bytes = json.dumps(metadata).encode('utf-8')
    if data is None:
        return f'{command} {len(metadata_bytes)}\n'.encode('ascii') + metadata_bytes
    return f'{command} {len(metadata_bytes)} {len(data)}\n'.encode('ascii') + metadata_bytes + data


def hello(target, prefix, directory='/var/log', **overrides):
    header = {
        'hostname': 'host.example.com',
        'directory': directory,
        'target': target,
        'prefix': {'length': len(prefix), 'sha1': sha1_b64(prefix)},
        'auth': {'client_token': CLIENT_TOKEN},
    }
    header.update(overrides)
    return frame('logline-agent-v1', header)


def data_frame(offset, payload):
    return frame('data', {'offset': offset, 'compression': None}, payload)


def rename_frame(src, dst):
    return frame('rename', {'from': src, 'to': dst})


class FakeReader:
    def __init__(self, data):
        self._buf = bytearray(data)

    async def readline(self):
        idx = self._buf.find(b'\n')
        if idx == -1:
            rest = bytes(self._buf)
            self._buf.clear()
            return rest
        line = bytes(self._buf[: idx + 1])
        del self._buf[: idx + 1]
        return line

    async def readexactly(self, n):
        if len(self._buf) < n:
            chunk = bytes(self._buf)
            self._buf.clear()
            raise IncompleteReadError(chunk, n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, name):
        return ('127.0.0.1', 12345)

    def write(self, data):
        self.buffer.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def make_conf(tmp_path):
    return SimpleNamespace(
        destination_directory=tmp_path,
        client_token_hashes={sha1_hex(CLIENT_TOKEN.encode('utf-8'))},
    )


def drive(conf, *messages):
    reader = FakeReader(b''.join(messages))
    writer = FakeWriter()
    run(handle_client(conf, reader, writer))
    return writer


def replies(writer):
    '''Parse the server's reply stream into a list of (status, payload).'''
    buf = bytes(writer.buffer)
    out = []
    i = 0
    while i < len(buf):
        nl = buf.index(b'\n', i)
        parts = buf[i:nl].decode('ascii').split()
        i = nl + 1
        payload = None
        if len(parts) == 2:
            n = int(parts[1])
            payload = json.loads(buf[i:i + n])
            i += n
        out.append((parts[0], payload))
    return out


def dst_dir(tmp_path):
    return tmp_path.resolve() / 'host.example.com' / 'var~log'


# --- tests ----------------------------------------------------------------


def test_create_and_append(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        hello('app.log', b'2026'),
        data_frame(0, b'2026 hello'),
        data_frame(10, b' world'),
    )
    assert [r[0] for r in replies(writer)] == ['ok', 'ok', 'ok']
    assert replies(writer)[0][1] == {'length': 0, 'prefix_sha1': None}
    assert (dst_dir(tmp_path) / 'app.log').read_bytes() == b'2026 hello world'


def test_reports_existing_length_and_prefix(tmp_path):
    d = dst_dir(tmp_path)
    d.mkdir(parents=True)
    (d / 'app.log').write_bytes(b'2026 existing content')
    writer = drive(make_conf(tmp_path), hello('app.log', b'2026'))
    status, payload = replies(writer)[0]
    assert status == 'ok'
    assert payload['length'] == len(b'2026 existing content')
    assert payload['prefix_sha1'] == sha1_b64(b'2026')


def test_never_rotates_on_prefix_mismatch(tmp_path):
    d = dst_dir(tmp_path)
    d.mkdir(parents=True)
    (d / 'app.log').write_bytes(b'OLD content')
    # the agent's prefix does not match; the server must just report, never rotate
    writer = drive(make_conf(tmp_path), hello('app.log', b'NEWp'))
    assert replies(writer)[0][1]['prefix_sha1'] == sha1_b64(b'OLD ')
    assert (d / 'app.log').read_bytes() == b'OLD content'
    assert sorted(p.name for p in d.iterdir()) == ['app.log']


def test_rename_fd_survives_and_keeps_appending(tmp_path):
    conf = make_conf(tmp_path)
    drive(
        conf,
        hello('app.log', b'2026'),
        data_frame(0, b'first segment\n'),
        rename_frame('app.log', 'app.log.20260101T120000Z'),
        data_frame(14, b'late line\n'),  # trailing write lands in the renamed file
    )
    d = dst_dir(tmp_path)
    assert not (d / 'app.log').exists()
    assert (d / 'app.log.20260101T120000Z').read_bytes() == b'first segment\nlate line\n'


def test_rename_finalize_then_idempotent_replay(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        hello('app.log', b'2026'),
        data_frame(0, b'segment body\n'),
        rename_frame('app.log', 'app.log.20260101T120000Z'),
        rename_frame('app.log.20260101T120000Z', 'app.log.20260101T120000Z.bd0fe0ff5ceeb'),
        # replay of the exact finalize: from is gone, to exists -> ok
        rename_frame('app.log.20260101T120000Z', 'app.log.20260101T120000Z.bd0fe0ff5ceeb'),
    )
    assert [r[0] for r in replies(writer)] == ['ok'] * 5
    d = dst_dir(tmp_path)
    assert (d / 'app.log.20260101T120000Z.bd0fe0ff5ceeb').read_bytes() == b'segment body\n'
    assert sorted(p.name for p in d.iterdir()) == ['app.log.20260101T120000Z.bd0fe0ff5ceeb']


def test_offset_mismatch_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        hello('app.log', b'2026'),
        data_frame(0, b'hello'),
        data_frame(999, b'!'),  # wrong offset -> ProtocolError, connection closed
    )
    # header reply + first data reply only; the bad frame gets no 'ok'
    assert [r[0] for r in replies(writer)] == ['ok', 'ok']
    assert (dst_dir(tmp_path) / 'app.log').read_bytes() == b'hello'


def test_offset_is_checked_against_file_size_not_fd_position(tmp_path):
    # Two connections hold the same target open: a stale one the agent gave up
    # on, and the one it reconnected with. After the new one appends, the stale
    # fd's position no longer matches the file. A late frame on the stale fd at
    # its old position must not overwrite the appended bytes, and the next frame
    # at the real end of file must be accepted on either fd.
    path = tmp_path / 'app.log'
    path.write_bytes(b'')
    meta = lambda offset: {'offset': offset, 'compression': None}  # noqa: E731
    with path.open('rb+') as stale, path.open('rb+') as fresh:
        run(apply_data(fresh, path, meta(0), b'first line\n'))
        with raises(ProtocolError):
            run(apply_data(stale, path, meta(0), b'XXXXXXXXXXX\n'))
        run(apply_data(stale, path, meta(11), b'second line\n'))
        run(apply_data(fresh, path, meta(23), b'third line\n'))
    assert path.read_bytes() == b'first line\nsecond line\nthird line\n'


def test_late_first_frame_does_not_truncate_a_target_created_meanwhile(tmp_path):
    # A stale connection saw no target in its handshake. Meanwhile another
    # connection created the target and wrote to it. The stale connection's late
    # first frame at offset 0 must be rejected, not truncate the file.
    d = dst_dir(tmp_path)
    d.mkdir(parents=True)

    class CreateOnHandshake(FakeReader):
        async def readline(self):
            line = await super().readline()
            if line.startswith(b'data'):
                (d / 'app.log').write_bytes(b'written by the other connection\n')
            return line

    reader = CreateOnHandshake(hello('app.log', b'x') + data_frame(0, b'late frame\n'))
    run(handle_client(make_conf(tmp_path), reader, FakeWriter()))
    assert (d / 'app.log').read_bytes() == b'written by the other connection\n'


# --- handle_rename unit tests ---------------------------------------------


def test_handle_rename_neither_side_exists(tmp_path):
    with raises(ProtocolError):
        handle_rename(tmp_path, tmp_path / 'app.log', None,
                      {'from': 'app.log', 'to': 'app.log.sealed'})


def test_handle_rename_rejects_traversal(tmp_path):
    (tmp_path / 'app.log').write_bytes(b'x')
    with raises(ProtocolError):
        handle_rename(tmp_path, tmp_path / 'app.log', None,
                      {'from': 'app.log', 'to': '../escape'})


def test_handle_rename_idempotent_when_already_done(tmp_path):
    (tmp_path / 'sealed').write_bytes(b'x')
    result = handle_rename(tmp_path, tmp_path / 'live', None,
                           {'from': 'live', 'to': 'sealed'})
    assert result == tmp_path / 'live'
    assert (tmp_path / 'sealed').read_bytes() == b'x'
