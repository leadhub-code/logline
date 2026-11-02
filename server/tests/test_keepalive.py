'''
The server enables aggressive TCP keepalive on accepted connections, so a
half-open connection (the agent host vanished without a FIN/RST) is detected
and closed by the kernel instead of holding a slot and a file open forever.
'''

from asyncio import Event, open_connection, run, start_server, wait_for
import json
import socket
import sys

import pytest

from logline_server.main import handle_client, sha1_b64, sha1_hex


CLIENT_TOKEN = 'secret'


def hello_frame():
    header = json.dumps({
        'hostname': 'host.example.com',
        'directory': '/var/log',
        'target': 'app.log',
        'prefix': {'length': 4, 'sha1': sha1_b64(b'2026')},
        'auth': {'client_token': CLIENT_TOKEN},
    }).encode('utf-8')
    return f'logline-agent-v1 {len(header)}\n'.encode('ascii') + header


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='keepalive tuning options are Linux-specific')
def test_accepted_connection_has_keepalive(tmp_path):
    from types import SimpleNamespace
    conf = SimpleNamespace(destination_directory=tmp_path, client_token_hashes={sha1_hex(CLIENT_TOKEN.encode('utf-8'))})

    async def scenario():
        server_sockets = []
        accepted = Event()

        async def spy(reader, writer):
            server_sockets.append(writer.get_extra_info('socket'))
            accepted.set()
            await handle_client(conf, reader, writer)

        server = await start_server(spy, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await open_connection('127.0.0.1', port)
            writer.write(hello_frame())
            await writer.drain()
            # the handshake reply proves handle_client is past its setup
            assert (await wait_for(reader.readline(), 5)).startswith(b'ok ')
            await wait_for(accepted.wait(), 5)
            sock = server_sockets[0]
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 30
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())
