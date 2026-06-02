'''
Server side of one logline/2 connection.

A ServerSession owns a single client connection and multiplexes the streams
on it. Incoming frames are processed by a single read loop; outgoing frames go
through a queue drained by one writer task, so the writer is never touched
concurrently. A periodic task flushes the sinks and sends the cumulative ACKs
that drive the agent's flow control; another sends heartbeats while idle.
'''

from asyncio import CancelledError, Event, Queue, create_task, gather, wait_for
from contextlib import suppress
from logging import getLogger

from msgspec import MsgspecError
from msgspec.json import decode, encode

from .auth import token_is_authorized
from .decompress import decompress
from .framing import (
    ACK,
    CLOSE,
    CONNECTION_STREAM,
    DATA,
    ERROR,
    HEARTBEAT,
    HELLO,
    HELLO_ACK,
    OPEN,
    OPEN_ACK,
    ConnectionClosed,
    ProtocolError,
    encode_frame,
    frame_type_name,
    read_frame,
    split_data_payload,
)
from .paths import build_destination_path
from .protocol import (
    PROTOCOL_VERSION,
    Ack,
    Close,
    DataMeta,
    Error,
    Hello,
    HelloAck,
    Open,
    OpenAck,
)
from .sink import open_sink


logger = getLogger(__name__)


def decode_payload(payload, message_type):
    '''Decode a control-frame JSON payload into a typed model.'''
    try:
        return decode(payload, type=message_type)
    except MsgspecError as e:
        raise ProtocolError(f'Invalid {message_type.__name__} payload: {e}')


