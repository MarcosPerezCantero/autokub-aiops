#!/usr/bin/env bash
# Instala las herramientas cliente del laboratorio en Ubuntu sobre WSL2:
#   kubectl (API de Kubernetes), k3d (clúster k3s en Docker), Helm (charts), k9s (interfaz de terminal),
#   gh (GitHub CLI) y make.
# Idempotente: se puede ejecutar varias veces. Ver docs/adr/0002-entorno-laboratorio.md
#
# Uso (desde Ubuntu):   sudo ./install-k8s-tools.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- Versiones fijadas (cambiar aquí para actualizar) ---------------------------
# kubectl: misma minor que el k3s por defecto de k3d (política de compatibilidad ±1)
KUBE_MINOR="v1.35"
K3D_VERSION="v5.9.0"   # su k3s por defecto es v1.35.x
# SHA-256 de k3d-linux-amd64 (checksums.txt de la release, verificado al fijar la versión)
K3D_SHA256="06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0"
K9S_VERSION="v0.51.0"
# SHA-256 de k9s_Linux_amd64.tar.gz (checksums.sha256 de la release)
K9S_SHA256="c3752ad51a5a4015a113819c4eeb6e55a4d0e4b8e652494797532f6fc8161dd7"

# --- Huellas publicadas de las claves de firma de cada repositorio --------------
K8S_FPR="DE15B14486CD377B9E876E1A234654DA9A296436"     # pkgs.k8s.io
HELM_FPR="DDF78C3E6EBB2D2CC223C95C62BA89D07698DBC6"    # helm.sh/docs/intro/install
# gh publica dos claves (la de 2022 caducó en 2026-09; la de 2026 es la vigente)
GH_FPRS="2C6106201985B60E6C7AC87323F3D4EA75716059 7F38BBB59D064DBCB3D84D725612B36462313325"

KEYRINGS=/etc/apt/keyrings
SOURCES=/etc/apt/sources.list.d

# 0. Comprobaciones previas
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: ejecutar con sudo." >&2
  exit 1
fi
ARCH=$(dpkg --print-architecture)
if [[ "$ARCH" != "amd64" ]]; then
  echo "ERROR: arquitectura $ARCH no soportada (el checksum de k3d es de amd64)." >&2
  exit 1
fi

# Descarga una clave y aborta si sus huellas primarias no son exactamente las esperadas.
# Comparar el conjunto completo evita aceptar un fichero con una clave extra añadida.
fetch_key() {
  local url=$1 dest=$2 expected=$3 tmp got want
  tmp=$(mktemp)
  curl -fsSL "$url" -o "$tmp"
  got=$(gpg --show-keys --with-colons "$tmp" 2>/dev/null \
        | awk -F: '/^pub/{p=1; next} /^fpr/ && p {print $10; p=0} /^sub/{p=0}' | sort | xargs)
  want=$(tr ' ' '\n' <<<"$expected" | sort | xargs)
  if [[ "$got" != "$want" ]]; then
    echo "ERROR: huella inesperada en $url ($got). Abortando." >&2
    rm -f "$tmp"
    exit 1
  fi
  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
}

# 1. Límites del kernel para k3d (lo primero: con el cupo agotado fallan hasta sudo y apt): todos los nodos y pods comparten el kernel de la VM y sus
#    procesos corren como root, así que agotan las 128 instancias de inotify por defecto
#    ("too many open files" en kubectl logs -f, k9s o los propios pods).
cat > /etc/sysctl.d/99-autokub.conf <<SYSCTL
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288
SYSCTL
sysctl -q -p /etc/sysctl.d/99-autokub.conf

# 2. Dependencias para descargar por HTTPS y verificar claves, y make (objetivos up/down del laboratorio)
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg make >/dev/null
install -m 0755 -d "$KEYRINGS"

# 3. Claves de firma de los tres repositorios apt
fetch_key "https://pkgs.k8s.io/core:/stable:/${KUBE_MINOR}/deb/Release.key" "$KEYRINGS/kubernetes.asc" "$K8S_FPR"
fetch_key "https://packages.buildkite.com/helm-linux/helm-debian/gpgkey"    "$KEYRINGS/helm.asc"       "$HELM_FPR"
fetch_key "https://cli.github.com/packages/githubcli-archive-keyring.gpg"   "$KEYRINGS/githubcli.gpg"  "$GH_FPRS"

