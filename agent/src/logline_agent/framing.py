'''
Binary framing for the logline/2 wire protocol (see Protocol.md).

Every frame is a 9-byte big-endian header -- type (u8), stream_id (u32),
payload length (u32) -- followed by the payload. Control frames carry a JSON
payload; DATA frames carry a self-describing binary payload.

This module is stdlib-only on purpose: the agent must run on older Debian
without a venv. An identical copy lives in the server package.
'''

from asyncio import IncompleteReadError
from struct import Struct


# Connection-level frames use stream 0.
CONNECTION_STREAM = 0

# Frame types.
HELLO = 1
HELLO_ACK = 2
OPEN = 3
OPEN_ACK = 4
DATA = 5
ACK = 6
HEARTBEAT = 7
CLOSE = 8
ERROR = 9

FRAME_TYPE_NAMES = {
    HELLO: 'HELLO', HELLO_ACK: 'HELLO_ACK', OPEN: 'OPEN', OPEN_ACK: 'OPEN_ACK',
    DATA: 'DATA', ACK: 'ACK', HEARTBEAT: 'HEARTBEAT', CLOSE: 'CLOSE', ERROR: 'ERROR',
}

_HEADER = Struct('>BII')
HEADER_SIZE = _HEADER.size
_META_LEN = Struct('>I')


class ProtocolError (Exception):
    '''The peer sent something that violates the protocol.'''


class ConnectionClosed (Exception):
    '''The peer closed the connection cleanly on a frame boundary.'''


def frame_type_name(frame_type):
    return FRAME_TYPE_NAMES.get(frame_type, f'UNKNOWN({frame_type})')


def encode_frame(frame_type, stream_id, payload):
    '''Encode a single frame (header + payload) to bytes.'''
    assert isinstance(payload, (bytes, bytearray))
    return _HEADER.pack(frame_type, stream_id, len(payload)) + bytes(payload)


async def read_frame(reader, max_frame_size):
    '''
    Read one frame from an asyncio StreamReader.

    Returns (frame_type, stream_id, payload). Raises ConnectionClosed on a
    clean EOF at a frame boundary, and ProtocolError on a truncated or
    oversized frame.
    '''
    try:
        header = await reader.readexactly(HEADER_SIZE)
    except IncompleteReadError as e:
        if not e.partial:
            raise ConnectionClosed()
        raise ProtocolError('Truncated frame header')
    frame_type, stream_id, payload_len = _HEADER.unpack(header)
    if payload_len > max_frame_size:
        raise ProtocolError(f'Frame payload too large: {payload_len} > {max_frame_size}')
    if not payload_len:
        return frame_type, stream_id, b''
    try:
        payload = await reader.readexactly(payload_len)
    except IncompleteReadError:
        raise ProtocolError('Truncated frame payload')
    return frame_type, stream_id, payload


def encode_data_payload(meta_bytes, body):
    '''Build a DATA payload: [meta_len u32][meta JSON][body].'''
    assert isinstance(meta_bytes, (bytes, bytearray))
    assert isinstance(body, (bytes, bytearray))
    return _META_LEN.pack(len(meta_bytes)) + bytes(meta_bytes) + bytes(body)


def split_data_payload(payload):
    '''Split a DATA payload into (meta_bytes, body).'''
    if len(payload) < _META_LEN.size:
        raise ProtocolError('Truncated DATA payload')
    (meta_len,) = _META_LEN.unpack_from(payload, 0)
    start = _META_LEN.size
    end = start + meta_len
    if end > len(payload):
        raise ProtocolError('DATA metadata length exceeds payload')
    return payload[start:end], payload[end:]
