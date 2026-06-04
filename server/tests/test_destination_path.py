from pytest import raises

from logline_server.main import ProtocolError, build_destination_dir


def test_normal_directory(tmp_path):
    dst = build_destination_dir(tmp_path, 'host.example.com', '/var/log')
    assert dst == tmp_path.resolve() / 'host.example.com' / 'var~log'


def test_root_directory(tmp_path):
    # A file directly in '/' maps straight under the host directory.
    dst = build_destination_dir(tmp_path, 'host.example.com', '/')
    assert dst == tmp_path.resolve() / 'host.example.com'


def test_empty_directory(tmp_path):
    dst = build_destination_dir(tmp_path, 'host.example.com', '')
    assert dst == tmp_path.resolve() / 'host.example.com'


def test_resulting_path_stays_inside_destination(tmp_path):
    dst = build_destination_dir(tmp_path, 'host.example.com', '/var/log')
    assert tmp_path.resolve() in dst.parents


def test_hostname_traversal_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, '../../../../etc/cron.d', '/evil')


def test_hostname_dotdot_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, '..', '/x')


def test_hostname_with_slash_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, 'a/b', '/x')


def test_empty_hostname_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, '', '/x')


def test_hostname_with_null_byte_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, 'host\x00', '/x')


def test_directory_dotdot_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, 'host', '/..')


def test_directory_with_null_byte_is_rejected(tmp_path):
    with raises(ProtocolError):
        build_destination_dir(tmp_path, 'host', '/var\x00/log')


def test_non_string_hostname_is_rejected(tmp_path):
    for bad_hostname in (123, ['host'], {'h': 1}, None):
        with raises(ProtocolError):
            build_destination_dir(tmp_path, bad_hostname, '/x')


def test_non_string_directory_is_rejected(tmp_path):
    for bad_directory in (456, ['/x'], {'p': 1}, None):
        with raises(ProtocolError):
            build_destination_dir(tmp_path, 'host', bad_directory)


def test_traversal_does_not_escape_destination(tmp_path):
    # Even a crafted combination must never resolve outside the destination dir.
    base = tmp_path.resolve()
    for hostname, directory in [
        ('../../tmp', '/evil'),
        ('host', '/../../etc'),
    ]:
        try:
            dst = build_destination_dir(tmp_path, hostname, directory)
        except ProtocolError:
            continue
        assert base in dst.parents or dst == base / hostname
