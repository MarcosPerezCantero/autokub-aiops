#!/usr/bin/env bash
# Arranca la cadena completa del prototipo: OPA, ejecutor y detector.
# Ctrl+C la detiene. Para provocar un incidente: ./scenarios/crashloop/inject.sh
#
# Uso (desde la raíz del repositorio): ./agent/prototype.sh [rules|llm]
set -euo pipefail

PROPOSER=${1:-rules}

if [[ ! -f .local/reader.kubeconfig || ! -f .local/executor.kubeconfig ]]; then
  ./infra/service-accounts.sh
fi

opa run --server --addr 127.0.0.1:8181 --log-level error policies/remediation.rego &
OPA_PID=$!
python3 executor/executor.py > /dev/null &
EXECUTOR_PID=$!
python3 viewer/server.py &
VIEWER_PID=$!
trap 'kill "$OPA_PID" "$EXECUTOR_PID" "$VIEWER_PID" 2>/dev/null' EXIT
sleep 2

# La clave de API solo la recibe este proceso, que no tiene permisos de escritura
if [[ "$PROPOSER" == "llm" ]]; then
  set -a
  . .local/anthropic.env
  set +a
fi
AUTOKUB_PROPOSER=$PROPOSER .venv/bin/python agent/watcher.py
