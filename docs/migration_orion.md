# Migration du nom JARVIS -> ORION

Le dépôt est passé à ORION le 16 octobre 2026 : paquet Python `orion`, `python -m orion`, modèles d'unités
`deploy/orion.service` et `deploy/orion-librespot.service`, chemins `/opt/orion`, variables `ORION_AGENT_TOKEN` et
`ORION_CALENDAR_ICS`. **Les machines ne sont pas encore migrées** : ce document décrit comment le faire, sans perte
de données et avec retour arrière.

## Compatibilité gardée (tant que les machines ne sont pas migrées)

| Ancien nom | Pourquoi il marche encore | À retirer quand |
|---|---|---|
| `python -m jarvis`, `-m jarvis.winagent`, `-m jarvis.control`, `-m jarvis.call.gateway` | paquet `jarvis/` de simples renvois vers `orion` (aucun code propre, testé) | service, raccourcis et unité `orion-call` migrés |
| `JARVIS_AGENT_TOKEN`, `JARVIS_CALENDAR_ICS` dans les `.env` | `secret()` lit l'ancien nom si le nouveau manque (testé) ; aucun secret à recopier | `.env` renommés (facultatif) |
| `/opt/jarvis` | les scripts `deploy/*.sh` déduisent leur dossier de leur emplacement | après la migration ci-dessous |
| Conteneur `jarvis-searxng` | nom gardé : le changer créerait un second conteneur en conflit sur le port 8080 | si SearXNG est réinstallé un jour |
| Thèmes « jarvis » et « naissancejarvis », `[face] jarvis_birthday`, alias JS `JarvisFace` | valeurs enregistrées dans les réglages et `config.local.toml` | jamais nécessaire |
| Dossier `%APPDATA%\JARVIS Control` | ORION Control le reprend déjà s'il existe | — |
| Modèle et enregistrements du mot « Jarvis » | données et fonctionnalité (ancien mot de réveil) | — |

## Mini-PC : `/opt/jarvis` -> `/opt/orion`

Prérequis : `/opt/jarvis` à jour sur ce commit (`git pull`), ORION redémarré et en bonne santé — avec le paquet de
compatibilité, rien ne change encore pour les services.

1. **Sauvegarde** (configuration, données, unités) :

       sudo tar czf /root/orion-migration-$(date +%F).tar.gz /etc/systemd/system/orion*.service \
         /etc/systemd/system/ollama.service /etc/sudoers.d/orion-service /etc/polkit-1/rules.d \
         /opt/jarvis/config.local.toml /opt/jarvis/.env /opt/jarvis/data

2. **Arrêt** (rien ne doit écrire pendant la copie : pas deux mémoires, deux journaux divergents) :

       sudo systemctl stop orion orion-call orion-librespot

3. **Copie** (propriétaires et droits conservés) puis contrôle :

       sudo cp -a /opt/jarvis /opt/orion
       sudo diff -rq /opt/jarvis/data /opt/orion/data && echo "données identiques"
       sudo -u orion /opt/orion/.venv/bin/python -c "import orion; print('paquet orion ok')"

   L'environnement Python pointe vers le Python du système (`home = /usr/bin`) : il fonctionne après la copie pour
   `python -m`. En cas de souci : `python3 -m venv --clear /opt/orion/.venv` puis réinstaller `requirements.txt`.

4. **Références** (unités, puis vérification des règles qui n'étaient pas lisibles sans droits) :

       sudo sed -i 's#/opt/jarvis#/opt/orion#g; s#-m jarvis$#-m orion#' \
         /etc/systemd/system/orion.service /etc/systemd/system/orion-librespot.service
       sudo sed -i 's#/opt/jarvis#/opt/orion#g' /etc/systemd/system/ollama.service
       sudo grep -rn "jarvis" /etc/systemd/system/orion-call.service /etc/sudoers.d/orion-service /etc/polkit-1/rules.d

   La dernière commande doit ne rien afficher ; sinon corriger ces lignes (`sudo visudo -f /etc/sudoers.d/orion-service`
   pour sudoers). L'unité `orion-call` peut garder `-m jarvis.call.gateway` (renvoi) ; pour le nouveau nom :
   `sudo sed -i 's#jarvis.call.gateway#orion.call.gateway#' /etc/systemd/system/orion-call.service`, puis
   `bash /opt/orion/deploy/appels.sh --code` (copie `orion` et le renvoi `jarvis` dans `/opt/orion-call/lib`).

5. **Démarrage et vérification** :

       sudo systemctl daemon-reload
       sudo systemctl start orion-librespot orion orion-call
       systemctl is-active orion orion-call orion-librespot
       cd /opt/orion && .venv/bin/python -m orion --health
       journalctl -u orion -n 30 --no-pager

   Puis un essai réel : un message Linphone, « Hey Orion, quelle heure est-il ? », le fond d'écran.

6. **Ancien dossier** : laisser `/opt/jarvis` en place, inutilisé, au moins une semaine. Le supprimer seulement après
   validation explicite : `sudo rm -rf /opt/jarvis`.

### Retour arrière (mini-PC)

    sudo systemctl stop orion orion-call orion-librespot
    sudo tar xzf /root/orion-migration-AAAA-MM-JJ.tar.gz -C /     # unités d'origine
    sudo cp -a /opt/orion/data/. /opt/jarvis/data/                 # données écrites depuis la bascule
    sudo systemctl daemon-reload && sudo systemctl start orion-librespot orion orion-call

## PC fixe et Katana

- **Agent Windows** : après mise à jour du code, `powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1`
  crée le raccourci « ORION Agent » (`-m orion.winagent`) et retire « JARVIS Agent ». Le dossier `C:\Users\theju\jarvis`
  peut être renommé ensuite (raccourcis à recréer par le même script) ; ce n'est pas nécessaire.
- **ORION Control** : son raccourci lance `-m jarvis.control` (renvoi) ; le recréer avec `-m orion.control` quand
  pratique.
- **Katana** : le code de l'agent se met à jour par `scripts/update_remote_agent.sh` (copie `orion` et le renvoi
  `jarvis`). La tâche planifiée « JARVIS Ollama » peut garder son nom (purement affiché).

## Dépôt GitHub

- Nom actuel : `NardenLaSouris/Jarvis` (remote `origin` des trois copies : PC, `/opt/jarvis`, `~/orion-dev`).
- Nom recommandé : `ORION`.
- Action sur GitHub (vous) : Settings -> General -> Repository name -> `ORION` -> Rename. GitHub redirige l'ancienne
  adresse ; les clones continuent de fonctionner, mais il vaut mieux les mettre à jour.
- Ensuite, dans chaque copie (PC, mini-PC `/opt/orion` ou `/opt/jarvis`, `~/orion-dev`), avec le nom choisi :

      git remote set-url origin https://github.com/NardenLaSouris/ORION.git
      git remote -v

- Conséquences : aucune intégration continue ni badge dans le dépôt ; aucune adresse GitHub dans la documentation.
  Ne pas créer ensuite un nouveau dépôt « Jarvis » : la redirection de l'ancienne adresse cesserait.

## Retour arrière du dépôt

Le changement de nom tient en un commit : `git revert <commit>` le défait sans réécrire l'historique. Les machines non
migrées n'en dépendent pas (renvois et anciens noms de secrets).
