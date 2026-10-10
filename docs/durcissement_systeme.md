# Durcissement du système du mini-PC (validé le 10 octobre 2026 — script prêt, à lancer par vous avec sudo)

Constat (chantier 6, mesuré le 9 octobre 2026) : le service ORION tourne sous le compte `user`, propriétaire du
code (`/opt/jarvis`), de la configuration et des politiques Cedar, et membre des groupes `sudo`, `docker` et `lxd`
(équivalents à root). Aucun outil d'ORION ne donne aujourd'hui accès au système de fichiers ou à un shell du mini
(vérifié : outils de fichiers exécutés sur le PC, dans des dossiers autorisés ; aucune commande libre), mais si une
faille du Core était exploitée, rien au niveau du système ne la limiterait.

## Application

    sudo bash /opt/jarvis/deploy/durcissement.sh

Le script ([deploy/durcissement.sh](../deploy/durcissement.sh)) :
1. crée le compte de service `orion` (sans shell, sans groupe sudo, docker ni lxd) ;
2. laisse le code à `user` (git pull), lisible mais non modifiable par `orion` ; `.env` et `config.local.toml` en
   640 `user:orion` ; seuls `data/` et `models/whisper` modifiables par `orion` ;
3. passe librespot sous `orion` (service système `orion-librespot`, binaire copié dans `/usr/local/bin`, identifiants
   copiés dans `/var/lib/orion/librespot`) ; une règle polkit permet à `orion` de relancer ce seul service
   (`[spotify] player_system = true`, ajouté à `config.local.toml`) ;
4. installe le service système `orion` : `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
   `PrivateDevices` (le son passe par le réseau), aucune capacité, familles réseau limitées à IPv4/IPv6/Unix ;
5. autorise `user` à `sudo systemctl restart orion` (et start, stop, status, restart orion-librespot) sans mot de
   passe, pour les déploiements ;
6. arrête et désactive les services utilisateur `jarvis` et `jarvis-librespot`, démarre les services système.

Après : journal `journalctl -u orion -f` ; déploiement `git pull` puis `sudo systemctl restart orion`.

## Non fait (volontairement)
- `user` reste dans `sudo` (votre compte d'administration) et dans `docker` : SearXNG (`jarvis-searxng`) tourne
  dans docker ; ORION ne le joint qu'en HTTP (127.0.0.1:8080), le compte `orion` n'a aucun accès à docker.
- Retrait de `user` du groupe `lxd`, s'il ne sert pas : `sudo gpasswd -d user lxd` (à votre main).

## Retour arrière

    sudo bash /opt/jarvis/deploy/durcissement_annuler.sh

Supprime les services système, la règle polkit et la règle sudo, rend `/opt/jarvis` à `user`, retire les lignes
ajoutées à `config.local.toml` et relance les services utilisateur d'origine.