class ServerSession:

    def __init__(self, conf, reader, writer):
        self.conf = conf
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info('peername')
        self.hostname = None
        self.sinks = {}            # stream_id -> Sink
        self.acked = {}            # stream_id -> last offset we sent an ACK for
        self.send_queue = Queue()
        self.closing = Event()

    async def run(self):
        logger.info('New connection from %s', self.peer)
        writer_task = create_task(self._writer_loop())
        acker_task = create_task(self._acker_loop())
        heartbeat_task = create_task(self._heartbeat_loop())
        try:
            await self._read_loop()
        except ConnectionClosed:
            logger.info('Connection from %s closed', self.peer)
        except ProtocolError as e:
            logger.warning('Protocol error from %s: %s', self.peer, e)
            self._enqueue_control(ERROR, CONNECTION_STREAM, Error(code='protocol_error', message=str(e)))
        except TimeoutError:
            logger.info('Connection from %s timed out', self.peer)
        except Exception as e:
            logger.exception('Session error for %s: %r', self.peer, e)
        finally:
            self.closing.set()
            # Give the writer a moment to flush a final ERROR/ACK, then stop.
            self.send_queue.put_nowait(None)
            with suppress(CancelledError):
                await writer_task
            acker_task.cancel()
            heartbeat_task.cancel()
            with suppress(CancelledError):
                await gather(acker_task, heartbeat_task, return_exceptions=True)
            self._close_all_sinks()
            self.writer.close()

    async def _read_loop(self):
        # The first frame must be a successful HELLO handshake.
        frame_type, _, payload = await wait_for(
            read_frame(self.reader, self.conf.max_frame_size), timeout=self.conf.handshake_timeout)
        if frame_type != HELLO:
            raise ProtocolError(f'Expected HELLO, got {frame_type_name(frame_type)}')
        self._handle_hello(payload)

        while not self.closing.is_set():
            frame_type, stream_id, payload = await wait_for(
                read_frame(self.reader, self.conf.max_frame_size), timeout=self.conf.idle_timeout)
            await self._dispatch(frame_type, stream_id, payload)

    def _handle_hello(self, payload):
        hello = decode_payload(payload, Hello)
        if hello.protocol != PROTOCOL_VERSION:
            raise ProtocolError(f'Unsupported protocol version: {hello.protocol!r}')
        if not token_is_authorized(hello.auth.token, self.conf.client_token_hashes):
            raise ProtocolError('Authentication failed')
        self.hostname = hello.hostname
        logger.info('Authenticated %s as host %r', self.peer, self.hostname)
        self._enqueue_control(HELLO_ACK, CONNECTION_STREAM, HelloAck(
            max_frame_size=self.conf.max_frame_size,
            ack_interval=self.conf.ack_interval))

    async def _dispatch(self, frame_type, stream_id, payload):
        if frame_type == DATA:
            await self._handle_data(stream_id, payload)
        elif frame_type == OPEN:
            self._handle_open(stream_id, payload)
        elif frame_type == HEARTBEAT:
            pass  # liveness only; receiving any frame already refreshes the read timeout
        elif frame_type == CLOSE:
            self._handle_close(stream_id, payload)
        else:
            raise ProtocolError(f'Unexpected frame: {frame_type_name(frame_type)}')

    def _handle_open(self, stream_id, payload):
        if stream_id == CONNECTION_STREAM:
            raise ProtocolError('OPEN must use a non-zero stream id')
        open_msg = decode_payload(payload, Open)
        dst_path = build_destination_path(self.conf.destination_directory, self.hostname, open_msg.path)
        if stream_id in self.sinks:
            self.sinks[stream_id].close()
        sink = open_sink(dst_path, open_msg.prefix.size, open_msg.prefix.sha256, self.conf.fsync)
        self.sinks[stream_id] = sink
        self.acked[stream_id] = sink.offset
        self._enqueue_control(OPEN_ACK, stream_id, OpenAck(offset=sink.offset))

    async def _handle_data(self, stream_id, payload):
        sink = self.sinks.get(stream_id)
        if sink is None:
            raise ProtocolError(f'DATA for unopened stream {stream_id}')
        meta_bytes, body = split_data_payload(payload)
        meta = decode_payload(meta_bytes, DataMeta)
        data = await decompress(meta.codec, body, meta.raw_size)
        sink.write(meta.offset, data)

    def _handle_close(self, stream_id, payload):
        reason = decode_payload(payload, Close).reason if payload else ''
        if stream_id == CONNECTION_STREAM:
            logger.info('Client %s closing connection: %s', self.peer, reason or '-')
            self.closing.set()
            return
        logger.info('Closing stream %d: %s', stream_id, reason or '-')
        sink = self.sinks.pop(stream_id, None)
        self.acked.pop(stream_id, None)
        if sink is not None:
            self._ack_stream(stream_id, sink)  # final ACK before the file goes away
            sink.close()

    async def _acker_loop(self):
        while not self.closing.is_set():
            await self._sleep(self.conf.ack_interval)
            for stream_id, sink in list(self.sinks.items()):
                self._ack_stream(stream_id, sink)

    def _ack_stream(self, stream_id, sink):
        if sink.offset == self.acked.get(stream_id):
            return
        sink.sync()  # only ACK what is durably stored (fsync if enabled)
        self.acked[stream_id] = sink.offset
        self._enqueue_control(ACK, stream_id, Ack(offset=sink.offset))

    async def _heartbeat_loop(self):
        while not self.closing.is_set():
            await self._sleep(self.conf.heartbeat_interval)
            self._enqueue_control(HEARTBEAT, CONNECTION_STREAM, None)

    async def _writer_loop(self):
        try:
            while True:
                frame = await self.send_queue.get()
                if frame is None:
                    break
                self.writer.write(frame)
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            logger.info('Write to %s failed: connection gone', self.peer)
            self.closing.set()

    async def _sleep(self, seconds):
        # Sleep, but wake up immediately when the session starts closing.
        with suppress(TimeoutError):
            await wait_for(self.closing.wait(), timeout=seconds)

    def _enqueue_control(self, frame_type, stream_id, payload_obj):
        payload = encode(payload_obj) if payload_obj is not None else b''
        self.send_queue.put_nowait(encode_frame(frame_type, stream_id, payload))

    def _close_all_sinks(self):
        for sink in self.sinks.values():
            with suppress(Exception):
                sink.close()
        self.sinks.clear()
