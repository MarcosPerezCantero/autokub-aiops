#!/usr/bin/env bash
# Deja el entorno como estaba antes de inject.sh, volviendo a la revisión anterior.
#
# Uso: ./revert.sh [deployment]    (por defecto, product-catalog)
set -euo pipefail

NAMESPACE=otel-demo
TARGET=${1:-product-catalog}
VARIABLE=PRODUCT_CATALOG_PORT
VALOR_ERRONEO=puerto-invalido

# Se consulta solo el contenedor principal: el Deployment tiene también un contenedor de inicio
valor_actual() {
  kubectl -n "$NAMESPACE" get "deployment/$TARGET" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name=='$TARGET')].env[?(@.name=='$VARIABLE')].value}"
}

if [[ "$(valor_actual)" != "$VALOR_ERRONEO" ]]; then
  echo "El escenario no está activo en $TARGET"
  exit 0
fi

kubectl -n "$NAMESPACE" rollout undo "deployment/$TARGET"
kubectl -n "$NAMESPACE" rollout status "deployment/$TARGET" --timeout=120s
