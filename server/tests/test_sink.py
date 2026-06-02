from hashlib import sha256

from pytest import raises

from logline_server.framing import ProtocolError
from logline_server.sink import open_sink


def make_sink(tmp_path, name='host/var~log/app.log', prefix=b'', fsync=False):
    dst = tmp_path / name
    prefix_sha = sha256(prefix).hexdigest()
    return open_sink(dst, len(prefix), prefix_sha, fsync), dst


def test_new_file_starts_at_zero(tmp_path):
    sink, dst = make_sink(tmp_path)
    assert sink.offset == 0
    sink.write(0, b'hello world\n')
    sink.close()
    assert dst.read_bytes() == b'hello world\n'


def test_sequential_writes_append(tmp_path):
    sink, dst = make_sink(tmp_path)
    sink.write(0, b'aaaa')
    sink.write(4, b'bbbb')
    sink.close()
    assert dst.read_bytes() == b'aaaabbbb'


def test_duplicate_write_is_dropped(tmp_path):
    sink, dst = make_sink(tmp_path)
    sink.write(0, b'aaaa')
    # Re-sending the same offset (e.g. after a reconnect) must not duplicate.
    assert sink.write(0, b'aaaa') == 0
    sink.write(4, b'bbbb')
    sink.close()
    assert dst.read_bytes() == b'aaaabbbb'


def test_partially_overlapping_write_only_appends_tail(tmp_path):
    sink, dst = make_sink(tmp_path)
    sink.write(0, b'aaaabbbb')
    # Overlaps the last 4 bytes and adds 4 new ones.
    written = sink.write(4, b'bbbbcccc')
    assert written == 4
    sink.close()
    assert dst.read_bytes() == b'aaaabbbbcccc'


def test_gap_is_rejected(tmp_path):
    sink, _ = make_sink(tmp_path)
    sink.write(0, b'aaaa')
    with raises(ProtocolError):
        sink.write(8, b'cccc')  # offset 8 leaves a gap (have 4)


def test_resume_reopens_at_current_length(tmp_path):
    prefix = b'2021-02-22 first line\n'
    sink, dst = make_sink(tmp_path, prefix=prefix)
    sink.write(0, prefix + b'more\n')
    sink.close()
    # Reopen with the same prefix -> resume, do not rotate.
    sink2, _ = make_sink(tmp_path, prefix=prefix)
    assert sink2.offset == len(prefix) + len(b'more\n')
    sink2.close()
    assert not list(dst.parent.glob('*.rotated-*'))


def test_changed_prefix_rotates(tmp_path):
    sink, dst = make_sink(tmp_path, prefix=b'old-prefix-data')
    sink.write(0, b'old-prefix-data and more')
    sink.close()
    # A different prefix means the source was rotated: keep the old file aside.
    sink2, _ = make_sink(tmp_path, prefix=b'new-prefix-data')
    assert sink2.offset == 0
    sink2.write(0, b'new content')
    sink2.close()
    rotated = list(dst.parent.glob('app.log.rotated-*'))
    assert len(rotated) == 1
    assert rotated[0].read_bytes() == b'old-prefix-data and more'
    assert dst.read_bytes() == b'new content'
