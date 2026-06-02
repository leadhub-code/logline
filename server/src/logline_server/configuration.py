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
    Logline Server configuration
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

        if args.bind:
            self.bind_host, self.bind_port = parse_address(args.bind)
        elif cfg.get('bind'):
            self.bind_host, self.bind_port = parse_address(cfg['bind'])
        else:
            self.bind_host, self.bind_port = '', self.default_port

        if args.dest:
            self.destination_directory = Path(args.dest)
        elif cfg.get('dest'):
            self.destination_directory = cfg_dir / cfg['dest']
        else:
            raise ConfigurationError('Destination directory not configured')

        if args.tls_cert:
            self.tls_cert_file = Path(args.tls_cert)
        elif cfg.get('tls', {}).get('cert'):
            self.tls_cert_file = cfg_dir / cfg['tls']['cert']
        else:
            self.tls_cert_file = None

        if args.tls_key:
            self.tls_key_file = Path(args.tls_key)
        elif cfg.get('tls', {}).get('key'):
            self.tls_key_file = cfg_dir / cfg['tls']['key']
        else:
            self.tls_key_file = None

        self.tls_password = None
        if args.tls_key_password_file:
            self.tls_password = Path(args.tls_key_password_file).read_text().strip()
        elif cfg.get('tls', {}).get('key_password_file'):
            self.tls_password = (cfg_dir / cfg['tls']['key_password_file']).read_text().strip()
        elif cfg.get('tls', {}).get('key_password'):
            self.tls_password = cfg['tls']['key_password']
        elif os.environ.get('TLS_KEY_PASSWORD'):
            self.tls_password = os.environ['TLS_KEY_PASSWORD']

        self.use_tls = bool(self.tls_cert_file)

        self.client_token_hashes = set()
        if args.client_token_hash:
            self.client_token_hashes.update(args.client_token_hash)
        if cfg.get('client_token_hashes'):
            assert isinstance(cfg['client_token_hashes'], list)
            self.client_token_hashes.update(cfg['client_token_hashes'])
        if not self.client_token_hashes:
            raise ConfigurationError('No client token hashes configured')

        # Protocol tuning. Sensible defaults; override via the "tuning" section
        # of the YAML config.
        tuning = cfg.get('tuning') or {}
        self.max_frame_size = int(tuning.get('max_frame_size', 4 * 1024 * 1024))
        self.handshake_timeout = float(tuning.get('handshake_timeout', 30))
        self.idle_timeout = float(tuning.get('idle_timeout', 120))
        self.heartbeat_interval = float(tuning.get('heartbeat_interval', 30))
        self.ack_interval = float(tuning.get('ack_interval', 0.5))

        # Durability: fsync each sink before acknowledging its data. Off by
        # default (the OS page cache already survives a process crash); turn it
        # on to also survive an OS/host crash, at some throughput cost.
        self.fsync = bool(getattr(args, 'fsync', False) or cfg.get('fsync', False))

        if self.heartbeat_interval >= self.idle_timeout:
            raise ConfigurationError('heartbeat_interval must be smaller than idle_timeout')


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
