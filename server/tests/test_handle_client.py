from asyncio import IncompleteReadError, run
import json
from types import SimpleNamespace

from logline_server.main import handle_client, sha1_b64, sha1_hex


CLIENT_TOKEN = 'secret'
PREFIX_DATA = b'hello world, this is a log file prefix'


def frame(command, metadata, data=None):
    '''Encode a single protocol message the way an agent would send it.'''
    metadata_bytes = json.dumps(metadata).encode('utf-8')
    if data is None:
        line = f'{command} {len(metadata_bytes)}\n'.encode('ascii')
        return line + metadata_bytes
    line = f'{command} {len(metadata_bytes)} {len(data)}\n'.encode('ascii')
    return line + metadata_bytes + data


def valid_header(**overrides):
    header = {
        'hostname': 'host.example.com',
        'directory': '/var/log',
        'target': 'app.log',
        'prefix': {'length': len(PREFIX_DATA), 'sha1': sha1_b64(PREFIX_DATA)},
        'auth': {'client_token': CLIENT_TOKEN},
    }
    header.update(overrides)
    return header


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
    '''Run handle_client against the given protocol messages and return the writer.'''
    reader = FakeReader(b''.join(messages))
    writer = FakeWriter()
    run(handle_client(conf, reader, writer))
    return writer


def dst_file(tmp_path):
    return tmp_path.resolve() / 'host.example.com' / 'var~log' / 'app.log'


def test_happy_path_writes_data(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        frame('logline-agent-v1', valid_header()),
        frame('data', {'offset': 0, 'compression': None}, b'log line\n'),
    )
    # Two 'ok' replies: the header reply and the data reply.
    assert writer.buffer.count(b'ok') == 2
    assert dst_file(tmp_path).read_bytes() == b'log line\n'


def test_non_dict_header_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', ['not', 'a', 'dict']))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_non_string_hostname_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', valid_header(hostname=123)))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_non_string_directory_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', valid_header(directory=['/var/log'])))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_non_dict_prefix_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', valid_header(prefix='not-a-dict')))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_invalid_prefix_length_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', valid_header(prefix={'length': 'lots', 'sha1': 'x'})))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_non_dict_auth_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(conf, frame('logline-agent-v1', valid_header(auth='not-a-dict')))
    assert b'ok' not in writer.buffer
    assert not dst_file(tmp_path).exists()


def test_non_dict_data_metadata_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        frame('logline-agent-v1', valid_header()),
        frame('data', ['not', 'a', 'dict'], b'log line\n'),
    )
    # The header reply succeeds, but the malformed first frame is rejected
    # before the target is lazily created, so no file appears.
    assert writer.buffer.count(b'ok') == 1
    assert not dst_file(tmp_path).exists()


def test_non_int_offset_is_rejected(tmp_path):
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        frame('logline-agent-v1', valid_header()),
        frame('data', {'offset': 'zero', 'compression': None}, b'log line\n'),
    )
    # A non-zero/invalid offset on the first frame is rejected before the
    # target is lazily created, so no file appears.
    assert writer.buffer.count(b'ok') == 1
    assert not dst_file(tmp_path).exists()


def test_boolean_offset_is_rejected(tmp_path):
    # bool is a subclass of int, so True must not be accepted as offset 1/0.
    conf = make_conf(tmp_path)
    writer = drive(
        conf,
        frame('logline-agent-v1', valid_header()),
        frame('data', {'offset': False, 'compression': None}, b'log line\n'),
    )
    # A boolean offset is rejected before the target is lazily created
    # (bool is not accepted even though False == 0), so no file appears.
    assert writer.buffer.count(b'ok') == 1
    assert not dst_file(tmp_path).exists()
