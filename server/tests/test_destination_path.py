from pytest import raises

from logline_server.main import ProtocolError, build_destination_path


def test_normal_path(tmp_path):
    dst = build_destination_path(tmp_path, 'host.example.com', '/var/log/app.log')
    assert dst == tmp_path.resolve() / 'host.example.com' / 'var~log' / 'app.log'


def test_path_without_directory(tmp_path):
    dst = build_destination_path(tmp_path, 'host.example.com', '/app.log')
    assert dst == tmp_path.resolve() / 'host.example.com' / 'app.log'


def test_resulting_path_stays_inside_destination(tmp_path):
    dst = build_destination_path(tmp_path, 'host.example.com', '/var/log/app.log')
    assert tmp_path.resolve() in dst.parents


def test_hostname_traversal_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, '../../../../etc/cron.d', '/evil')


def test_hostname_dotdot_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, '..', '/x')


def test_hostname_with_slash_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, 'a/b', '/x')


def test_empty_hostname_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, '', '/x')


def test_hostname_with_null_byte_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, 'host\x00', '/x')


def test_filename_traversal_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, 'host', '/foo/..')


def test_empty_path_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_path(tmp_path, 'host', '/')


def test_non_string_hostname_is_rejected(tmp_path):
    for bad_hostname in (123, ['host'], {'h': 1}, None):
        with raises(ProtocolError):
            build_destination_path(tmp_path, bad_hostname, '/x')


def test_non_string_path_is_rejected(tmp_path):
    for bad_path in (456, ['/x'], {'p': 1}, None):
        with raises(ProtocolError):
            build_destination_path(tmp_path, 'host', bad_path)


def test_traversal_does_not_escape_destination(tmp_path):
    # Even a crafted combination must never resolve outside the destination dir.
    base = tmp_path.resolve()
    for hostname, path in [
        ('../../tmp', '/evil'),
        ('host', '/../../etc/passwd'),
    ]:
        try:
            dst = build_destination_path(tmp_path, hostname, path)
        except ProtocolError:
            continue
        assert base in dst.parents
