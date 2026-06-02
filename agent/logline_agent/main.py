from argparse import ArgumentParser
from asyncio import create_task, run, sleep
from functools import partial
from glob import glob
from logging import DEBUG, ERROR, INFO, Formatter, StreamHandler, getLogger
from logging.handlers import WatchedFileHandler
from pathlib import Path

from .client import connect_to_server
from .configuration import Configuration
from .coordinator import PathCoordinator


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
    except Exception as e:
        logger.exception('Logline Agent failed: %r', e)
    except BaseException as e:
        logger.info('Logline Agent stopping: %r', e)
    else:
        logger.info('Logline Agent done')


log_format = '%(asctime)s [%(process)d] %(name)-20s %(levelname)5s: %(message)s'

own_log_files = set()

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
    own_log_files.add(Path(log_file_path).resolve())
    if stderr_log_handler:
        # decrease stderr handler level since we are logging into file instead
        if stderr_log_handler.level == INFO:
            stderr_log_handler.setLevel(ERROR)


async def async_main(conf):
    watched_paths = {}
    assert conf.server_host
    assert conf.server_port
    client_factory = partial(connect_to_server, conf=conf)
    while True:
        for p in iter_files(conf):
            p_task = watched_paths.get(str(p))
            if p_task and p_task.done():
                logger.warning('Task for path %s is not running; task.exception: %r', p, p_task.exception())
                p_task = None
            if p_task is None:
                #logger.debug('Found out new path %s from glob %s', p, glob_str)
                watched_paths[str(p)] = create_task(watch_path(conf, p, client_factory))

        await sleep(conf.scan_new_files_interval)


def iter_files(conf):
    paths = set()
    for glob_str in conf.scan_globs:
        for p in glob(glob_str, recursive=True):
            if any((Path(p).parent / filename).exists() for filename in conf.exclude_if_file_present):
                continue
            paths.add(Path(p).resolve())
    for glob_str in conf.exclude_globs:
        for p in glob(glob_str, recursive=True):
            paths.discard(Path(p).resolve())
    return sorted(paths)


async def watch_path(conf, file_path, client_factory):
    '''
    Follow one tailed path. All segment connections (the live segment and, during
    a rotation, the closing one) are owned by a per-path coordinator that is the
    sole authority on file identity and rotation; see ``coordinator.py``.
    '''
    assert file_path == file_path.resolve()
    coordinator = PathCoordinator(
        conf, file_path, client_factory, is_own_log=file_path in own_log_files)
    await coordinator.run()
