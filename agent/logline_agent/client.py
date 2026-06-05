'''
Client for the Logline Server
'''

from asyncio import open_connection, to_thread, wait_for
from base64 import b64encode
import gzip
from hashlib import sha1
import json
from logging import getLogger
from pathlib import Path
import re
from socket import getfqdn
from ssl import Purpose, create_default_context
from time import monotonic as monotime

from .telemetry import record_bytes_sent, record_connect, record_frame, record_send_duration


logger = getLogger(__name__)

socket_timeout = 300

# Establishing the TCP/TLS connection should be quick; without a timeout a
# silent or unreachable server would make open_connection() hang indefinitely.
connect_timeout = 30


class ClientError (Exception):
    pass


async def connect_to_server(conf, log_path, target, log_prefix):
    '''
    Connect to the server specified in the configuration.

    Initial header is sent to the server, containing some metadata, the source
    ``directory`` (which the server maps to a destination directory), the explicit
    agent-chosen ``target`` filename to write into, and the log file prefix.
    '''
    assert isinstance(log_prefix, bytes)
    assert isinstance(target, str)
    assert isinstance(conf.client_token, str)
    logger.debug('Connecting to %s:%s', conf.server_host, conf.server_port)
    if conf.use_tls:
        logger.debug('Using TLS; cafile: %s', conf.tls_cert_file or '-')
        ssl_context = create_default_context(
            purpose=Purpose.SERVER_AUTH,
            cafile=str(conf.tls_cert_file) if conf.tls_cert_file else None)
    else:
        ssl_context = None
    try:
        reader, writer = await wait_for(
            open_connection(conf.server_host, conf.server_port, ssl=ssl_context),
            timeout=connect_timeout)
    except Exception:
        record_connect('error')
        raise
    cc = ClientConnection(reader, writer)
    await cc.send_header({
        'hostname': getfqdn(),
        'directory': str(Path(log_path).parent),
        'target': target,
        'prefix': {
            'length': len(log_prefix),
            'sha1': sha1_b64(log_prefix),
        },
        'auth': {
            'client_token': conf.client_token,
        },
    })
    assert cc.header_reply
    record_connect('ok')
    return cc


class ClientConnection:
    '''
    Use connect_to_server() to create instance of this class.
    '''

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.header_reply = None

    def close(self):
        self.writer.close()

    async def send_header(self, header):
        self.header_reply = await self._send_command('logline-agent-v1', header)

    async def send_data(self, offset, content):
        assert isinstance(offset, int)
        assert isinstance(content, bytes)
        metadata = {
            'offset': offset,
            'compression': None,
        }
        content_gz = await to_thread(gzip.compress, content)
        if len(content_gz) < len(content):
            metadata['compression'] = 'gzip'
            content = content_gz
        record_bytes_sent(len(content))
        await self._send_command('data', metadata, content)

    async def send_rename(self, src, dst):
        '''
        Ask the server to rename ``src`` to ``dst`` within this connection's
        directory. The frame is idempotent server-side, so it is safe to replay.
        The open fd on the server survives the rename, so appends may continue
        afterwards into the renamed file.
        '''
        assert isinstance(src, str)
        assert isinstance(dst, str)
        await self._send_command('rename', {'from': src, 'to': dst})

    async def _send_command(self, command, metadata, data=None):
        assert isinstance(command, str)
        assert isinstance(metadata, dict)
        md_json = json.dumps(metadata)
        md_json_safe = obfuscate_secrets(md_json)
        md_bytes = md_json.encode()
        md_bytes += b'\n'
        t0 = monotime()
        if data is None:
            logger.debug('Sending: %s %s', command, md_json_safe)
            self.writer.write('{} {}\n'.format(command, len(md_bytes)).encode('ascii'))
            self.writer.write(md_bytes)
        else:
            assert isinstance(data, bytes)
            logger.debug('Sending: %s %s + %d B data', command, md_json_safe, len(data))
            self.writer.write('{} {} {}\n'.format(command, len(md_bytes), len(data)).encode('ascii'))
            self.writer.write(md_bytes)
            self.writer.write(data)
        await wait_for(self.writer.drain(), timeout=socket_timeout)
        reply_line = await wait_for(self.reader.readline(), timeout=socket_timeout)
        #logger.debug('Received reply line %r', reply_line)
        reply_line_parts = reply_line.decode('ascii').split()
        if len(reply_line_parts) == 2:
            reply_status, reply_length = reply_line_parts
            reply_length = int(reply_length)
        else:
            reply_status, = reply_line_parts
            reply_length = 0
        if reply_length:
            reply_json = await wait_for(self.reader.readexactly(reply_length), timeout=socket_timeout)
            reply = json.loads(reply_json.decode('utf-8'))
            del reply_json
        else:
            reply = None
        elapsed = monotime() - t0
        duration_ms = int(elapsed * 1000)
        record_send_duration(elapsed)
        if reply_status == 'ok':
            record_frame('ok')
            logger.debug('Received reply in %d ms: %s %s', duration_ms, reply_status, '-' if reply is None else repr(reply))
            return reply
        elif reply_status == 'error':
            record_frame('error')
            logger.warning('Received reply in %d ms: %s %s', duration_ms, reply_status, '-' if reply is None else repr(reply))
            raise ClientError('Error reply: {}'.format(reply))
        else:
            record_frame('error')
            raise ClientError('Protocol error')


def sha1_b64(data):
    assert isinstance(data, bytes)
    return b64encode(sha1(data).digest()).decode('ascii')


assert sha1_b64(b'hello') == 'qvTGHdzF6KLavt4PO0gs2a6pQ00='


def obfuscate_secrets(json_str):
    assert isinstance(json_str, str)
    json_str = re.sub(r'("client_token":\s+"[^"]{2})([^"]+)([^"]{2}")', r'\1...\3', json_str, flags=re.ASCII)
    return json_str


assert obfuscate_secrets('{"auth": {"client_token": "topsecret"}}') == '{"auth": {"client_token": "to...et"}}'
