#!/usr/bin/env bash
# Provoca un CrashLoopBackOff: despliega una revisión con configuración errónea,
# como lo haría una entrega defectuosa. Revertir con revert.sh.
#
# Uso: ./inject.sh [deployment]    (por defecto, product-catalog)
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

if [[ "$(valor_actual)" == "$VALOR_ERRONEO" ]]; then
  echo "El escenario ya está activo en $TARGET"
  exit 0
fi

kubectl -n "$NAMESPACE" set env "deployment/$TARGET" -c "$TARGET" "$VARIABLE=$VALOR_ERRONEO"
kubectl -n "$NAMESPACE" annotate "deployment/$TARGET" --overwrite \
  kubernetes.io/change-cause="escenario crashloop: $VARIABLE erróneo"
echo "Escenario crashloop activo en $TARGET"
