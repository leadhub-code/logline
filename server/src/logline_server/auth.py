'''
Client authentication for the logline/2 protocol.

The agent sends a shared token; the server stores only the SHA-256 hashes of
the tokens it accepts and compares in constant time.
'''

from hashlib import sha256
from secrets import compare_digest


def hash_token(token):
    '''Return the hex SHA-256 hash of a token string (as stored in config).'''
    return sha256(token.encode('utf-8')).hexdigest()


def token_is_authorized(token, allowed_hashes):
    '''
    Constant-time check that the token's SHA-256 hash is among the allowed
    hashes. Every candidate is compared (no early return) so the running time
    does not reveal which hash matched.
    '''
    token_hash = hash_token(token)
    authorized = False
    for allowed in allowed_hashes:
        if compare_digest(token_hash, allowed):
            authorized = True
    return authorized
