'''
msgspec models for the logline/2 control-frame payloads (see Protocol.md).

Only the server uses these typed models. The agent is deliberately
stdlib-only -- it must run on Debian 11/12 without a venv, where msgspec is
not available -- and builds the same JSON with the standard library instead.
'''

from msgspec import Struct


PROTOCOL_VERSION = 'logline/2'


class Auth(Struct):
    token: str


class Hello(Struct):
    protocol: str
    hostname: str
    auth: Auth


class HelloAck(Struct):
    max_frame_size: int
    ack_interval: float


class Prefix(Struct):
    size: int
    sha256: str


class Open(Struct):
    path: str
    prefix: Prefix


class OpenAck(Struct):
    offset: int


class DataMeta(Struct):
    offset: int
    codec: str
    raw_size: int


class Ack(Struct):
    offset: int


class Close(Struct):
    reason: str = ''


class Error(Struct):
    code: str
    message: str
