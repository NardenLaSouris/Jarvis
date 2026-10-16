#!/bin/bash
# Mise en service de la passerelle d'appel d'ORION (orion-call) sur le mini-PC. À lancer par vous, avec sudo :
#   sudo bash /opt/orion/deploy/appels.sh <compte SIP d'ORION> <votre adresse Linphone>
#   ex. : sudo bash /opt/orion/deploy/appels.sh orionproject sip:vous@sip.linphone.org
# Mise à jour du code après un « git pull » (sans sudo pour la copie, relance sans mot de passe) :
#   bash /opt/orion/deploy/appels.sh --code
# Retour arrière : sudo bash /opt/orion/deploy/appels_annuler.sh
#
# Effet : compte système « orion-call » (sans shell ni groupe privilégié), environnement Python à part
# (/opt/orion-call/venv, versions figées de requirements-call.txt), seulement les fichiers d'ORION utiles à la
# passerelle (/opt/orion-call/lib, à vous, lisibles par orion-call), secrets à part (/etc/orion-call/env : jeton du
# Core et mot de passe Linphone ; orion-call n'a aucun accès au .env d'ORION), appareils jumelés dans
# /var/lib/orion-call, service systemd durci, port 8770 ouvert à la maison et au VPN seulement.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  # dossier du dépôt (/opt/orion, ou /opt/jarvis avant la migration)
BASE=/opt/orion-call
DATA=/var/lib/orion-call
CONF=/etc/orion-call
DEPLOY_USER=user
VPN_PHONE=192.168.27.65  # iPhone par le VPN WireGuard de la Freebox (adresse fixe)

