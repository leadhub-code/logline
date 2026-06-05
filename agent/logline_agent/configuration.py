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
    LogLine Agent configuration
    '''

    def __init__(self, args):
        if args.conf:
            cfg_path = Path(args.conf)
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

        self.scan_globs = []
        if args.scan:
            self.scan_globs.extend(args.scan)
        if cfg.get('scan'):
            assert isinstance(cfg['scan'], list)
            self.scan_globs.extend(cfg['scan'])
        logger.debug('scan_globs: %r', self.scan_globs)
        if not self.scan_globs:
            raise ConfigurationError('No log sources were configured')

        self.exclude_globs = []
        if cfg.get('exclude'):
            assert isinstance(cfg['exclude'], list)
            self.exclude_globs.extend(cfg['exclude'])
        logger.debug('exclude_globs: %r', self.exclude_globs)

        self.exclude_if_file_present = []
        if cfg.get('exclude_if_file_present'):
            assert isinstance(cfg['exclude_if_file_present'], list)
            self.exclude_if_file_present.extend(cfg['exclude_if_file_present'])
        logger.debug('exclude_if_file_present: %r', self.exclude_if_file_present)

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
            raise ConfigurationError('TLS cert is not a file: {}'.format(self.tls_cert_file))

        self.use_tls = args.tls \
            or self.tls_cert_file \
            or cfg.get('tls', {}).get('enable') \
            or cfg.get('tls', {}).get('enabled')

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

        self.prefix_length_bytes = 50
        self.min_prefix_length_bytes = 20

        self.tail_read_interval_seconds = 1
        self.scan_new_files_interval_seconds = 1
        self.rotated_files_inactivity_threshold_seconds = 600

        # After a new inode is detected, how long to wait for lh-logrotate's
        # `.lh-logrotate-waiting` marker before treating the rotation as an
        # orphan (markerless). lh-logrotate writes the marker as it rotates, so
        # the real wait is usually ~0; this is headroom for scan jitter. It also
        # bounds how long the new live segment is delayed, so keep it small.
        self.seal_marker_grace_seconds = 10

        # Orphan only: how long the closing connection keeps draining with no
        # growth before it gives up and closes. Reuses the rotated-file
        # inactivity knob (a longer value is strictly safer - it only costs a
        # lingering fd, never data).
        self.seal_idle_seconds = self.rotated_files_inactivity_threshold_seconds

        metrics_cfg = cfg.get('metrics') or {}
        self.metrics_enabled = bool(metrics_cfg.get('enabled'))
        if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true':
            self.metrics_enabled = False
        # Endpoint falls back to the standard OTEL_EXPORTER_OTLP_ENDPOINT env var,
        # then (when left as None) to the SDK's own localhost:4317 default.
        self.metrics_endpoint = metrics_cfg.get('endpoint') or os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')
        # host.name is normally supplied by the local OTel collector, whose
        # resource detection fills in (and overrides) the host's DNS name, so we
        # do not set it here. Allow an explicit override for deployments that
        # export without such a collector in front.
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
    raise Exception('Unknown address format: {}'.format(s))
