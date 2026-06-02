'''
Agent side of one logline/2 connection.

A single connection carries every watched file as its own stream. A scanner
task discovers files and starts a tail task for each; each tail task opens a
stream, then reads and ships data without waiting for a reply to every frame.
The server's cumulative ACKs drive a per-stream in-flight window so a fast log
cannot run unbounded ahead of what the server has stored. A reader task
dispatches ACK/OPEN_ACK/HEARTBEAT/ERROR frames, a writer task drains the send
queue, and a heartbeat task keeps the connection alive while idle.

Everything here is standard-library only: the agent must run on older Debian
without a venv.
'''

from asyncio import (
    FIRST_COMPLETED,
    Event,
    Future,
    Queue,
    create_task,
    gather,
    open_connection,
    to_thread,
    wait,
    wait_for,
)
from contextlib import suppress
import gzip
import json
from logging import getLogger
from os import fstat
from socket import getfqdn
from ssl import Purpose, create_default_context
from time import monotonic as monotime
import zlib

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
    encode_data_payload,
    encode_frame,
    frame_type_name,
    read_frame,
)
from .tailer import iter_log_files, sha256_hex


logger = getLogger(__name__)

PROTOCOL = 'logline/2'


class Stream:
    '''Agent-side state for one tailed file multiplexed on the connection.'''

    def __init__(self, stream_id, path):
        self.stream_id = stream_id
        self.path = path
        self.open_ack = Future()   # resolved with the server's start offset
        self.sent_offset = 0
        self.acked_offset = 0
        self.ack_event = Event()   # set whenever acked_offset advances

    def on_ack(self, offset):
        if offset > self.acked_offset:
            self.acked_offset = offset
            self.ack_event.set()


