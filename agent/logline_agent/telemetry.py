'''
Optional OpenTelemetry metrics for the agent.

Metrics are off unless ``metrics.enabled`` is configured. While off this module
is a complete no-op and the OpenTelemetry SDK is never imported, so the base
install stays lean. When on, the SDK is imported lazily and the process exports
delta-temporality metric streams over OTLP to a local collector.

``host.name`` is left to the local collector by default (its resource detection
adds the host's name), and is only attached here when explicitly configured.
Several agents may run on one host; a fresh ``service.instance.id`` per process
keeps their streams distinct.
'''

from logging import getLogger


logger = getLogger(__name__)

SERVICE_NAME = 'logline-agent'

# Latency histogram boundaries in seconds: from sub-millisecond LAN round-trips
# up to the socket timeout.
_DURATION_BOUNDARIES = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 30, 120, 300]

# Synchronous instrument handles, created by init_metrics(); None while off.
_bytes_read = None
_bytes_sent = None
_send_duration = None
_frames = None
_connects = None
_rotations = None

# Sources for the observable gauges. ``_files_watched_source`` is a callable
# returning the live tail count; ``_lag_sources`` maps a token to a callable
# returning a segment's current shipping lag in bytes. The gauge callbacks run
# on the SDK's exporter thread while the event loop mutates these; that is
# intentionally lock-free and tolerant of slightly stale reads (a snapshot of
# the dict plus atomic int loads, never garbage).
_enabled = False
_files_watched_source = None
_lag_sources = {}


def init_metrics(conf):
    '''
    Initialise metrics if enabled and return the ``MeterProvider`` (so the caller
    can flush and shut it down), or ``None`` when metrics are off or the SDK is
    not installed.
    '''
    global _enabled, _bytes_read, _bytes_sent, _send_duration, _frames, _connects, _rotations
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
    meter = provider.get_meter('logline.agent')

    _bytes_read = meter.create_counter(
        'logline.agent.bytes_read', unit='By', description='Uncompressed bytes read from tailed files')
    _bytes_sent = meter.create_counter(
        'logline.agent.bytes_sent_wire', unit='By', description='Bytes sent to the server on the wire after compression')
    _send_duration = meter.create_histogram(
        'logline.agent.send.duration', unit='s', description='Per-frame round-trip time to the server',
        explicit_bucket_boundaries_advisory=_DURATION_BOUNDARIES)
    _frames = meter.create_counter(
        'logline.agent.frames', unit='1', description='Frames sent to the server by outcome')
    _connects = meter.create_counter(
        'logline.agent.connects', unit='1', description='Server connection attempts by outcome')
    _rotations = meter.create_counter(
        'logline.agent.rotations', unit='1', description='Detected log file rotations')
    meter.create_observable_gauge(
        'logline.agent.files_watched', callbacks=[_observe_files_watched], unit='1',
        description='Files currently being tailed')
    meter.create_observable_gauge(
        'logline.agent.shipping_lag', callbacks=[_observe_shipping_lag], unit='By',
        description='Maximum bytes still pending shipment across tailed files')

    _enabled = True
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


def _observe_files_watched(options):
    from opentelemetry.metrics import Observation
    if _files_watched_source is None:
        return []
    return [Observation(_files_watched_source())]


def _observe_shipping_lag(options):
    from opentelemetry.metrics import Observation
    # One aggregate across all tailed files (max), never per-file, to keep file
    # paths out of the metric labels.
    lags = [lag for source in list(_lag_sources.values()) if (lag := source()) is not None]
    if not lags:
        return []
    return [Observation(max(lags))]


def set_files_watched_source(source):
    '''Register the callable that returns the current count of tailed files.'''
    global _files_watched_source
    if _enabled:
        _files_watched_source = source


def register_lag_source(token, source):
    '''Register a per-segment callable returning its current shipping lag in bytes.'''
    if _enabled:
        _lag_sources[token] = source


def unregister_lag_source(token):
    _lag_sources.pop(token, None)


def record_bytes_read(count):
    if _bytes_read is not None and count:
        _bytes_read.add(count)


def record_bytes_sent(count):
    if _bytes_sent is not None and count:
        _bytes_sent.add(count)


def record_send_duration(seconds):
    if _send_duration is not None:
        _send_duration.record(seconds)


def record_frame(result):
    if _frames is not None:
        _frames.add(1, {'result': result})


def record_connect(result):
    if _connects is not None:
        _connects.add(1, {'result': result})


def record_rotation():
    if _rotations is not None:
        _rotations.add(1)
