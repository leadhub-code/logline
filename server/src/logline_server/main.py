from argparse import ArgumentParser
from asyncio import CancelledError, Event, create_task, get_running_loop, run, start_server
from contextlib import suppress
from logging import DEBUG, ERROR, INFO, Formatter, StreamHandler, getLogger
from logging.handlers import WatchedFileHandler
from signal import SIGINT, SIGTERM
from ssl import Purpose, create_default_context

from .configuration import Configuration
from .session import ServerSession


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
    p.add_argument('--fsync', action='store_true', help='fsync received data before acknowledging it')
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
    if stderr_log_handler and stderr_log_handler.level == INFO:
        # decrease stderr handler level since we are logging into file instead
        stderr_log_handler.setLevel(ERROR)


async def async_main(conf):
    ssl_context = make_ssl_context(conf)
    shutdown = Event()
    loop = get_running_loop()
    for sig in (SIGTERM, SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)

    async def handle(reader, writer):
        await ServerSession(conf, reader, writer).run()

    server = await start_server(handle, conf.bind_host, conf.bind_port, ssl=ssl_context)
    logger.info('Listening on %s', ' '.join(str(s.getsockname()) for s in server.sockets))
    async with server:
        serve_task = create_task(server.serve_forever())
        await shutdown.wait()
        logger.info('Shutdown requested, no longer accepting connections')
        server.close()
        serve_task.cancel()
        with suppress(CancelledError):
            await serve_task


def make_ssl_context(conf):
    if not conf.use_tls:
        return None
    ssl_context = create_default_context(purpose=Purpose.CLIENT_AUTH)
    logger.debug('Using TLS; certfile: %s keyfile: %s', conf.tls_cert_file, conf.tls_key_file)
    ssl_context.load_cert_chain(
        certfile=conf.tls_cert_file,
        keyfile=conf.tls_key_file,
        password=conf.tls_password)
    return ssl_context
