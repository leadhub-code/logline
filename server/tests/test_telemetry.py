from types import SimpleNamespace

import pytest

from logline_server import telemetry


def test_disabled_returns_none_and_helpers_are_noops():
    conf = SimpleNamespace(metrics_enabled=False, metrics_endpoint=None, metrics_host_name=None)
    assert telemetry.init_metrics(conf) is None
    # None of the call-site helpers should do anything (or raise) while disabled.
    telemetry.record_connection('ok')
    telemetry.active_connection_inc()
    telemetry.active_connection_dec()
    telemetry.record_bytes_received(123)
    telemetry.record_bytes_written(123)
    telemetry.record_write_duration(0.01)
    telemetry.record_decompress_duration('gzip', 0.01)
    telemetry.record_protocol_error('offset_mismatch')
    telemetry.record_auth_failure()
    telemetry.record_rename('ok')
    telemetry.shutdown_metrics(None)


def test_enabled_builds_provider_and_records(monkeypatch):
    pytest.importorskip('opentelemetry')
    # Cap the OTLP timeout so the shutdown flush against a dead endpoint is quick.
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TIMEOUT', '1')
    conf = SimpleNamespace(metrics_enabled=True, metrics_endpoint='http://localhost:4317', metrics_host_name=None)
    provider = telemetry.init_metrics(conf)
    assert provider is not None
    try:
        telemetry.record_connection('ok')
        telemetry.active_connection_inc()
        telemetry.active_connection_dec()
        telemetry.record_bytes_written(4096)
        telemetry.record_write_duration(0.002)
        telemetry.record_decompress_duration('gzip', 0.001)
        telemetry.record_protocol_error('offset_mismatch')
        telemetry.record_rename('noop')
    finally:
        telemetry.shutdown_metrics(provider)
