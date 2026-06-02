from argparse import ArgumentParser
from asyncio import Event, get_running_loop, run, wait_for
from contextlib import suppress
from logging import DEBUG, ERROR, INFO, Formatter, StreamHandler, getLogger
from logging.handlers import WatchedFileHandler
from signal import SIGINT, SIGTERM

from .configuration import Configuration
from .framing import ConnectionClosed, ProtocolError
from .session import AgentSession


logger = getLogger(__name__)


def get_argument_parser():
    p = ArgumentParser()
    p.add_argument('--conf', help='path to configuration file')
    p.add_argument('--log', help='path to log file')
    p.add_argument('--verbose', '-v', action='store_true')
    p.add_argument('--scan', action='append')
    p.add_argument('--server')
    p.add_argument('--tls', action='store_true')
    p.add_argument('--tls-cert', help='path to the file with certificate in PEM format')
    p.add_argument('--token-file', help='path to the file containing client token')
    return p


def agent_main():
    args = get_argument_parser().parse_args()
    setup_logging(verbose=args.verbose)
    conf = Configuration(args=args)
    setup_log_file(conf.log_file)
    logger.info('Logline Agent starting')
    try:
        run(async_main(conf))
    except KeyboardInterrupt:
        logger.info('Logline Agent interrupted')
    else:
        logger.info('Logline Agent stopped')


log_format = '%(asctime)s [%(process)d] %(name)-20s %(levelname)5s: %(message)s'

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
    shutdown = Event()
    loop = get_running_loop()
    for sig in (SIGTERM, SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)

    while not shutdown.is_set():
        session = AgentSession(conf, shutdown)
        try:
            await session.run()
        except (OSError, ConnectionClosed, ProtocolError, TimeoutError) as e:
            logger.warning('Connection to server failed: %r', e)
        except Exception as e:
            logger.exception('Session crashed: %r', e)
        if shutdown.is_set():
            break
        logger.info('Reconnecting in %.1f s', conf.reconnect_interval)
        with suppress(TimeoutError):
            await wait_for(shutdown.wait(), timeout=conf.reconnect_interval)
