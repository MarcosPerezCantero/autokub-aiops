#!/usr/bin/env bash
# Aplica el RBAC del ejecutor y genera su kubeconfig con un token temporal.
# El fichero queda en .local/ (fuera de Git) y solo sirve para la identidad del ejecutor.
#
# Uso: ./executor/kubeconfig.sh
set -euo pipefail

NAMESPACE=otel-demo
ACCOUNT=autokub-executor
DESTINO=.local/executor.kubeconfig

kubectl apply -f deploy/executor/rbac.yaml

SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
TOKEN=$(kubectl -n "$NAMESPACE" create token "$ACCOUNT" --duration=24h)

umask 077
mkdir -p .local
cat > "$DESTINO" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: autokub
    cluster:
      server: $SERVER
      certificate-authority-data: $CA
users:
  - name: $ACCOUNT
    user:
      token: $TOKEN
contexts:
  - name: executor
    context:
      cluster: autokub
      user: $ACCOUNT
      namespace: $NAMESPACE
current-context: executor
KUBECONFIG
echo "Kubeconfig del ejecutor en $DESTINO (válido 24 h)"
