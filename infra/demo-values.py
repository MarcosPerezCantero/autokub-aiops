#!/usr/bin/env python3
"""Values de observabilidad para el chart de la tienda, con las alertas de observability/alerts.yml.

Las reglas viven en un único fichero, el mismo que prueban los tests de promtool.
Uso: .venv/bin/python infra/demo-values.py > .local/demo-observability.json
"""
import hashlib
import json
import os

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

with open(os.path.join(ROOT, "observability", "alerts.yml"), encoding="utf-8") as file:
    text = file.read()
rules = yaml.safe_load(text)

values = {
    "prometheus": {
        # Estado de los objetos de Kubernetes: reinicios, OOMKilled, réplicas no disponibles
        "kube-state-metrics": {"enabled": True},
        # Recibe las alertas; en la fase del agente completo las enviará a autokub
        "alertmanager": {
            "enabled": True,
            "config": {
                "route": {"receiver": "registro", "group_by": ["alertname", "namespace", "service_name"]},
                "receivers": [{"name": "registro"}],
            },
        },
        "serverFiles": {"alerting_rules.yml": rules},
        # El Prometheus de la tienda no recarga la configuración: si cambian las alertas, se reinicia
        "server": {"podAnnotations": {"autokub/alerts-checksum": hashlib.sha256(text.encode()).hexdigest()[:16]}},
    }
}
print(json.dumps(values, indent=2, ensure_ascii=False))
