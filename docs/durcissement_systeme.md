# Durcissement du système du mini-PC (EN ATTENTE DE VALIDATION — rien n'est appliqué)

Constat (chantier 6, mesuré le 9 octobre 2026) : le service ORION tourne sous le compte `user`, propriétaire du
code (`/opt/jarvis`), de la configuration et des politiques Cedar, et membre des groupes `sudo`, `docker` et `lxd`
(équivalents à root). Aucun outil d'ORION ne donne aujourd'hui accès au système de fichiers ou à un shell du mini
(vérifié : outils de fichiers exécutés sur le PC, dans des dossiers autorisés ; aucune commande libre), mais si une
faille du Core était exploitée, rien au niveau du système ne la limiterait.

## Proposition (à appliquer avec sudo, après accord)

1. Compte dédié sans privilège : `sudo useradd --system --create-home --shell /usr/sbin/nologin orion`
   (aucun groupe sudo, docker ou lxd ; groupe `audio` inutile : le son passe par le réseau).
2. Code, configuration et politiques en lecture seule pour ce compte :
   `sudo chown -R root:orion /opt/jarvis && sudo chmod -R u=rwX,g=rX,o= /opt/jarvis`
   puis données seules en écriture : `sudo chown -R orion:orion /opt/jarvis/data /opt/jarvis/models/openwakeword`
   et `.env` : `sudo chown root:orion /opt/jarvis/.env && sudo chmod 640 /opt/jarvis/.env`.
3. Service système durci (au lieu du service utilisateur) : `User=orion`, `NoNewPrivileges=yes`,
   `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `ReadWritePaths=/opt/jarvis/data`,
   `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `CapabilityBoundingSet=`, `LockPersonality=yes`.
4. Mises à jour du code : faites par `user` (git pull) puis `sudo systemctl restart jarvis`, jamais par le service.
5. Retirer `user` des groupes `docker` et `lxd` s'ils ne servent pas (vérifier SearXNG, qui tourne peut-être sous
   docker) : `sudo gpasswd -d user lxd`.

## Points à vérifier avant (non vérifiés)
- SearXNG (127.0.0.1:8080) : lancé par docker sous `user` ? Le retrait du groupe docker le casserait.
- librespot (service utilisateur `jarvis-librespot`) écrit `data/music.fifo` : le compte `orion` doit pouvoir le lire.
- Le cache des modèles Whisper et Hugging Face (`~/.cache`) doit être déplacé ou rendu lisible par `orion`.

## Retour arrière
Réactiver le service utilisateur (`systemctl --user enable --now jarvis`), désactiver le service système, rendre
`/opt/jarvis` à `user` (`sudo chown -R user:user /opt/jarvis`).
