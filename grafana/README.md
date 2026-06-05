# Grafana dashboard

`logline-overview.json` is an importable Grafana dashboard for the OpenTelemetry
metrics exported by the agent and the server.

## Import

- Grafana UI: *Dashboards → New → Import → Upload JSON file*, then pick the
  Prometheus datasource when prompted.
- Or provision it from a config-managed dashboards directory.

## Assumptions

The panel queries assume the metrics reach Prometheus through an OpenTelemetry
collector with the usual translation:

- Resource attributes become labels: `service_name`, `service_instance_id`, and
  `host.name` → `host_name`.
- Counters get a `_total` suffix and byte/second units add `_bytes` / `_seconds`
  (the collector's default `add_metric_suffixes`).
- Delta temporality is converted to cumulative before storage.

If your collector is configured differently, adjust the metric names in the
panel `expr` fields.

## Labels and filtering

Every process exports a unique `service_instance_id`; the collector adds
`host_name`. Several agents on one host share a `host_name` but stay distinct by
`service_instance_id`. Server deployments that share a host (each typically a
group of `reuse_port` containers) are told apart by `service_namespace`, set per
container group via `OTEL_RESOURCE_ATTRIBUTES`. The `reuse_port` containers
within a group are given a stable ordinal `service.instance.index` (0, 1, …) so
the dashboard can group by a readable, restart-stable label; the per-process
uuid `service_instance_id` stays as the unique stream key for drilldown. Set
both per container, e.g.:

```
OTEL_RESOURCE_ATTRIBUTES=service.namespace=server-a,service.instance.index=0
```

The dashboard has **Host**, **Server namespace**, and **Server instance
(index)** template variables built on those labels.
