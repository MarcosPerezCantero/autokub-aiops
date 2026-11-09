#!/usr/bin/env bash
# Activa o desactiva un fallo de la tienda cambiando la variante por defecto de una feature flag.
# flagd copia el ConfigMap a un directorio propio al arrancar (para que su interfaz pueda editarlo),
# así que tras cambiarlo hay que reiniciar flagd. `make demo` vuelve a dejar todas las flags en off.
#
# Uso: ./set-flag.sh <flag> <variante>
#   ./set-flag.sh paymentFailure 50%      # incidente 2: payment falla, el síntoma aparece en checkout
#   ./set-flag.sh emailMemoryLeak 100x    # incidente 3: fuga de memoria en email (OOMKilled)
#   ./set-flag.sh intlShippingSlowdown on # incidente 4: latencia en shipping
#   ./set-flag.sh paymentFailure off      # revertir
set -euo pipefail

NAMESPACE=otel-demo
FLAG=${1:?flag}
VARIANT=${2:?variante}

kubectl -n "$NAMESPACE" get configmap flagd-config -o json | python3 -c '
import json, sys
flag, variant = sys.argv[1], sys.argv[2]
cm = json.load(sys.stdin)
flags = json.loads(cm["data"]["demo.flagd.json"])
if flag not in flags["flags"]:
    sys.exit(f"flag desconocida: {flag}")
variants = flags["flags"][flag]["variants"]
if variant not in variants:
    sys.exit(f"variante desconocida para {flag}: {variant} (posibles: {sorted(variants)})")
flags["flags"][flag]["defaultVariant"] = variant
cm["data"]["demo.flagd.json"] = json.dumps(flags, indent=2)
print(json.dumps(cm))
' "$FLAG" "$VARIANT" | kubectl replace -f - >/dev/null
kubectl -n "$NAMESPACE" rollout restart deployment/flagd >/dev/null
kubectl -n "$NAMESPACE" rollout status deployment/flagd --timeout=120s >/dev/null
echo "$FLAG = $VARIANT"
