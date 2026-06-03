from types import SimpleNamespace

from pytest import raises

from logline_server.main import ProtocolError, check_client_auth, sha1_hex


def make_conf(*tokens):
    return SimpleNamespace(client_token_hashes={sha1_hex(t.encode('utf-8')) for t in tokens})


def test_valid_token_is_accepted():
    conf = make_conf('secret')
    # Should not raise.
    check_client_auth(conf, {'client_token': 'secret'})


def test_unknown_token_is_rejected():
    conf = make_conf('secret')
    with raises(Exception):
        check_client_auth(conf, {'client_token': 'wrong'})


def test_missing_auth_is_rejected():
    conf = make_conf('secret')
    with raises(Exception):
        check_client_auth(conf, None)


def test_missing_client_token_is_rejected():
    conf = make_conf('secret')
    with raises(Exception):
        check_client_auth(conf, {})


def test_non_string_client_token_is_rejected():
    # A truthy but non-string client_token must raise ProtocolError instead of
    # crashing on .encode().
    conf = make_conf('secret')
    for bad_token in (123, ['secret'], {'x': 1}):
        with raises(ProtocolError):
            check_client_auth(conf, {'client_token': bad_token})
