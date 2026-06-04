from argparse import ArgumentParser
from asyncio import TimeoutError, run, start_server, to_thread, wait_for
from base64 import b64encode
from functools import partial
import gzip
from hashlib import sha1
from io import SEEK_END
import json
from logging import DEBUG, ERROR, INFO, Formatter, StreamHandler, getLogger
from logging.handlers import WatchedFileHandler
import lzma
import os
from reprlib import repr as smart_repr
from ssl import Purpose, create_default_context

from .configuration import Configuration, ConfigurationError
from .util import decompress_zst


logger = getLogger(__name__)

# How long to wait for a freshly connected client to send its initial command
# before giving up. This guards against connections that occupy a slot without
# ever authenticating (e.g. slowloris-style). Established, authenticated
# connections are intentionally allowed to stay idle for as long as needed,
# since the agent only sends data when the watched log grows.
handshake_timeout = 30


def server_main():
    p = ArgumentParser()
    p.add_argument('--conf', help='path to configuration file')
    p.add_argument('--log', help='path to log file')
    p.add_argument('--verbose', '-v', action='store_true')
    p.add_argument('--bind')
    p.add_argument('--reuse-port', action='store_true',
        help='set SO_REUSEPORT so multiple server instances can listen on the same '
             'address for load balancing by the kernel (where supported)')
    p.add_argument('--dest', help='directory to store the received logs')
    p.add_argument('--tls-cert', help='path to the file with certificate in PEM format')
    p.add_argument('--tls-key', help='path to the file with key in PEM format')
    p.add_argument('--tls-key-password-file', help='path to the file with key password in plaintext')
    p.add_argument('--client-token-hash', action='append')
    args = p.parse_args()
    setup_logging(verbose=args.verbose)
    conf = Configuration(args=args)
    setup_log_file(conf.log_file)
    run(async_main(conf))


log_format = '%(asctime)s [%(process)d] %(name)s %(levelname)5s: %(message)s'

stderr_log_handler = None


def setup_logging(verbose):
    global stderr_log_handler
    h = StreamHandler()
    h.setFormatter(Formatter(log_format))
    h.setLevel(DEBUG if verbose else INFO)
    getLogger('').addHandler(h)
    getLogger('').setLevel(DEBUG)
    stderr_log_handler = h


def setup_log_file(log_file_path):
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
    server = await create_server(conf)
    logger.info('Listening on %s', ' '.join(str(s.getsockname()) for s in server.sockets))
    async with server:
        await server.serve_forever()


async def create_server(conf):
    if conf.use_tls:
        ssl_context = create_default_context(purpose=Purpose.CLIENT_AUTH)
        logger.debug('Using TLS; certfile: %s keyfile: %s', conf.tls_cert_file, conf.tls_key_file)
        ssl_context.load_cert_chain(
            certfile=conf.tls_cert_file,
            keyfile=conf.tls_key_file,
            password=conf.tls_password)
    else:
        ssl_context = None
    if conf.reuse_port:
        try:
            from socket import SO_REUSEPORT  # noqa: F401
        except ImportError:
            raise ConfigurationError('--reuse-port was requested but SO_REUSEPORT is not supported on this platform') from None
    return await start_server(
        partial(handle_client, conf),
        conf.bind_host, conf.bind_port,
        ssl=ssl_context,
        reuse_port=conf.reuse_port)


