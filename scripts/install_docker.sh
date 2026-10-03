#!/usr/bin/env bash
# Installe Docker (paquets Ubuntu) et démarre les conteneurs de JARVIS (SearXNG pour la recherche Web).
#   sudo bash scripts/install_docker.sh
# Relançable sans risque : ce qui est déjà en place est conservé (clé SearXNG comprise).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "À lancer avec sudo : sudo bash $0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="${SUDO_USER:-root}"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update
  apt-get install -y docker.io docker-compose-v2
fi
systemctl enable --now docker

if [ "$OWNER" != root ] && ! id -nG "$OWNER" | grep -qw docker; then
  usermod -aG docker "$OWNER"
  echo "$OWNER ajouté au groupe docker (effectif à la prochaine connexion)."
fi

ENV_FILE="$ROOT/searxng/.env"
if [ ! -s "$ENV_FILE" ]; then
  umask 077
  echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > "$ENV_FILE"
  chown "$OWNER:" "$ENV_FILE"
fi

docker compose -f "$ROOT/searxng/docker-compose.yml" up -d

for _ in $(seq 30); do
  if curl -fs -o /dev/null "http://127.0.0.1:8080/search?q=jarvis&format=json"; then
    echo "SearXNG prêt sur http://127.0.0.1:8080 (redémarre avec Docker)."
    exit 0
  fi
  sleep 2
done
echo "SearXNG ne répond pas encore : docker logs jarvis-searxng" >&2
exit 1
