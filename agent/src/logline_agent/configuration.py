from logging import getLogger
import os
from pathlib import Path
import re

import yaml


logger = getLogger(__name__)


class ConfigurationError (Exception):
    '''
    This exception means that some user input is missing or invalid.
    '''


class Configuration:
    '''
    Logline Agent configuration
    '''

    default_port = 5645

    def __init__(self, args):
        if args.conf:
            cfg_path = Path(args.conf)
        elif os.environ.get('CONF_FILE'):
            cfg_path = Path(os.environ['CONF_FILE'])
        else:
            cfg_path = None

        if cfg_path:
            cfg_dir = cfg_path.parent
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        else:
            cfg_dir = None
            cfg = {}

        if args.log:
            self.log_file = Path(args.log)
        elif cfg.get('log', {}).get('file'):
            self.log_file = cfg_dir / cfg['log']['file']
        else:
            self.log_file = None

        self.scan_globs = []
        if args.scan:
            self.scan_globs.extend(args.scan)
        if cfg.get('scan'):
            assert isinstance(cfg['scan'], list)
            self.scan_globs.extend(cfg['scan'])
        if not self.scan_globs:
            raise ConfigurationError('No log sources were configured')

        self.exclude_globs = list(cfg.get('exclude') or [])
        self.exclude_if_file_present = list(cfg.get('exclude_if_file_present') or [])

        if args.server:
            self.server_host, self.server_port = parse_address(args.server)
        elif cfg.get('server'):
            self.server_host, self.server_port = parse_address(cfg['server'])
        else:
            raise ConfigurationError('No server address configured')

        if args.tls_cert:
            self.tls_cert_file = Path(args.tls_cert)
        elif cfg.get('tls', {}).get('cert'):
            self.tls_cert_file = cfg_dir / cfg['tls']['cert']
        else:
            self.tls_cert_file = None

        if self.tls_cert_file and not self.tls_cert_file.is_file():
            raise ConfigurationError(f'TLS cert is not a file: {self.tls_cert_file}')

        self.use_tls = bool(
            args.tls
            or self.tls_cert_file
            or cfg.get('tls', {}).get('enable')
            or cfg.get('tls', {}).get('enabled'))

        if args.token_file:
            self.client_token = Path(args.token_file).read_text().strip()
        elif os.environ.get('CLIENT_TOKEN'):
            self.client_token = os.environ['CLIENT_TOKEN']
        elif cfg.get('client_token'):
            self.client_token = cfg['client_token']
        elif cfg.get('client_token_file'):
            self.client_token = (cfg_dir / cfg['client_token_file']).read_text().strip()
        else:
            raise ConfigurationError('Client token is not configured')

        # File identity: the prefix is the first bytes of a file plus their
        # SHA-256, used by the server to detect rotation. We start streaming as
        # soon as the file has any content; the prefix is whatever is there, up
        # to prefix_size bytes.
        self.prefix_size = 256
        self.min_prefix_size = 1

        # Tuning. Override via the "tuning" section of the YAML config.
        tuning = cfg.get('tuning') or {}
        self.chunk_size = int(tuning.get('chunk_size', 256 * 1024))
        self.window_bytes = int(tuning.get('window_bytes', 4 * 1024 * 1024))
        self.max_frame_size = int(tuning.get('max_frame_size', 4 * 1024 * 1024))
        self.tail_read_interval = float(tuning.get('tail_read_interval', 1))
        self.scan_interval = float(tuning.get('scan_interval', 1))
        self.heartbeat_interval = float(tuning.get('heartbeat_interval', 30))
        self.idle_timeout = float(tuning.get('idle_timeout', 120))
        self.connect_timeout = float(tuning.get('connect_timeout', 30))
        self.reconnect_interval = float(tuning.get('reconnect_interval', 5))
        self.rotated_files_inactivity_threshold = float(tuning.get('rotated_files_inactivity_threshold', 600))

        # Compression of DATA bodies. One of: none, gzip, deflate (all stdlib).
        self.codec = tuning.get('codec', 'gzip')
        if self.codec not in ('none', 'gzip', 'deflate'):
            raise ConfigurationError(f'Unsupported codec: {self.codec!r}')
        self.min_compress_size = int(tuning.get('min_compress_size', 256))


def parse_address(s):
    m = re.match(r'^([^:]+):([0-9]+)$', s)
    if m:
        host, port = m.groups()
        return host, int(port)
    m = re.match(r'^:?([0-9]+)$', s)
    if m:
        port, = m.groups()
        return '', int(port)
    raise ConfigurationError(f'Unknown address format: {s}')
