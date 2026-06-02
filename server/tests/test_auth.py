from hashlib import sha256

from logline_server.auth import hash_token, token_is_authorized


def test_authorized_token_is_accepted():
    allowed = {hash_token('correct-token')}
    assert token_is_authorized('correct-token', allowed)


def test_wrong_token_is_rejected():
    allowed = {hash_token('correct-token')}
    assert not token_is_authorized('wrong-token', allowed)


def test_one_of_several_allowed_tokens():
    allowed = {hash_token('a'), hash_token('b'), hash_token('c')}
    assert token_is_authorized('b', allowed)
    assert not token_is_authorized('d', allowed)


def test_empty_allowed_set_rejects_everything():
    assert not token_is_authorized('anything', set())


def test_hash_is_sha256_hex():
    assert hash_token('correct-token') == sha256(b'correct-token').hexdigest()
    assert len(hash_token('x')) == 64  # 32 bytes -> 64 hex chars
