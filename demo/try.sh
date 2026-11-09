#!/usr/bin/env bash
# Prueba autokub en un clúster local en un solo comando: crea un clúster k3d, despliega la tienda
# de demostración, construye las imágenes e instala autokub en modo propuesta.
# Requisitos: Docker, k3d, kubectl y helm. Unos 6 GB de RAM libres.
#
# Uso (desde la raíz del repositorio): make try
set -euo pipefail

NAMESPACE=autokub-system
TAG=dev
VALUES=.local/try-values.json

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

for tool in docker k3d kubectl helm python3; do
  command -v "$tool" >/dev/null || { echo "Falta $tool" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker no está en marcha" >&2; exit 1; }

mkdir -p .local
step "1/5 Clúster k3d"
make --no-print-directory up

step "2/5 Tienda de demostración (puede tardar unos minutos la primera vez)"
make --no-print-directory demo >/dev/null
kubectl -n otel-demo wait --for=condition=Available deployment --all --timeout=600s >/dev/null
echo "Tienda lista en http://localhost:8080"

step "3/5 Imágenes de autokub"
make --no-print-directory images > .local/try-build.log 2>&1 || { tail -20 .local/try-build.log; exit 1; }
k3d image import -c autokub "ghcr.io/marcosperezcantero/autokub-agent:$TAG" \
  "ghcr.io/marcosperezcantero/autokub-executor:$TAG" >/dev/null

step "4/5 Modelo"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
proposer=rules
if [[ -f .local/anthropic.env ]]; then
  # La clave se lee aquí y solo llega al Secret que usa el pod del agente
  key=$(sed -n 's/^ANTHROPIC_API_KEY=//p' .local/anthropic.env | tr -d '"'"'"'')
  if [[ -n "$key" ]]; then
    kubectl -n "$NAMESPACE" create secret generic autokub-llm --from-literal=api-key="$key" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    proposer=llm
  fi
fi
echo "Proponedor: $proposer$([[ $proposer == rules ]] && echo ' (sin clave de API: reglas fijas; crea .local/anthropic.env para usar el agente LLM)')"

step "5/5 autokub en modo propuesta"
python3 - "$proposer" "$TAG" > "$VALUES" <<'PY'
import json, sys
config = json.load(open("config/lab.json"))
print(json.dumps({"mode": "propose", "proposer": sys.argv[1], "namespaces": config["namespaces"],
                  "image": {"tag": sys.argv[2], "pullPolicy": "Never"}}))
PY
helm upgrade --install autokub charts/autokub -n "$NAMESPACE" -f "$VALUES" >/dev/null
kubectl -n "$NAMESPACE" rollout status deploy/autokub-executor --timeout=180s >/dev/null
kubectl -n "$NAMESPACE" rollout status deploy/autokub-agent --timeout=180s >/dev/null

cat <<MSG

autokub está en marcha en modo propuesta: detecta y propone, pero no modifica nada.

  make try-incident   provoca un CrashLoopBackOff en product-catalog
  make try-events     muestra en directo qué detecta, qué propone y qué decide OPA
  make try-execute    pasa a modo ejecución: autokub aplica lo que OPA autoriza
  make try-reset      revierte el incidente y vuelve a modo propuesta
  make down           borra el clúster
MSG
