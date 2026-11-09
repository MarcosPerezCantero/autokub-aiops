#!/usr/bin/env bash
# Aplica el RBAC del lector y del ejecutor, generado desde la configuración, y crea un
# kubeconfig con token temporal para cada uno. Los ficheros quedan en .local/ (fuera de Git).
#
# Uso: ./infra/service-accounts.sh [config/lab.json]
set -euo pipefail

CONFIG=${1:-${AUTOKUB_CONFIG:-config/lab.json}}
SYSTEM_NAMESPACE=autokub-system

python3 infra/rbac.py "$CONFIG" | kubectl apply -f -

generate() {
  local account=$1 destination=$2 server ca token
  server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
  ca=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
  token=$(kubectl -n "$SYSTEM_NAMESPACE" create token "$account" --duration=24h)
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
current-context: $account
KUBECONFIG
  echo "Kubeconfig de $account en $destination (válido 24 h)"
}

umask 077
mkdir -p .local
generate autokub-reader .local/reader.kubeconfig
generate autokub-executor .local/executor.kubeconfig
