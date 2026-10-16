#!/bin/bash
# Retour arrière de la passerelle d'appel (avant deploy/appels.sh) :
#   sudo bash /opt/orion/deploy/appels_annuler.sh
# Les appareils jumelés sont gardés dans /root/orion-call-appareils.json. Côté ORION, désactiver aussi les appels :
# [call] enabled = false dans /opt/orion/config.local.toml, puis sudo systemctl restart orion.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo."; exit 1; }

systemctl disable --now orion-call 2>/dev/null || true
rm -f /etc/systemd/system/orion-call.service /etc/sudoers.d/orion-call
systemctl daemon-reload
[ -f /var/lib/orion-call/call_devices.json ] && install -m 600 /var/lib/orion-call/call_devices.json /root/orion-call-appareils.json
rm -rf /opt/orion-call /var/lib/orion-call /etc/orion-call
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw delete allow from 192.168.27.65 to any port 8770 proto tcp >/dev/null 2>&1 || true
fi
userdel orion-call 2>/dev/null || true
echo "Passerelle retirée (jeton CALL_GATEWAY_TOKEN laissé dans /opt/orion/.env, sans effet sans passerelle)."
