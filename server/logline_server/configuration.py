from logging import getLogger
import os
from pathlib import Path
import re

import yaml


logger = getLogger(__name__)


class ConfigurationError (Exception):
    '''
    This exception means that some user input is missing.
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
            cfg = yaml.safe_load(cfg_path.read_text())
        else:
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

        self.reuse_port = bool(args.reuse_port or cfg.get('reuse_port'))

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

        metrics_cfg = cfg.get('metrics') or {}
        self.metrics_enabled = bool(metrics_cfg.get('enabled'))
        if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
            self.metrics_enabled = False
        # Endpoint falls back to the standard OTEL_EXPORTER_OTLP_ENDPOINT env var,
        # then (when left as None) to the SDK's own localhost:4317 default.
        self.metrics_endpoint = metrics_cfg.get('endpoint') or os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')
        # Optional host.name override. The server normally leaves host.name unset
        # and lets the collector's resource detection supply the real host; set
        # this only when running where that detection is unavailable or wrong
        # (e.g. a container without host networking).
        self.metrics_host_name = (metrics_cfg.get('host_name') or os.environ.get('LOGLINE_HOST_NAME')) if self.metrics_enabled else None


def parse_address(s):
    m = re.match(r'^([^:]+):([0-9]+)$', s)
    if m:
        host, port = m.groups()
        return host, int(port)
    m = re.match(r'^:?([0-9]+)$', s)
    if m:
        port, = m.groups()
        return '', int(port)
    raise Exception(f'Unknown address format: {s}')

