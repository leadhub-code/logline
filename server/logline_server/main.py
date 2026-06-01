from argparse import ArgumentParser
from asyncio import run, sleep, start_server
from base64 import b64encode
from datetime import datetime, timezone
from functools import partial
import gzip
import hashlib
from io import SEEK_END
import json
from logging import getLogger
import lzma
import os
import signal
from time import monotonic as monotime
from reprlib import repr as smart_repr

from .configuration import Configuration
from .util import to_thread, decompress_zst


logger = getLogger(__name__)


def server_main():
    p = ArgumentParser()
    p.add_argument('--conf', help='path to configuration file')
    p.add_argument('--log', help='path to log file')
    p.add_argument('--verbose', '-v', action='store_true')
    p.add_argument('--bind')
    p.add_argument('--dest', help='directory to store the received logs')
    p.add_argument('--tls-cert', help='path to the file with certificate in PEM format')
    p.add_argument('--tls-key', help='path to the file with key in PEM format')
    p.add_argument('--tls-key-password-file', help='path to the file with key password in plaintext')
    p.add_argument('--client-token-hash', action='append')
    p.add_argument('--workers', help="number of worker processes, or 'auto' for one per CPU")
    args = p.parse_args()
    setup_logging(verbose=args.verbose)
    conf = Configuration(args=args)
    setup_log_file(conf.log_file)
    if conf.workers > 1:
        run_workers(conf)
    else:
        run(async_main(conf))


log_format = '%(asctime)s [%(process)d] %(name)s %(levelname)5s: %(message)s'

stderr_log_handler = None


def setup_logging(verbose):
    global stderr_log_handler
    from logging import DEBUG, INFO, getLogger, Formatter, StreamHandler
    h = StreamHandler()
    h.setFormatter(Formatter(log_format))
    h.setLevel(DEBUG if verbose else INFO)
    getLogger('').addHandler(h)
    getLogger('').setLevel(DEBUG)
    stderr_log_handler = h


def setup_log_file(log_file_path):
    from logging import DEBUG, INFO, ERROR, getLogger, Formatter
    from logging.handlers import WatchedFileHandler
    if not log_file_path:
        return
    h = WatchedFileHandler(str(log_file_path))
    h.setFormatter(Formatter(log_format))
    h.setLevel(DEBUG)
    getLogger('').addHandler(h)
    if stderr_log_handler:
        # decrease stderr handler level since we are logging into file instead
        if stderr_log_handler.level == INFO:
            stderr_log_handler.setLevel(ERROR)


async def async_main(conf):
    if conf.use_tls:
        from ssl import create_default_context, Purpose
        ssl_context = create_default_context(purpose=Purpose.CLIENT_AUTH)
        logger.debug('Using TLS; certfile: %s keyfile: %s', conf.tls_cert_file, conf.tls_key_file)
        ssl_context.load_cert_chain(
            certfile=conf.tls_cert_file,
            keyfile=conf.tls_key_file,
            password=conf.tls_password)
    else:
        ssl_context = None
    server = await start_server(
        partial(handle_client, conf),
        conf.bind_host, conf.bind_port,
        ssl=ssl_context,
        # SO_REUSEPORT lets every worker bind the same port; the kernel
        # load-balances connections across them. Only needed (and only
        # portable) in the multi-worker case - keep single-process identical.
        reuse_port=(conf.workers > 1))
    logger.info('Listening on %s', ' '.join(str(s.getsockname()) for s in server.sockets))
    async with server:
        await server.serve_forever()


