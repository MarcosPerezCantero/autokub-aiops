#!/usr/bin/env bash
# Arranca la cadena completa del prototipo: OPA, ejecutor y detector.
# Ctrl+C la detiene. Para provocar un incidente: ./scenarios/crashloop/inject.sh
#
# Uso (desde la raíz del repositorio): ./agent/prototype.sh [rules|llm]
set -euo pipefail

PROPOSER=${1:-rules}
export AUTOKUB_CONFIG=${AUTOKUB_CONFIG:-config/lab.json}

if [[ ! -f .local/reader.kubeconfig || ! -f .local/executor.kubeconfig ]]; then
  ./infra/service-accounts.sh "$AUTOKUB_CONFIG"
fi

# La configuración se carga como datos en data.autokub.config: la política no tiene listas fijas
opa run --server --addr 127.0.0.1:8181 --log-level error policies/remediation.rego "autokub.config:$AUTOKUB_CONFIG" &
OPA_PID=$!
# Token de las aprobaciones: solo lo leen el ejecutor y el panel, nunca el detector ni el agente
if [[ ! -s .local/approval.token ]]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(36))' > .local/approval.token)
fi
AUTOKUB_APPROVAL_TOKEN_FILE=.local/approval.token python3 executor/executor.py > /dev/null &
EXECUTOR_PID=$!
AUTOKUB_APPROVAL_TOKEN_FILE=.local/approval.token python3 panel/server.py &
PANEL_PID=$!
trap 'kill "$OPA_PID" "$EXECUTOR_PID" "$PANEL_PID" 2>/dev/null' EXIT
sleep 2

# La clave de API solo la recibe este proceso, que no tiene permisos de escritura
if [[ "$PROPOSER" == "llm" ]]; then
  set -a
  . .local/anthropic.env
  set +a
fi
# Con el agente LLM, el chat de solo lectura del panel escucha en localhost:8092
AUTOKUB_CHAT_PORT=8092 AUTOKUB_PROPOSER=$PROPOSER .venv/bin/python agent/watcher.py
