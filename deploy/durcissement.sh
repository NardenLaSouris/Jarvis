#!/bin/bash
# Durcissement du service ORION sur le mini-PC (docs/durcissement_systeme.md). À lancer UNE fois, par vous :
#   sudo bash /opt/jarvis/deploy/durcissement.sh
# Effet : le service tourne sous un compte « orion » sans sudo, docker ni lxd, dans un service systemd durci ; le
# code et la configuration lui sont en lecture seule ; seuls data/ et le cache des modèles Whisper sont modifiables.
# librespot passe aussi sous orion (service orion-librespot) ; SearXNG reste dans docker, joint par HTTP seulement.
# Le compte « user » reste propriétaire du code (git pull) et peut redémarrer le service sans mot de passe.
# Retour arrière : sudo bash /opt/jarvis/deploy/durcissement_annuler.sh
set -euo pipefail

ROOT=/opt/jarvis
DEPLOY_USER=user
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo."; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "$ROOT/.venv introuvable."; exit 1; }

echo "1. Compte de service « orion » (sans shell, sans groupe privilégié)"
id orion >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin orion
usermod -aG orion "$DEPLOY_USER"                  # librespot (sous user) écrit dans data/music.fifo

echo "2. Droits : code et configuration lisibles par orion, non modifiables ; data/ et cache Whisper modifiables"
chown -R "$DEPLOY_USER:orion" "$ROOT"
chmod -R u+rwX,g+rX,g-w,o-rwx "$ROOT"
chown -R orion:orion "$ROOT/data" "$ROOT/models/whisper"
chmod -R u+rwX,g+rwX,o-rwx "$ROOT/data" "$ROOT/models/whisper"
chmod g+s "$ROOT/data"
chown "$DEPLOY_USER:orion" "$ROOT/.env" "$ROOT/config.local.toml"
chmod 640 "$ROOT/.env" "$ROOT/config.local.toml"

echo "3. Lecteur Spotify (librespot) déplacé sous le compte orion, en service système"
install -m 755 -o root -g root "/home/$DEPLOY_USER/.local/bin/librespot" /usr/local/bin/librespot
install -d -m 700 -o orion -g orion /var/lib/orion /var/lib/orion/librespot
cp -a "/home/$DEPLOY_USER/.cache/librespot/." /var/lib/orion/librespot/ 2>/dev/null || true
chown -R orion:orion /var/lib/orion
cat > /etc/systemd/system/orion-librespot.service <<UNIT
[Unit]
Description=ORION, lecteur Spotify (librespot, appareil « ORION »), compte de service durci
After=network-online.target

[Service]
User=orion
Group=orion
ExecStart=/usr/local/bin/librespot --name ORION --device-type speaker --cache /var/lib/orion/librespot --disable-discovery --disable-audio-cache --backend pipe --device $ROOT/data/music.fifo --format S16 --bitrate 320 --initial-volume 100
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=$ROOT/data /var/lib/orion/librespot
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
UNIT
# ORION relance son lecteur quand l'appareil disparaît de Spotify : seul ce service, et seulement le relancer.
cat > /etc/polkit-1/rules.d/50-orion-librespot.rules <<'RULE'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "orion" &&
        action.lookup("unit") == "orion-librespot.service" && action.lookup("verb") == "restart") {
        return polkit.Result.YES;
    }
});
RULE
if ! grep -q "^player_system" "$ROOT/config.local.toml"; then
    sed -i '/^\[spotify\]$/a player_service = "orion-librespot"\nplayer_system = true' "$ROOT/config.local.toml"
fi
grep -A3 "^\[spotify\]" "$ROOT/config.local.toml"

echo "4. Service système durci"
cat > /etc/systemd/system/orion.service <<UNIT
[Unit]
Description=ORION, assistant vocal (Core), compte de service durci
After=network-online.target orion-librespot.service
Wants=network-online.target

[Service]
User=orion
Group=orion
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m jarvis
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Paris HF_HUB_OFFLINE=1
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=$ROOT/data $ROOT/models/whisper
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
UMask=0007

[Install]
WantedBy=multi-user.target
UNIT

echo "5. Redémarrage sans mot de passe pour $DEPLOY_USER (ce service seulement)"
cat > /etc/sudoers.d/orion-service <<SUDO
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart orion, /usr/bin/systemctl stop orion, /usr/bin/systemctl start orion, /usr/bin/systemctl status orion, /usr/bin/systemctl restart orion-librespot
SUDO
chmod 440 /etc/sudoers.d/orion-service
visudo -cf /etc/sudoers.d/orion-service

echo "6. Bascule : services utilisateur arrêtés, services système démarrés"
sudo -u "$DEPLOY_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$DEPLOY_USER")" systemctl --user disable --now jarvis jarvis-librespot || true
chown -R orion:orion "$ROOT/data"
systemctl daemon-reload
systemctl enable --now orion-librespot orion
sleep 20
systemctl is-active orion-librespot orion && echo "ORION tourne sous le compte orion (journal : journalctl -u orion -f)."
