from asyncio import StreamReader, run

from pytest import raises

from logline_server.framing import (
    DATA,
    HELLO,
    ConnectionClosed,
    ProtocolError,
    encode_data_payload,
    encode_frame,
    read_frame,
    split_data_payload,
)


def read_one(data, max_frame_size=1 << 20):
    async def go():
        reader = StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await read_frame(reader, max_frame_size)
    return run(go())


def test_frame_round_trip():
    payload = b'{"hello":"world"}'
    frame_type, stream_id, payload_out = read_one(encode_frame(HELLO, 0, payload))
    assert frame_type == HELLO
    assert stream_id == 0
    assert payload_out == payload


def test_empty_payload_round_trip():
    frame_type, stream_id, payload_out = read_one(encode_frame(HELLO, 3, b''))
    assert (frame_type, stream_id, payload_out) == (HELLO, 3, b'')


def test_data_payload_round_trip():
    meta = b'{"offset":0,"codec":"none","raw_size":14}'
    body = b'some log bytes'
    frame_type, stream_id, payload_out = read_one(encode_frame(DATA, 7, encode_data_payload(meta, body)))
    assert frame_type == DATA
    assert stream_id == 7
    assert split_data_payload(payload_out) == (meta, body)


def test_clean_eof_raises_connection_closed():
    with raises(ConnectionClosed):
        read_one(b'')


def test_truncated_header_raises_protocol_error():
    with raises(ProtocolError):
        read_one(b'\x01\x00\x00')  # 3 bytes, the header needs 9


def test_oversized_frame_is_rejected():
    with raises(ProtocolError):
        read_one(encode_frame(HELLO, 0, b'x' * 100), max_frame_size=10)


def test_split_data_payload_rejects_bogus_meta_len():
    with raises(ProtocolError):
        split_data_payload(b'\xff\xff\xff\xff' + b'short')