async def handle_client(conf, reader, writer):
    try:
        addr = writer.get_extra_info('peername')
        logger.info('New client has connected: %s', addr)
        try:
            command, metadata, data = await wait_for(recv_command(reader, first=True), timeout=handshake_timeout)
        except ReceivedHTTPRequestError:
            logger.info('Received like HTTP request')
            await send_http_response(writer)
            return
        except TimeoutError:
            logger.info('Client did not send the initial command within %s s, closing connection', handshake_timeout)
            return
        if command != 'logline-agent-v1' or data:
            raise Exception(f"Protocol error - received {smart_repr(command)} as first command")
        header = metadata
        if not isinstance(header, dict):
            raise ProtocolError(f'Expected a JSON object as header, received {smart_repr(header)}')
        for field in ('hostname', 'path', 'prefix', 'auth'):
            if not header.get(field):
                raise ProtocolError(f'Missing required header field: {field}')

        prefix = header['prefix']
        if not isinstance(prefix, dict):
            raise ProtocolError(f'Expected a JSON object as prefix, received {smart_repr(prefix)}')
        prefix_length = prefix.get('length')
        if not isinstance(prefix_length, int) or isinstance(prefix_length, bool) or prefix_length < 0:
            raise ProtocolError(f'Invalid prefix length: {smart_repr(prefix_length)}')
        prefix_sha1 = prefix.get('sha1')
        if not isinstance(prefix_sha1, str):
            raise ProtocolError(f'Invalid prefix sha1: {smart_repr(prefix_sha1)}')

        auth = header['auth']
        if not isinstance(auth, dict):
            raise ProtocolError(f'Expected a JSON object as auth, received {smart_repr(auth)}')
        check_client_auth(conf, auth)

        dst_path = build_destination_path(
            conf.destination_directory, header['hostname'], header['path'])

        if not dst_path.parent.is_dir():
            if not dst_path.parent.parent.is_dir():
                logger.debug('Creating directory: %s', dst_path.parent.parent)
                dst_path.parent.parent.mkdir()
            logger.debug('Creating directory: %s', dst_path.parent)
            dst_path.parent.mkdir()

        target = header.get('target')
        if not isinstance(target, str) or not _is_safe_path_segment(target):
            raise ProtocolError(f'Invalid target: {smart_repr(target)}')
        await serve_client(conf, reader, writer, dst_path.parent, target, prefix_length)

    except ConnectionClosed:
        logger.info('Client closed connection')
    except Exception as e:
        logger.exception('Failed to handle client: %r', e)
    finally:
        logger.info('Closing connection')
        writer.close()


async def serve_client(conf, reader, writer, dst_dir, target, prefix_length):
    '''
    The agent is the sole authority on file identity and rotation. The server
    only appends to the agent-named ``target`` and renames it when told to; it
    never decides identity from content.
    '''
    dst_path = dst_dir / target
    f = None
    try:
        try:
            f = dst_path.open('rb+')
        except FileNotFoundError:
            f = None
            logger.debug('Target does not exist yet: %s', dst_path)

        # Report the current length and prefix hash; never rotate. The agent
        # decides what to do from this (resume, or seal a stale file first).
        if f is not None:
            f.seek(0)
            f_prefix = f.read(prefix_length)
            prefix_sha1 = sha1_b64(f_prefix) if f_prefix else None
            f.seek(0, SEEK_END)
            length = f.tell()
        else:
            prefix_sha1 = None
            length = 0
        await send_reply(writer, 'ok', {'length': length, 'prefix_sha1': prefix_sha1})

        while True:
            command, metadata, data = await recv_command(reader)
            if command == 'data':
                if f is None:
                    # Lazily create the target on the first append. A new target
                    # has length 0, so the first write must be at integer offset
                    # 0. Validate before creating so a malformed first frame
                    # never leaves a stray empty file behind.
                    if not isinstance(metadata, dict):
                        raise ProtocolError(f"Expected a JSON object as 'data' metadata, received {smart_repr(metadata)}")
                    offset = metadata.get('offset')
                    if not isinstance(offset, int) or isinstance(offset, bool) or offset != 0:
                        raise ProtocolError(
                            'First append to a new target {} must be at offset 0, got {!r}'.format(dst_path, offset))
                    logger.info('Creating new target: %s', dst_path)
                    f = dst_path.open('wb+')
                await apply_data(f, dst_path, metadata, data)
                await send_reply(writer, 'ok', None)
            elif command == 'rename':
                dst_path = handle_rename(dst_dir, dst_path, f, metadata)
                await send_reply(writer, 'ok', None)
            else:
                raise ProtocolError(f"Expected 'data' or 'rename' command, received {smart_repr(command)}")
    finally:
        if f:
            f.close()


