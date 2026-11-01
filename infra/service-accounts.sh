#!/usr/bin/env bash
# Aplica el RBAC del lector y del ejecutor y genera un kubeconfig con token temporal para cada uno.
# Los ficheros quedan en .local/ (fuera de Git).
#
# Uso: ./infra/service-accounts.sh
set -euo pipefail

NAMESPACE=otel-demo

generate() {
  local account=$1 rbac=$2 destination=$3 server ca token
  kubectl apply -f "$rbac"
  server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
  ca=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
  token=$(kubectl -n "$NAMESPACE" create token "$account" --duration=24h)
  cat > "$destination" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: autokub
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $account
    user:
      token: $token
contexts:
  - name: $account
    context:
      cluster: autokub
      user: $account
      namespace: $NAMESPACE
current-context: $account
KUBECONFIG
  echo "Kubeconfig de $account en $destination (válido 24 h)"
}

umask 077
mkdir -p .local
generate autokub-reader deploy/agent/rbac.yaml .local/reader.kubeconfig
generate autokub-executor deploy/executor/rbac.yaml .local/executor.kubeconfig