def run_workers(conf):
    '''
    Fork ``conf.workers`` worker processes, each running its own event loop and
    binding the listening socket with SO_REUSEPORT. The parent stays in the
    foreground and supervises: when a worker dies it is restarted in place, so a
    single worker crash only drops that worker's connections (the agent
    reconnects). SIGTERM/SIGINT are forwarded to the workers for clean shutdown.
    '''
    # rapid-crash guard: if a freshly spawned worker keeps dying immediately
    # (e.g. the bind fails), stop fork-looping and surface the failure.
    min_healthy_uptime = 5.0
    max_rapid_deaths = conf.workers * 3
    rapid_deaths = 0

    children = {}  # pid -> spawn monotime
    shutting_down = False

    def spawn():
        pid = os.fork()
        if pid == 0:
            # child: drop the parent's signal handlers and run the server
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            try:
                run(async_main(conf))
            except KeyboardInterrupt:
                os._exit(0)
            except BaseException as e:
                logger.exception('Worker failed: %r', e)
                os._exit(1)
            else:
                os._exit(0)
        children[pid] = monotime()
        logger.info('Started worker process %d (%d/%d)', pid, len(children), conf.workers)

    def handle_signal(signum, frame):
        nonlocal shutting_down
        shutting_down = True
        logger.info('Received signal %d, stopping %d worker(s)', signum, len(children))
        for pid in list(children):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    for _ in range(conf.workers):
        spawn()

    while children:
        try:
            pid, status = os.wait()
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        started = children.pop(pid, None)
        if shutting_down:
            logger.info('Worker %d exited (status %d)', pid, status)
            continue
        uptime = monotime() - started if started is not None else None
        logger.warning('Worker %d died (status %d, uptime %.1fs), restarting',
                       pid, status, uptime if uptime is not None else -1)
        if uptime is not None and uptime < min_healthy_uptime:
            rapid_deaths += 1
            if rapid_deaths >= max_rapid_deaths:
                logger.error('Too many worker crashes on startup, giving up')
                for p in list(children):
                    try:
                        os.kill(p, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                raise SystemExit(1)
            sleep_before_respawn(1.0)
        spawn()


def sleep_before_respawn(seconds):
    # plain blocking sleep in the supervisor (no event loop here)
    import time
    time.sleep(seconds)


async def handle_client(conf, reader, writer):
    f = None
    try:
        addr = writer.get_extra_info('peername')
        logger.info('New client has connected: %s', addr)
        try:
            command, metadata, data = await recv_command(reader, first=True)
        except ReceivedHTTPRequestError as e:
            logger.info('Received like HTTP request')
            await send_http_response(writer)
            return
        if command != 'logline-agent-v1' or data:
            raise Exception(f"Protocol error - received {smart_repr(command)} as first command")
        header = metadata
        for field in ('hostname', 'path', 'prefix', 'auth'):
            if not header.get(field):
                raise ProtocolError(f"Missing {field!r} in header")

        check_client_auth(conf, header.get('auth'))

        dst_path = build_destination_path(
            conf.destination_directory, header['hostname'], header['path'])

        if not dst_path.parent.is_dir():
            logger.debug('Creating directory: %s', dst_path.parent)
            dst_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            f = dst_path.open('rb+')
        except FileNotFoundError:
            f = None
            logger.debug('File does not exist yet: %s', dst_path)
        else:
            assert f.tell() == 0
            f_prefix = f.read(header['prefix']['length'])
            if f_prefix and sha1_b64(f_prefix) == header['prefix']['sha1']:
                # it's the correct file :)
                logger.info('File has the correct prefix: %s', dst_path)
            else:
                # need to create new file
                logger.info('File has different prefix, rotating: %s', dst_path)
                f.close()
                f = None
                iso_dt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                dst_path.rename(dst_path.with_name(dst_path.name + f".rotated-{iso_dt}"))

        if not f:
            logger.info('Creating new file: %s', dst_path)
            f = dst_path.open('wb+')

        f.seek(0, SEEK_END)
        f_length = f.tell()

        await send_reply(writer, 'ok', {'length': f_length})

        while True:
            command, metadata, data = await recv_command(reader)
            if command != 'data':
                raise ProtocolError(f"Expected 'data' command, received {smart_repr(command)}")
            if not isinstance(data, bytes):
                raise ProtocolError("'data' command without a payload")
            if metadata.get('compression') == 'gzip':
                data = await to_thread(gzip.decompress, data)
            elif metadata.get('compression') == 'lzma':
                data = await to_thread(lzma.decompress, data)
            elif metadata.get('compression') == 'zst':
                data = await decompress_zst(data)
            elif metadata.get('compression') != None:
                raise Exception(f"Unsupported compression method: {metadata['compression']}")
            if f.tell() != metadata.get('offset'):
                raise ProtocolError(
                    'Offset mismatch: file is at {}, client sent {!r}'.format(f.tell(), metadata.get('offset')))
            logger.debug('Writing %d bytes at offset %s to file %s (fd: %s)', len(data), f.tell(), dst_path, f.fileno())
            f.write(data)
            f.flush()
            await send_reply(writer, 'ok', None)

    except ConnectionClosed:
        logger.info('Client closed connection')
    except Exception as e:
        logger.exception('Failed to handle client: %r', e)
    finally:
        logger.info('Closing connection')
        writer.close()
        if f:
            f.close()


async def send_http_response(writer):
    writer.write(b'HTTP/1.0 404 Not Found\r\n')
    writer.write(b'Content-Type: text/plain\r\n')
    writer.write(b'\r\n')
    writer.write(b'This is not a HTTP service.\n')
    await writer.drain()


def _is_safe_path_segment(segment):
    '''
    A safe path segment is a non-empty string that refers to a single
    directory/file entry and cannot be used to traverse the filesystem.
    '''
    if not segment or segment in ('.', '..'):
        return False
    if '/' in segment or '\\' in segment or '\x00' in segment:
        return False
    return True


def build_destination_path(destination_directory, hostname, path):
    '''
    Build the destination file path for the received log file.

    The hostname and path values come from the (authenticated but otherwise
    untrusted) client, so they must never be allowed to escape the configured
    destination directory via path traversal.
    '''
    if not _is_safe_path_segment(hostname):
        raise ProtocolError(f'Invalid hostname: {smart_repr(hostname)}')

    *dir_parts, filename = path.strip('/').split('/')
    if not _is_safe_path_segment(filename):
        raise ProtocolError(f'Invalid path: {smart_repr(path)}')

    # dir_parts are joined with '~' into a single path segment, so any '/' they
    # might contain has already been removed by split('/'); still reject any
    # remaining traversal or null-byte characters defensively.
    mangled_dir = '~'.join(dir_parts)
    if '\x00' in mangled_dir or mangled_dir in ('.', '..'):
        raise ProtocolError(f'Invalid path: {smart_repr(path)}')

    base = destination_directory.resolve()
    dst_path = (base / hostname / mangled_dir / filename) if mangled_dir \
        else (base / hostname / filename)

    # Final defense in depth: make sure the resolved destination really stays
    # inside the configured destination directory.
    resolved_parent = dst_path.parent.resolve()
    if resolved_parent != base and base not in resolved_parent.parents:
        raise ProtocolError(f'Refusing to write outside destination directory: {smart_repr(str(dst_path))}')

    return dst_path


def check_client_auth(conf, header_auth):
    if not header_auth:
        raise Exception('No auth info received in header')
    if header_auth.get('client_token'):
        ct_bytes = header_auth['client_token'].encode('utf-8')
        if sha1_hex(ct_bytes) in conf.client_token_hashes:
            logger.debug('Client token verified with SHA1 hash %s', sha1_hex(ct_bytes))
            return
        raise Exception(f'Unknown client token; hash: {sha1_hex(ct_bytes)}')
    raise Exception(f'Client token was not received in header')


class ConnectionClosed (Exception):
    pass


class ProtocolError (Exception):
    pass


class ReceivedHTTPRequestError (ProtocolError):
    pass


async def recv_command(reader, first=False):
    line = await reader.readline()
    if not line:
        raise ConnectionClosed()
    if first and b'HTTP/' in line:
        raise ReceivedHTTPRequestError(f'Invalid command line format: {smart_repr(line)}')
    try:
        parts = line.decode('ascii').split()
    except UnicodeDecodeError:
        raise ProtocolError(f"Failed to parse command line: {smart_repr(line)}")
    if len(parts) == 1:
        command, = parts
        return command, None, None
    if len(parts) == 2:
        command, metadata_size = parts
        if not metadata_size.isdigit():
            raise ProtocolError(f"Failed to parse command line: {smart_repr(line)}")
        metadata_size = int(metadata_size)
        data_size = None
    elif len(parts) == 3:
        command, metadata_size, data_size = parts
        if not metadata_size.isdigit() or not data_size.isdigit():
            raise ProtocolError(f"Failed to parse command line: {smart_repr(line)}")
        metadata_size = int(metadata_size)
        data_size = int(data_size)
    else:
        raise ProtocolError(f"Failed to parse command line: {smart_repr(line)}")
    metadata_bytes = await reader.readexactly(metadata_size)
    metadata = json.loads(metadata_bytes)
    if data_size is None:
        data = None
    elif data_size == 0:
        data = b''
    else:
        data = await reader.readexactly(data_size)
    if data is None:
        logger.debug('Received %s %r', command, metadata)
    else:
        logger.debug('Received %s %r + %d B data', command, metadata, len(data))
    return command, metadata, data


async def send_reply(writer, status, payload):
    assert isinstance(status, str)
    if payload is None:
        writer.write(f'{status}\n'.encode('ascii'))
        logger.debug('Sent reply %s -', status)
    else:
        payload_bytes = json.dumps(payload).encode('utf-8')
        writer.write(f'{status} {len(payload_bytes)}\n'.encode('ascii'))
        writer.write(payload_bytes)
        logger.debug('Sent reply %s %r', status, payload)
    await writer.drain()


def sha1_b64(data):
    return b64encode(hashlib.sha1(data).digest()).decode('ascii')


assert sha1_b64(b'hello') == 'qvTGHdzF6KLavt4PO0gs2a6pQ00='


def sha1_hex(data):
    return hashlib.sha1(data).hexdigest()


assert sha1_hex(b'hello') == 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'
