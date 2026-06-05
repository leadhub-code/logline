from types import SimpleNamespace

import pytest

from logline_agent import telemetry


def test_disabled_returns_none_and_helpers_are_noops():
    conf = SimpleNamespace(metrics_enabled=False, metrics_endpoint=None, metrics_host_name=None)
    assert telemetry.init_metrics(conf) is None
    telemetry.record_bytes_read(100)
    telemetry.record_bytes_sent(50)
    telemetry.record_send_duration(0.01)
    telemetry.record_frame('ok')
    telemetry.record_connect('error')
    telemetry.record_rotation()
    # The observable-gauge sources stay unregistered while disabled.
    telemetry.set_files_watched_source(lambda: 7)
    telemetry.register_lag_source('seg', lambda: 42)
    assert telemetry._files_watched_source is None
    assert 'seg' not in telemetry._lag_sources
    telemetry.unregister_lag_source('seg')
    telemetry.shutdown_metrics(None)


def test_enabled_builds_provider_and_observes(monkeypatch):
    pytest.importorskip('opentelemetry')
    # Cap the OTLP timeout so the shutdown flush against a dead endpoint is quick.
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TIMEOUT', '1')
    conf = SimpleNamespace(
        metrics_enabled=True, metrics_endpoint='http://localhost:4317', metrics_host_name='host.example.com')
    provider = telemetry.init_metrics(conf)
    assert provider is not None
    try:
        telemetry.set_files_watched_source(lambda: 3)
        telemetry.register_lag_source('seg-a', lambda: 10)
        telemetry.register_lag_source('seg-b', lambda: 25)
        # The shipping-lag gauge reports a single aggregate (the max) across files.
        from opentelemetry.metrics import CallbackOptions
        observations = telemetry._observe_shipping_lag(CallbackOptions())
        assert [o.value for o in observations] == [25]
        watched = telemetry._observe_files_watched(CallbackOptions())
        assert [o.value for o in watched] == [3]
        telemetry.record_bytes_read(2048)
        telemetry.record_frame('ok')
        telemetry.record_connect('ok')
        telemetry.record_rotation()
    finally:
        telemetry.unregister_lag_source('seg-a')
        telemetry.unregister_lag_source('seg-b')
        telemetry.shutdown_metrics(provider)
