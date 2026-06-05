'''
Optional OpenTelemetry metrics for the server.

Metrics are off unless ``metrics.enabled`` is configured. While off this module
is a complete no-op and the OpenTelemetry SDK is never imported, so the base
install stays lean. When on, the SDK is imported lazily and the process exports
delta-temporality metric streams over OTLP to a local collector.

Each process mints a fresh ``service.instance.id`` at startup. Several instances
can share one listening port (``reuse_port``), and the unique id keeps their
streams distinct so the collector can re-aggregate and then drop it.
'''

from logging import getLogger


logger = getLogger(__name__)

SERVICE_NAME = 'logline-server'

# Latency histogram boundaries in seconds: from sub-millisecond LAN round-trips
# up to the socket timeout. Shared by every duration instrument.
_DURATION_BOUNDARIES = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 30, 120, 300]

# Instrument handles, created by init_metrics(); all None while metrics are off.
_connections = None
_active_connections = None
_bytes_received = None
_bytes_written = None
_write_duration = None
_decompress_duration = None
_protocol_errors = None
_auth_failures = None
_renames = None


def init_metrics(conf):
    '''
    Initialise metrics if enabled and return the ``MeterProvider`` (so the caller
    can flush and shut it down), or ``None`` when metrics are off or the SDK is
    not installed.
    '''
    global _connections, _active_connections, _bytes_received, _bytes_written
    global _write_duration, _decompress_duration, _protocol_errors, _auth_failures, _renames
    if not conf.metrics_enabled:
        return None
    import uuid
    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
        from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning('Metrics are enabled but the OpenTelemetry SDK is not installed; '
                       'metrics stay off. Install the "otel" extra to enable them.')
        return None

    attrs = {'service.name': SERVICE_NAME, 'service.instance.id': str(uuid.uuid4())}
    if conf.metrics_host_name:
        attrs['host.name'] = conf.metrics_host_name
    resource = Resource.create(attrs)
    exporter_kwargs = {
        'preferred_temporality': {
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        },
    }
    if conf.metrics_endpoint:
        exporter_kwargs['endpoint'] = conf.metrics_endpoint
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(**exporter_kwargs), export_interval_millis=10_000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter('logline.server')

    _connections = meter.create_counter(
        'logline.server.connections', unit='1', description='Accepted client connections by outcome')
    _active_connections = meter.create_up_down_counter(
        'logline.server.active_connections', unit='1', description='Currently open client connections')
    _bytes_received = meter.create_counter(
        'logline.server.bytes_received_wire', unit='By', description='Bytes received from clients on the wire')
    _bytes_written = meter.create_counter(
        'logline.server.bytes_written', unit='By', description='Decompressed bytes written to disk')
    _write_duration = meter.create_histogram(
        'logline.server.write.duration', unit='s', description='Time to write and flush one data frame',
        explicit_bucket_boundaries_advisory=_DURATION_BOUNDARIES)
    _decompress_duration = meter.create_histogram(
        'logline.server.decompress.duration', unit='s', description='Time to decompress one data frame',
        explicit_bucket_boundaries_advisory=_DURATION_BOUNDARIES)
    _protocol_errors = meter.create_counter(
        'logline.server.protocol_errors', unit='1', description='Protocol violations by type')
    _auth_failures = meter.create_counter(
        'logline.server.auth_failures', unit='1', description='Rejected client authentications')
    _renames = meter.create_counter(
        'logline.server.renames', unit='1', description='Applied rename control frames by outcome')

    logger.info('Metrics enabled, exporting to %s', conf.metrics_endpoint or 'the default OTLP endpoint')
    return provider


def shutdown_metrics(provider):
    '''Flush the last interval and shut the provider down; safe to call with None.'''
    if provider is None:
        return
    try:
        provider.force_flush()
        provider.shutdown()
    except Exception as e:
        logger.warning('Metrics shutdown failed: %r', e)


def record_connection(result):
    if _connections is not None:
        _connections.add(1, {'result': result})


def active_connection_inc():
    if _active_connections is not None:
        _active_connections.add(1)


def active_connection_dec():
    if _active_connections is not None:
        _active_connections.add(-1)


def record_bytes_received(count):
    if _bytes_received is not None and count:
        _bytes_received.add(count)


def record_bytes_written(count):
    if _bytes_written is not None and count:
        _bytes_written.add(count)


def record_write_duration(seconds):
    if _write_duration is not None:
        _write_duration.record(seconds)


def record_decompress_duration(compression, seconds):
    if _decompress_duration is not None:
        _decompress_duration.record(seconds, {'compression': compression})


def record_protocol_error(error_type):
    if _protocol_errors is not None:
        _protocol_errors.add(1, {'error_type': error_type})


def record_auth_failure():
    if _auth_failures is not None:
        _auth_failures.add(1)


def record_rename(result):
    if _renames is not None:
        _renames.add(1, {'result': result})