copy_code() {  # fichiers d'ORION utiles à la passerelle, rien d'autre (ni Core, ni configuration, ni secrets)
    for d in "$BASE/lib" "$BASE/lib/orion" "$BASE/lib/orion/call" "$BASE/lib/jarvis" "$BASE/lib/jarvis/call"; do
        install -d -m 750 -o "$DEPLOY_USER" -g orion-call "$d"
    done
    for f in __init__.py net.py persist.py config.py trace.py; do
        install -m 640 -o "$DEPLOY_USER" -g orion-call "$ROOT/orion/$f" "$BASE/lib/orion/$f"
    done
    install -m 640 -o "$DEPLOY_USER" -g orion-call "$ROOT"/orion/call/*.py "$BASE/lib/orion/call/"
    # Ancien nom (unité installée avant la migration : « -m jarvis.call.gateway ») : simple renvoi vers orion.
    install -m 640 -o "$DEPLOY_USER" -g orion-call "$ROOT/jarvis/__init__.py" "$BASE/lib/jarvis/__init__.py"
    install -m 640 -o "$DEPLOY_USER" -g orion-call "$ROOT"/jarvis/call/*.py "$BASE/lib/jarvis/call/"
}

if [ "${1:-}" = "--code" ]; then  # mise à jour : copie (vous êtes propriétaire de lib), puis relance
    mkdir -p "$BASE/lib/orion/call" "$BASE/lib/jarvis/call"
    for f in __init__.py net.py persist.py config.py trace.py; do cp "$ROOT/orion/$f" "$BASE/lib/orion/$f"; done
    cp "$ROOT"/orion/call/*.py "$BASE/lib/orion/call/"
    cp "$ROOT/jarvis/__init__.py" "$BASE/lib/jarvis/__init__.py"  # ancien nom : renvoi vers orion
    cp "$ROOT"/jarvis/call/*.py "$BASE/lib/jarvis/call/"
    sudo -n systemctl restart orion-call && sleep 3 && systemctl is-active orion-call
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo."; exit 1; }
SIP_USER="${1:?compte SIP ORION attendu (ex. orionproject)}"
SIP_PEER="${2:?votre adresse Linphone attendue (ex. sip:vous@sip.linphone.org)}"
[[ "$SIP_USER" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Compte SIP invalide."; exit 1; }
[[ "$SIP_PEER" =~ ^sip:[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$ ]] || { echo "Adresse Linphone invalide."; exit 1; }
[ -f "$ROOT/requirements-call.txt" ] || { echo "$ROOT/requirements-call.txt introuvable (git pull ?)."; exit 1; }

echo "1. Compte de service « orion-call » (sans shell, sans groupe privilégié)"
id orion-call >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin orion-call
usermod -aG orion-call "$DEPLOY_USER"  # mises à jour du code sans sudo (copie dans lib)

echo "2. Environnement Python à part (versions figées)"
install -d -m 755 -o root -g root "$BASE"
[ -x "$BASE/venv/bin/python" ] || python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q --upgrade pip
"$BASE/venv/bin/pip" install -q -r "$ROOT/requirements-call.txt"
"$BASE/venv/bin/python" -c "import aiortc, av, pylibsrtp, numpy; print('   aiortc', aiortc.__version__, '| av', av.__version__)"

echo "3. Code de la passerelle (seulement orion/call et ses 4 modules de base)"
copy_code

echo "4. Données et secrets"
install -d -m 700 -o orion-call -g orion-call "$DATA"
if [ ! -f "$DATA/call_devices.json" ] && [ -f "/home/$DEPLOY_USER/v2-qa/data/call_devices.json" ]; then
    install -m 600 -o orion-call -g orion-call "/home/$DEPLOY_USER/v2-qa/data/call_devices.json" "$DATA/call_devices.json"
    echo "   appareils jumelés repris de la passerelle d'essai"
fi
install -d -m 750 -o root -g orion-call "$CONF"
TOKEN=$(sed -n 's/^CALL_GATEWAY_TOKEN=//p' "$ROOT/.env" | head -1)
if [ -z "$TOKEN" ]; then
    TOKEN=$("$BASE/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(32))")
    [ -z "$(tail -c1 "$ROOT/.env")" ] || echo >> "$ROOT/.env"
    printf "CALL_GATEWAY_TOKEN=%s\n" "$TOKEN" >> "$ROOT/.env"  # le Core le lit ici ; propriétaire et droits gardés
fi
PASSWORD=$(sed -n 's/^LINPHONE_ORION_PASSWORD=//p' "/home/$DEPLOY_USER/v2-qa/.env" 2>/dev/null | head -1 || true)
if [ -z "$PASSWORD" ]; then
    read -rsp "Mot de passe du compte SIP $SIP_USER (non affiché) : " PASSWORD; echo
fi
umask 027
printf "CALL_GATEWAY_TOKEN=%s\nLINPHONE_ORION_PASSWORD=%s\n" "$TOKEN" "$PASSWORD" > "$CONF/env"
chown root:orion-call "$CONF/env"
chmod 640 "$CONF/env"
cat > "$CONF/call.toml" <<TOML
# Passerelle d'appel d'ORION (deploy/appels.sh). Secrets : $CONF/env.
[call]
gateway = "http://127.0.0.1:8770"
listen_host = "0.0.0.0"
app_networks = ["192.168.1.0/24", "$VPN_PHONE/32"]
devices_path = "$DATA/call_devices.json"
sip_user = "$SIP_USER"
sip_domain = "sip.linphone.org"
sip_peer = "$SIP_PEER"
TOML
chown root:orion-call "$CONF/call.toml"
chmod 640 "$CONF/call.toml"
unset PASSWORD TOKEN

echo "5. Service système durci"
cat > /etc/systemd/system/orion-call.service <<UNIT
[Unit]
Description=ORION, passerelle d'appel (application mobile, Linphone), compte de service durci
After=network-online.target
Wants=network-online.target

[Service]
User=orion-call
Group=orion-call
WorkingDirectory=$BASE
Environment=PYTHONPATH=$BASE/lib PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Paris
EnvironmentFile=$CONF/env
ExecStart=$BASE/venv/bin/python -m orion.call.gateway --config $CONF/call.toml
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths=$DATA
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
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallArchitectures=native
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

echo "6. Relance sans mot de passe pour $DEPLOY_USER (ce service seulement)"
cat > /etc/sudoers.d/orion-call <<SUDO
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart orion-call, /usr/bin/systemctl stop orion-call, /usr/bin/systemctl start orion-call, /usr/bin/systemctl status orion-call
SUDO
chmod 440 /etc/sudoers.d/orion-call
visudo -cf /etc/sudoers.d/orion-call

echo "7. Pare-feu du mini : port 8770 pour la maison et l'iPhone par le VPN seulement"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow from 192.168.1.0/24 to any port 8770 proto tcp comment "ORION passerelle appels" >/dev/null
    ufw allow from "$VPN_PHONE" to any port 8770 proto tcp comment "ORION passerelle appels (iPhone VPN)" >/dev/null
fi

echo "8. Démarrage"
systemctl daemon-reload
systemctl enable --now orion-call
sleep 12
systemctl is-active orion-call
journalctl -u orion-call -n 8 --no-pager | sed -E 's/^.*python\[[0-9]+\]: //'
echo "Passerelle en service (journal : journalctl -u orion-call -f)."
