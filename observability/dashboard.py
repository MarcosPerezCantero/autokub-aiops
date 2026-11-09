#!/usr/bin/env python3
"""Genera el panel de Grafana del catálogo de incidentes (observability/dashboard.json).

Mismas consultas que las alertas de observability/alerts.yml, para ver la evolución que las dispara.
Uso: python3 observability/dashboard.py > observability/dashboard.json
"""
import json

DATASOURCE = {"type": "prometheus", "uid": "webstore-metrics"}  # Prometheus de la tienda
SERVER = 'span_kind="SPAN_KIND_SERVER"'


def panel(id_, title, expr, legend, kind="timeseries", unit=None, x=0, y=0, w=12, h=8, thresholds=None):
    item = {
        "id": id_,
        "type": kind,
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "datasource": DATASOURCE, "expr": expr, "legendFormat": legend}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    if unit:
        item["fieldConfig"]["defaults"]["unit"] = unit
    if thresholds is not None:
        item["fieldConfig"]["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}, {"color": "red", "value": thresholds}],
        }
        item["fieldConfig"]["defaults"]["custom"] = {"thresholdsStyle": {"mode": "line"}}
    return item


alerts = panel(
    1, "Alertas activas", 'ALERTS{alertstate="firing", catalog!=""}', "", kind="table", y=0, w=24, h=6
)
alerts["targets"][0].update({"instant": True, "format": "table"})
alerts["transformations"] = [
    {"id": "organize", "options": {
        "excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True, "job": True, "instance": True},
        "renameByName": {"alertname": "Alerta", "catalog": "Incidente", "severity": "Gravedad"},
    }}
]

panels = [
    alerts,
    panel(
        2, "Peticiones con error por servicio",
        f'sum by (service_name) (rate(traces_span_metrics_calls_total{{{SERVER}, status_code="STATUS_CODE_ERROR"}}[2m]))'
        f' / sum by (service_name) (rate(traces_span_metrics_calls_total{{{SERVER}}}[2m]))',
        "{{service_name}}", unit="percentunit", y=6, thresholds=0.05,
    ),
    panel(
        3, "Latencia p95 por servicio",
        "histogram_quantile(0.95, sum by (service_name, le) "
        f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{SERVER}}}[5m])))",
        "{{service_name}}", unit="ms", x=12, y=6, thresholds=1000,
    ),
    panel(
        4, "Reinicios de contenedores (5 min)",
        "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[5m])) > 0",
        "{{namespace}}/{{pod}}", y=14,
    ),
    panel(
        5, "Réplicas no disponibles",
        "sum by (namespace, deployment) (kube_deployment_status_replicas_unavailable) > 0",
        "{{namespace}}/{{deployment}}", x=12, y=14,
    ),
]

print(json.dumps({
    "uid": "autokub-catalogo",
    "title": "autokub — catálogo de incidentes",
    "tags": ["autokub"],
    "timezone": "browser",
    "refresh": "30s",
    "time": {"from": "now-30m", "to": "now"},
    "schemaVersion": 39,
    "panels": panels,
}, indent=2, ensure_ascii=False))