async def apply_data(f, dst_path, metadata, data):
    '''Decompress, verify the offset, append and flush a single ``data`` frame.'''
    if not isinstance(data, bytes):
        raise ProtocolError(f"Expected a payload with the 'data' command, received {smart_repr(data)}")
    if not isinstance(metadata, dict):
        raise ProtocolError(f"Expected a JSON object as 'data' metadata, received {smart_repr(metadata)}")
    compression = metadata.get('compression')
    if compression == 'gzip':
        data = await to_thread(gzip.decompress, data)
    elif compression == 'lzma':
        data = await to_thread(lzma.decompress, data)
    elif compression == 'zst':
        data = await decompress_zst(data)
    elif compression is not None:
        raise Exception(f"Unsupported compression method: {compression}")
    offset = metadata.get('offset')
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ProtocolError(f'Invalid data offset: {smart_repr(offset)}')
    if offset != f.tell():
        raise ProtocolError(f'Unexpected data offset: client sent {smart_repr(offset)}, expected {f.tell()}')
    logger.debug('Writing %d bytes at offset %s to file %s (fd: %s)', len(data), f.tell(), dst_path, f.fileno())
    f.write(data)
    f.flush()


def handle_rename(dst_dir, current_path, f, metadata):
    '''
    Apply an in-band ``rename`` control frame within ``dst_dir``.

    The frame is idempotent: if ``from`` is gone but ``to`` already exists the
    rename is treated as already applied (safe for restart/replay). If the open
    fd ``f`` holds the file being renamed it survives the rename (the same inode
    is simply relabelled), so the connection keeps appending afterwards; we
    return the file's new path so the caller's bookkeeping follows it.

    Completed segments are made crash-durable here (and only here): fsync the
    segment file and the parent directory so neither the final bytes nor the
    rename can be lost on a server crash.
    '''
    src = metadata.get('from')
    dst = metadata.get('to')
    if not isinstance(src, str) or not _is_safe_path_segment(src):
        raise ProtocolError(f'Invalid rename "from": {smart_repr(src)}')
    if not isinstance(dst, str) or not _is_safe_path_segment(dst):
        raise ProtocolError(f'Invalid rename "to": {smart_repr(dst)}')
    src_path = dst_dir / src
    new_path = dst_dir / dst
    fd_follows = f is not None and src_path == current_path

    if src_path.exists():
        src_path.rename(new_path)
        logger.info('Renamed %s -> %s', src_path, new_path)
        if fd_follows:
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError as e:
                logger.warning('fsync of %s failed: %r', new_path, e)
    elif new_path.exists():
        logger.info('Rename %s -> %s is a no-op (%s already exists)', src_path, new_path, new_path)
    else:
        raise ProtocolError(
            f'Cannot rename: neither {smart_repr(src)} nor {smart_repr(dst)} exists in {dst_dir}')

    fsync_dir(dst_dir)
    return new_path if fd_follows else current_path


def fsync_dir(path):
    '''fsync a directory so a rename/create within it becomes durable.'''
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning('fsync of directory %s failed: %r', path, e)
    finally:
        os.close(fd)


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
    if not isinstance(hostname, str):
        raise ProtocolError(f'Invalid hostname: {smart_repr(hostname)}')
    if not isinstance(path, str):
        raise ProtocolError(f'Invalid path: {smart_repr(path)}')
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
    client_token = header_auth.get('client_token')
    if client_token:
        if not isinstance(client_token, str):
            raise ProtocolError(f'Invalid client token: {smart_repr(client_token)}')
        ct_bytes = client_token.encode('utf-8')
        if sha1_hex(ct_bytes) in conf.client_token_hashes:
            logger.debug('Client token verified with SHA1 hash %s', sha1_hex(ct_bytes))
            return
        raise Exception(f'Unknown client token; hash: {sha1_hex(ct_bytes)}')
    raise Exception('Client token was not received in header')


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
    return b64encode(sha1(data).digest()).decode('ascii')


assert sha1_b64(b'hello') == 'qvTGHdzF6KLavt4PO0gs2a6pQ00='


def sha1_hex(data):
    return sha1(data).hexdigest()


assert sha1_hex(b'hello') == 'aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d'
