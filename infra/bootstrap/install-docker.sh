#!/usr/bin/env bash
# Instala Docker Engine (repositorio oficial) en Ubuntu sobre WSL2.
# Idempotente: se puede ejecutar varias veces. Ver docs/adr/0002-entorno-laboratorio.md
#
# Uso (desde Ubuntu):   sudo ./install-docker.sh [usuario]
# Uso (desde Windows):  wsl -d Ubuntu-24.04 -u root -- bash -s < install-docker.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

TARGET_USER="${1:-${SUDO_USER:-autokub}}"
# Huella publicada por Docker para su clave de firma de paquetes
EXPECTED_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

# 1. Limpia definiciones previas del repositorio (posibles restos defectuosos)
rm -f /etc/apt/sources.list.d/docker.sources /etc/apt/sources.list.d/docker.list

# 2. Dependencias para descargar por HTTPS y verificar claves
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg >/dev/null

# 3. Clave GPG de Docker, verificando su huella antes de confiar en ella
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
FPR=$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
if [[ "$FPR" != "$EXPECTED_FPR" ]]; then
  echo "ERROR: huella inesperada ($FPR). Abortando." >&2
  exit 1
fi

# 4. Repositorio oficial, limitado a paquetes firmados con esa clave
CODENAME=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
cat > /etc/apt/sources.list.d/docker.sources <<SRC
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
SRC

# 5. Docker Engine, CLI, containerd y plugins (buildx, compose)
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null

# 6. Docker sin sudo para el usuario (pertenecer al grupo docker equivale a root)
usermod -aG docker "$TARGET_USER"

# 7. Servicio gestionado por systemd y arranque automático
systemctl enable --now docker containerd

echo "Docker $(docker version --format '{{.Server.Version}}') instalado; usuario '$TARGET_USER' en el grupo docker."
