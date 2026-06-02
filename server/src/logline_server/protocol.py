'''
msgspec models for the Logline agent-server wire protocol.

These describe the JSON metadata exchanged on the wire (see Protocol.md).
Only the server uses these typed models. The agent is deliberately
stdlib-only -- it must run on Debian 11/12 without a venv, where msgspec is
not available -- and builds the same JSON with the standard library instead.
'''

from typing import Optional

from msgspec import Struct


class Prefix(Struct):
    length: int
    sha1: str


class Auth(Struct):
    client_token: str


class Header(Struct):
    hostname: str
    path: str
    prefix: Prefix
    auth: Auth


class DataMeta(Struct):
    offset: int
    compression: Optional[str] = None


class OkLengthReply(Struct):
    length: int