# 4. Repositorios, cada uno limitado a paquetes firmados con su propia clave.
#    Se borran antes posibles definiciones .list de instalaciones manuales previas.
rm -f "$SOURCES"/kubernetes.list "$SOURCES"/helm-stable-debian.list "$SOURCES"/github-cli.list

# El repo de Kubernetes es uno por minor: fija kubectl a KUBE_MINOR por diseño
cat > "$SOURCES/kubernetes.sources" <<SRC
Types: deb
URIs: https://pkgs.k8s.io/core:/stable:/${KUBE_MINOR}/deb/
Suites: /
Signed-By: $KEYRINGS/kubernetes.asc
SRC

cat > "$SOURCES/helm.sources" <<SRC
Types: deb
URIs: https://packages.buildkite.com/helm-linux/helm-debian/any/
Suites: any
Components: main
Signed-By: $KEYRINGS/helm.asc
SRC

cat > "$SOURCES/github-cli.sources" <<SRC
Types: deb
URIs: https://cli.github.com/packages
Suites: stable
Components: main
Architectures: $ARCH
Signed-By: $KEYRINGS/githubcli.gpg
SRC

# 5. kubectl, Helm y gh desde sus repositorios (apt no reinstala lo que ya está al día)
apt-get update -qq
apt-get install -y -qq kubectl helm gh >/dev/null

# 6. k3d: binario de GitHub Releases, solo si falta o no es la versión fijada
if [[ "$(k3d version 2>/dev/null | awk '/^k3d version/{print $3}')" != "$K3D_VERSION" ]]; then
  tmp=$(mktemp)
  curl -fsSL "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-amd64" -o "$tmp"
  if ! echo "$K3D_SHA256  $tmp" | sha256sum -c --quiet -; then
    echo "ERROR: checksum de k3d no coincide. Abortando." >&2
    rm -f "$tmp"
    exit 1
  fi
  install -m 0755 "$tmp" /usr/local/bin/k3d
  rm -f "$tmp"
fi

# 7. k9s: igual que k3d (binario de GitHub Releases, versión y checksum fijados)
if [[ "$(k9s version -s 2>/dev/null | awk '/^Version/{print $2}')" != "$K9S_VERSION" ]]; then
  tmpdir=$(mktemp -d)
  curl -fsSL "https://github.com/derailed/k9s/releases/download/${K9S_VERSION}/k9s_Linux_amd64.tar.gz" -o "$tmpdir/k9s.tar.gz"
  if ! echo "$K9S_SHA256  $tmpdir/k9s.tar.gz" | sha256sum -c --quiet -; then
    echo "ERROR: checksum de k9s no coincide. Abortando." >&2
    rm -rf "$tmpdir"
    exit 1
  fi
  tar -xzf "$tmpdir/k9s.tar.gz" -C "$tmpdir" k9s
  install -m 0755 "$tmpdir/k9s" /usr/local/bin/k9s
  rm -rf "$tmpdir"
fi

# 8. Autocompletado de bash (se regenera en cada ejecución)
kubectl completion bash > /etc/bash_completion.d/kubectl
helm completion bash    > /etc/bash_completion.d/helm
k3d completion bash     > /etc/bash_completion.d/k3d
gh completion -s bash   > /etc/bash_completion.d/gh
k9s completion bash     > /etc/bash_completion.d/k9s

# 9. Resumen
echo "kubectl $(kubectl version --client -o json | awk -F'"' '/gitVersion/{print $4}')"
echo "helm    $(helm version --short)"
echo "k3d     $(k3d version | awk '/^k3d version/{print $3}') (k3s por defecto: $(k3d version | awk '/^k3s version/{print $3}'))"
echo "k9s     $(k9s version -s | awk '/^Version/{print $2}')"
echo "gh      $(gh --version | awk 'NR==1{print $3}')"