class AgentSession:

    def __init__(self, conf, shutdown):
        self.conf = conf
        self.shutdown = shutdown
        self.reader = None
        self.writer = None
        self.max_frame_size = conf.max_frame_size
        self.send_queue = Queue()
        self.streams = {}        # stream_id -> Stream
        self.tail_tasks = {}     # path str -> task
        self.next_stream_id = 1
        self.closed = Event()
        self._loop_tasks = []

    async def run(self):
        await self._connect()
        self._loop_tasks = [
            create_task(self._writer_loop()),
            create_task(self._reader_loop()),
            create_task(self._scanner_loop()),
            create_task(self._heartbeat_loop()),
        ]
        try:
            await self._wait_for_stop()
        finally:
            await self._teardown()

    # -- connection setup --------------------------------------------------

    async def _connect(self):
        ssl_context = self._make_ssl_context()
        logger.info('Connecting to %s:%s', self.conf.server_host, self.conf.server_port)
        self.reader, self.writer = await wait_for(
            open_connection(self.conf.server_host, self.conf.server_port, ssl=ssl_context),
            timeout=self.conf.connect_timeout)
        hello = {
            'protocol': PROTOCOL,
            'hostname': getfqdn(),
            'auth': {'token': self.conf.client_token},
        }
        self.writer.write(encode_frame(HELLO, CONNECTION_STREAM, json.dumps(hello).encode()))
        await wait_for(self.writer.drain(), timeout=self.conf.connect_timeout)
        frame_type, _, payload = await wait_for(
            read_frame(self.reader, self.max_frame_size), timeout=self.conf.connect_timeout)
        if frame_type == ERROR:
            raise ProtocolError(f'Server rejected connection: {json.loads(payload).get("message")}')
        if frame_type != HELLO_ACK:
            raise ProtocolError(f'Expected HELLO_ACK, got {frame_type_name(frame_type)}')
        ack = json.loads(payload)
        self.max_frame_size = min(self.max_frame_size, int(ack.get('max_frame_size', self.max_frame_size)))
        logger.info('Connected and authenticated to %s:%s', self.conf.server_host, self.conf.server_port)

    def _make_ssl_context(self):
        if not self.conf.use_tls:
            return None
        logger.debug('Using TLS; cafile: %s', self.conf.tls_cert_file or '-')
        return create_default_context(
            purpose=Purpose.SERVER_AUTH,
            cafile=str(self.conf.tls_cert_file) if self.conf.tls_cert_file else None)

    # -- background loops --------------------------------------------------

    async def _writer_loop(self):
        try:
            while True:
                frame = await self.send_queue.get()
                if frame is None:
                    break
                self.writer.write(frame)
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.info('Write failed, connection gone: %r', e)
        finally:
            self.closed.set()

    async def _reader_loop(self):
        try:
            while True:
                frame_type, stream_id, payload = await wait_for(
                    read_frame(self.reader, self.max_frame_size), timeout=self.conf.idle_timeout)
                self._dispatch(frame_type, stream_id, payload)
        except ConnectionClosed:
            logger.info('Server closed the connection')
        except (ProtocolError, OSError) as e:
            logger.warning('Reader stopping: %r', e)
        except TimeoutError:
            logger.warning('No data from server within idle timeout, dropping connection')
        finally:
            self.closed.set()

    def _dispatch(self, frame_type, stream_id, payload):
        if frame_type == ACK:
            stream = self.streams.get(stream_id)
            if stream is not None:
                stream.on_ack(json.loads(payload)['offset'])
        elif frame_type == OPEN_ACK:
            stream = self.streams.get(stream_id)
            if stream is not None and not stream.open_ack.done():
                stream.open_ack.set_result(json.loads(payload)['offset'])
        elif frame_type == HEARTBEAT:
            pass
        elif frame_type == ERROR:
            logger.error('Server error: %s', json.loads(payload))
            self.closed.set()
        elif frame_type == CLOSE:
            logger.info('Server is closing the connection')
            self.closed.set()
        else:
            logger.warning('Unexpected frame from server: %s', frame_type_name(frame_type))

    async def _scanner_loop(self):
        while not self.closed.is_set():
            for path in iter_log_files(self.conf):
                key = str(path)
                task = self.tail_tasks.get(key)
                if task is not None and task.done():
                    self._log_finished_tail(key, task)
                    task = None
                if task is None:
                    self.tail_tasks[key] = create_task(self._tail_file(path))
            await self._sleep(self.conf.scan_interval)

    def _log_finished_tail(self, key, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning('Tail task for %s stopped: %r', key, exc)

    async def _heartbeat_loop(self):
        while not self.closed.is_set():
            await self._sleep(self.conf.heartbeat_interval)
            if not self.closed.is_set():
                self.send_queue.put_nowait(encode_frame(HEARTBEAT, CONNECTION_STREAM, b''))

    # -- per-file tailing --------------------------------------------------

    async def _tail_file(self, path):
        while not self.closed.is_set():
            try:
                f = open(path, 'rb')
            except FileNotFoundError:
                await self._sleep(self.conf.tail_read_interval)
                continue
            try:
                inode = fstat(f.fileno()).st_ino
                prefix = f.read(self.conf.prefix_size)
                if len(prefix) < self.conf.min_prefix_size:
                    await self._sleep(self.conf.tail_read_interval)
                    continue
                stream = self._open_stream(path, prefix)
                try:
                    start_offset = await wait_for(stream.open_ack, timeout=self.conf.idle_timeout)
                    f.seek(start_offset)
                    logger.info('Tailing %s on stream %d from offset %d', path, stream.stream_id, start_offset)
                    await self._pump_file(path, f, inode, stream)
                finally:
                    self._close_stream(stream)
            finally:
                f.close()

    async def _pump_file(self, path, f, inode, stream):
        '''Ship data from f until the file is rotated, truncated, or we stop.'''
        last_active = monotime()
        while not self.closed.is_set():
            await self._wait_for_window(stream)
            if self.closed.is_set():
                return
            chunk = f.read(self.conf.chunk_size)
            if chunk:
                await self._send_data(stream, chunk)
                last_active = monotime()
                continue
            if self._rotated_or_truncated(path, f, inode):
                return
            if monotime() - last_active > self.conf.rotated_files_inactivity_threshold and not path.exists():
                logger.info('File gone and idle, dropping stream %d (%s)', stream.stream_id, path)
                return
            await self._sleep(self.conf.tail_read_interval)

    async def _wait_for_window(self, stream):
        while not self.closed.is_set():
            if stream.sent_offset - stream.acked_offset < self.conf.window_bytes:
                return
            stream.ack_event.clear()
            if stream.sent_offset - stream.acked_offset < self.conf.window_bytes:
                return
            try:
                await wait_for(stream.ack_event.wait(), timeout=self.conf.idle_timeout)
            except TimeoutError:
                raise ProtocolError(f'No ACK for stream {stream.stream_id} within idle timeout')

    def _rotated_or_truncated(self, path, f, inode):
        try:
            current_inode = path.stat().st_ino
        except FileNotFoundError:
            return False  # gone for now; handled by the inactivity check
        if current_inode != inode:
            logger.info('File rotated (inode %s -> %s): %s', inode, current_inode, path)
            return True
        if fstat(f.fileno()).st_size < f.tell():
            logger.info('File truncated: %s', path)
            return True
        return False

    async def _send_data(self, stream, chunk):
        offset = stream.sent_offset
        codec, body = await self._compress(chunk)
        meta = {'offset': offset, 'codec': codec, 'raw_size': len(chunk)}
        payload = encode_data_payload(json.dumps(meta).encode(), body)
        self.send_queue.put_nowait(encode_frame(DATA, stream.stream_id, payload))
        stream.sent_offset = offset + len(chunk)

    async def _compress(self, chunk):
        if self.conf.codec == 'none' or len(chunk) < self.conf.min_compress_size:
            return 'none', chunk
        if self.conf.codec == 'gzip':
            compressed = await to_thread(gzip.compress, chunk)
        else:
            compressed = await to_thread(zlib.compress, chunk)
        if len(compressed) < len(chunk):
            return self.conf.codec, compressed
        return 'none', chunk

    def _open_stream(self, path, prefix):
        stream = Stream(self.next_stream_id, path)
        self.next_stream_id += 1
        self.streams[stream.stream_id] = stream
        payload = {'path': str(path), 'prefix': {'size': len(prefix), 'sha256': sha256_hex(prefix)}}
        self.send_queue.put_nowait(encode_frame(OPEN, stream.stream_id, json.dumps(payload).encode()))
        return stream

    def _close_stream(self, stream):
        self.streams.pop(stream.stream_id, None)
        if not self.closed.is_set():
            payload = json.dumps({'reason': 'stream closed'}).encode()
            self.send_queue.put_nowait(encode_frame(CLOSE, stream.stream_id, payload))

    # -- lifecycle ---------------------------------------------------------

    async def _wait_for_stop(self):
        waiters = [create_task(self.shutdown.wait()), create_task(self.closed.wait())]
        try:
            await wait(waiters, return_when=FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def _teardown(self):
        graceful = self.shutdown.is_set() and not self.closed.is_set()
        if graceful:
            with suppress(Exception):
                self.writer.write(encode_frame(CLOSE, CONNECTION_STREAM, json.dumps({'reason': 'shutting down'}).encode()))
                await wait_for(self.writer.drain(), timeout=5)
        self.closed.set()
        tasks = self._loop_tasks + [t for t in self.tail_tasks.values() if t is not None]
        for t in tasks:
            t.cancel()
        self.send_queue.put_nowait(None)
        with suppress(Exception):
            await gather(*tasks, return_exceptions=True)
        self.writer.close()
        with suppress(Exception):
            await self.writer.wait_closed()

    async def _sleep(self, seconds):
        # Sleep, but wake immediately once the connection is closing.
        with suppress(TimeoutError):
            await wait_for(self.closed.wait(), timeout=seconds)
