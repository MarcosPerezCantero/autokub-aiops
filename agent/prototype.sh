#!/usr/bin/env bash
# Arranca la cadena completa del prototipo: OPA, ejecutor y detector.
# Ctrl+C la detiene. Para provocar un incidente: ./scenarios/crashloop/inject.sh
#
# Uso (desde la raíz del repositorio): ./agent/prototype.sh
set -euo pipefail

if [[ ! -f .local/reader.kubeconfig || ! -f .local/executor.kubeconfig ]]; then
  ./infra/service-accounts.sh
fi

opa run --server --addr 127.0.0.1:8181 --log-level error policies/remediation.rego &
OPA_PID=$!
python3 executor/executor.py > /dev/null &
EXECUTOR_PID=$!
trap 'kill "$OPA_PID" "$EXECUTOR_PID" 2>/dev/null' EXIT
sleep 2

python3 agent/watcher.py
