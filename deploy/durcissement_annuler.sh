#!/bin/bash
# Retour au service utilisateur d'origine (avant deploy/durcissement.sh) :
#   sudo bash /opt/jarvis/deploy/durcissement_annuler.sh
set -euo pipefail

ROOT=/opt/jarvis
DEPLOY_USER=user
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo."; exit 1; }

systemctl disable --now orion orion-librespot 2>/dev/null || true
rm -f /etc/systemd/system/orion.service /etc/systemd/system/orion-librespot.service /etc/sudoers.d/orion-service       /etc/polkit-1/rules.d/50-orion-librespot.rules
systemctl daemon-reload
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$ROOT"
chmod 600 "$ROOT/.env" "$ROOT/config.local.toml"
sed -i '/^player_service = "orion-librespot"$/d; /^player_system = true$/d' "$ROOT/config.local.toml"
sudo -u "$DEPLOY_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$DEPLOY_USER")" systemctl --user enable --now jarvis-librespot jarvis
echo "Service utilisateur d'origine relancé (le compte orion est laissé, sans effet : userdel orion pour le retirer)."
