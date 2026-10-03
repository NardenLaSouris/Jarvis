# JARVIS V1 — assistant vocal local

« Jarvis » → « Oui, monsieur ? » → question → transcription → LLM local → réponse vocale
→ quelques secondes d'écoute pour une relance → retour en veille.

Tout tourne localement : openWakeWord (wake word), faster-whisper (STT), Ollama (LLM),
Piper (TTS). La V1 ne fait que converser : aucune action sur le système.

## Architecture

```
jarvis/
  __main__.py          point d'entrée (CLI)
  config.py            configuration centrale (lit config.toml)
  interfaces.py        contrats : AudioSource, AudioSink, WakeWordDetector, SpeechToText, LanguageModel, TextToSpeech
  agent.py             cœur : machine à états veille → écoute → réflexion → réponse → relance → veille
  factory.py           assemble les moteurs concrets à partir de la config (seul endroit qui les connaît)
  prompts.py           prompt système (persona, capacités disponibles / prévues)
  capabilities/        point d'extension pour les futurs outils (vide en V1)
  audio/
    devices.py         micro et haut-parleur (sounddevice)
    files.py           source/sortie WAV pour tests et diagnostic
    endpointing.py     détection début/fin de phrase (énergie, seuil adaptatif)
    resample.py
  wakeword/openwakeword.py   inférence openWakeWord en ONNX (sans scipy/scikit-learn)
  stt/faster_whisper.py
  llm/ollama.py        client HTTP Ollama (bibliothèque standard)
  tts/piper.py
wakeword_training/            entraînement d'un wake word personnalisé (hors exécution)
  specs/jarvis_fr.toml        description du wake word « Jarvis » français
scripts/download_models.py    télécharge les modèles définis dans config.toml
tests/                        tests unitaires, wake word, et bout en bout sur scenario.wav
```

L'agent ne dépend que de `interfaces.py`. Pour changer de moteur, ajouter un module et
le brancher dans `factory.py`. Pour ajouter une capacité, créer un module dans
`capabilities/` et l'enregistrer dans le `CapabilityRegistry` : le prompt système la
présentera automatiquement au LLM.

## Installation (mini-PC Linux, Debian/Ubuntu)

```bash
sudo apt install python3-venv libportaudio2
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b          # ou tout modèle adapté au matériel, à reporter dans config.toml
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/download_models.py
```

Sous Windows : `python -m venv .venv`, puis `.venv\Scripts\...` à la place de `.venv/bin/...`.

Sur le mini-PC, JARVIS tourne en service utilisateur systemd (démarrage automatique, relance en cas
d'arrêt inattendu) : voir l'en-tête de `deploy/jarvis.service` pour l'installer ;
`systemctl --user restart jarvis` après une mise à jour, `journalctl --user -u jarvis -f` pour le journal.

## Utilisation

```bash
python -m jarvis                    # lancement normal
python -m jarvis --list-devices     # périphériques audio (pour [audio] input_device / output_device)
python -m jarvis --wake-test        # affiche en direct le score du wake word pour régler le seuil
python -m jarvis --input-wav f.wav --output-dir out/   # simule le micro avec un fichier
python -m jarvis --tts-test "Bonjour monsieur."        # voix de JARVIS sur la sortie audio configurée
```

## Réponse en flux (LLM → TTS → audio)

JARVIS commence à parler dès que la première phrase de la réponse est prête, pendant qu'Ollama
génère la suite (`jarvis/streaming.py`) :

```
Ollama (stream) -> SentenceBuffer -> file de phrases -> thread TTS -> file audio -> AudioSink
   thread « llm-phrases »                               thread « tts »          thread principal
```

- **`SentenceBuffer`** accumule les fragments du LLM. Il ne rend que des phrases complètes, terminées
  par `. ! ? ; …`, d'au moins 12 caractères, sans couper sur « M. », « etc. » ni sur « 2.5 ».
- **Filtrage phrase par phrase** par le routeur (`ReplyFilter`) : formules d'entrée creuses,
  salutations non sollicitées, relances de fin, « monsieur » au plus une fois. Une fausse action
  prétendue **coupe la génération**.
- **Trois étapes en parallèle** : le thread LLM remplit la file de phrases, le thread TTS la
  synthétise, et le thread principal joue l'audio dans l'ordre. Chaque phrase est synthétisée puis
  jouée une seule fois, sans superposition.
- **Réponses courtes et prédéfinies** : même chemin, avec une seule phrase, sans attente superflue.
- **Interruption** : `SpeechPipeline.cancel()` arrête la génération (le flux Ollama est fermé), vide
  les files et n'en joue plus rien. C'est prêt pour un futur « Jarvis, stop ».
- **Voix Piper** : chargée et préchauffée au démarrage, jamais rechargée entre deux phrases. Le TTS
  reste interchangeable : ElevenLabs fonctionne aussi avec ce pipeline (`[tts] engine`), avec une
  requête par phrase.

Métriques affichées à chaque réponse : `STT`, `LLM first token`, `LLM first sentence`,
`TTS first sentence`, `Audio first chunk`, `LLM total`, `TTS total` et `Total response`.

## Personnalité et réponses prédéfinies

Tout se règle dans `personality.toml`, sans toucher au code :
- **Identité** : nom de l'assistant, prénom et titre de l'utilisateur (« monsieur »), ton, humour,
  exemples de style.
- **Phrases** : réponses au wake word, phrases d'indisponibilité, réponse si le LLM est injoignable.
- **Intentions prédéfinies** : formulations reconnues et variantes de réponse.

Chaque demande transcrite passe par le routeur (`jarvis/router.py`), dans cet ordre :

1. **commandes critiques** : « stop », « tais-toi »…, et retour en veille ;
2. **intentions prédéfinies** : salutations, remerciements, identité, nom, créateur, capacités,
   au revoir. Réponse immédiate, sans LLM ;
3. **capacités** (outils) enregistrées dans `jarvis/capabilities` : aucune en V1 ;
4. **intentions de repli** : demande d'action vers une fonction pas encore disponible
   (« éteins la lumière », « envoie un message »…). Elles passent après les capacités, pour qu'une
   capacité ajoutée plus tard soit prioritaire ;
5. **LLM** (Ollama) : pour tout le reste. Son prompt est construit à partir de la personnalité et des
   capacités réelles.

**Contexte de conversation.** Une intention peut n'être reconnue que juste après une autre
(`after`). Par exemple, « Moi ça va » après « Comment allez-vous ? » donne « Parfait. ». Ce contexte
repart de zéro à chaque réveil par le wake word. Le prompt indique aussi au LLM si la conversation
est déjà engagée, pour qu'il ne resalue pas.

**Identité.** `assistant_name`, `user_name` et `user_title` sont la source de vérité : elles alimentent
les réponses prédéfinies (« Qui es-tu ? », « Qui est Jules ? ») et un bloc d'identité explicite dans le
prompt. Le prénom de l'utilisateur n'apparaît que lorsque la conversation porte sur lui.

Chaque demande produit une ligne de log `[routing]` : `predefined:identity`, `critical:stop`,
`unavailable:unavailable_home`, `capability:…` ou `llm`. Une ligne `[timing] LLM` n'apparaît que si le
LLM a réellement été sollicité.

**Tolérance aux erreurs du STT** (`[matching]` dans `personality.toml`) :
- Le texte est normalisé : minuscules, accents, apostrophes, ponctuation, espaces.
- Des **réécritures** explicites corrigent les confusions fréquentes (« qui est-tu » → « qui es-tu »,
  « t'es qui » → « tu es qui »).
- Pour les phrases de 6 mots au plus, une **petite faute par mot** est tolérée (`fuzzy_threshold`),
  sauf sur les mots porteurs de sens (`strict_words` : tu, ton, son, qui…). Ainsi, « présante-toi » est
  reconnu, mais « Quel est son nom ? » n'est pas pris pour « Quel est ton nom ? ».

**Garde-fous sur les réponses du LLM :**
- Une réponse qui prétend avoir agi sans qu'aucun outil n'ait été exécuté (« La lumière est éteinte »,
  « Je m'en occupe »…) est remplacée par une phrase d'indisponibilité.
- Une formule d'entrée creuse (« Bien sûr, monsieur. », « Je suis heureux de vous aider. ») est
  retirée quand la vraie réponse suit (liste `filler_openings`).
- Une salutation d'ouverture (« Bonjour, monsieur. ») est retirée, sauf si l'utilisateur vient de saluer
  (`greeting_openings`).
- Une relance creuse en fin de réponse (« N'hésitez pas à me demander. ») est retirée (`filler_closings`).
- « monsieur » est conservé au plus une fois par réponse.
- Une réponse coupée par la limite de longueur est ramenée à sa dernière phrase complète.

Les variantes sont tirées au hasard, sans jamais répéter deux fois de suite la même. Les réponses
peuvent utiliser `{title}`, `{assistant_name}`, `{salutation}` (Bonjour/Bonsoir selon l'heure),
`{available}` et `{planned}`.

```bash
python tests/test_personality.py
```

## Outils (V2) : contrôle du PC

JARVIS agit sur le PC via des outils explicitement enregistrés, jamais via un shell. Le LLM ne fait que
proposer un appel structuré ; JARVIS le valide, applique le niveau de risque de l'outil et rapporte le
résultat réel (état relu après l'action).

| Outil | Rôle | Risque |
|---|---|---|
| `get_time`, `get_date` | heure, date | SAFE |
| `system_info` | système, processeur, mémoire, disque, carte graphique, durée d'allumage | SAFE |
| `open_url` | ouvre une page http(s) dans le navigateur par défaut | SAFE |
| `open_application` | ouvre une application autorisée | SAFE |
| `list_running_applications` | lesquelles des applications autorisées sont ouvertes | SAFE |
| `set_volume` | volume du système de 0 à 100 % (relu après réglage) | SAFE |
| `mute_volume`, `unmute_volume` | coupe / remet le son | SAFE |
| `close_application` | ferme une application autorisée | CONFIRMATION_REQUIRED |
| `lock_pc` | verrouille la session (`LockWorkStation`) | CONFIRMATION_REQUIRED |

Code : `jarvis/tools/system.py`, `applications.py`, `audio.py` (assemblés par `builtin.py`).

```
demande -> routeur -> LLM : appel JSON contraint par le schéma de chaque outil -> validation stricte
        -> registre -> permissions (SAFE : exécuté ; à confirmer : question ; RESTRICTED : refusé)
        -> exécution (délai max) -> résultat réel -> LLM -> Piper
```

- Applications : seule la section `[tools.applications]` de `config.toml` fait foi. Les applications du
  catalogue (discord, steam, chrome, spotify, vscode, notepad) sont trouvées automatiquement (registre
  Windows « App Paths », emplacements habituels, Microsoft Store) ; `executable = "..."` impose un chemin ;
  une application hors catalogue demande `executable` (chemin absolu) et `process`.
- Le LLM ne fournit jamais de chemin, de commande, de PID ni de nom de processus.
- Un nombre (volume) proposé par le LLM doit figurer dans la demande : « monte un peu le son » ne règle rien.
- `confirm = true` peut ajouter une confirmation à un outil SAFE ; la confirmation d'un outil qui l'exige
  ne peut pas être retirée.
- JARVIS n'annonce une action réussie que si l'outil a réellement réussi (sinon : message d'erreur).
- Chaque exécution est journalisée (`outil {...}` : outil, paramètres, décision, confirmation, succès, durée).
- Transcription : Whisper reçoit le vocabulaire attendu (`[stt] vocabulary_hint`), puis
  `jarvis/stt/correction.py` corrige une commande courte mal entendue (« ou vos teams » -> « ouvre steam »).

```bash
python -m pytest tests/test_tools.py -q      # simulé : rien n'est réellement lancé, fermé, réglé ni verrouillé
```

### Minuteurs et rappels

« Mets un minuteur de 10 minutes », « Rappelle-moi dans 20 minutes de sortir le linge », « Annule mon
minuteur », « Quels rappels sont prévus ? » : outils SAFE `create_timer`, `cancel_timer`, `list_timers`,
`create_reminder`, `cancel_reminder`, `list_reminders`.

- Le LLM transmet la durée telle qu'elle a été dite ; `jarvis/scheduling/durations.py` la convertit
  (« 1 heure 30 », « une demi-heure », « trois quarts d'heure »...) et vérifie qu'elle figure dans la demande.
- `TimerManager` (`jarvis/scheduling/`) : un seul fil pour toutes les échéances, arrêté avec JARVIS ;
  publie `timer.created / cancelled / finished` et `reminder.*` sur le bus (journal d'activité inclus).
- À l'échéance, une notification est créée (phrase `timer_finished` / `reminder_finished` de
  `personality.toml`, sans LLM) et JARVIS la prononce dès qu'il est libre, en veille comme en conversation.

### Météo

« Quel temps fait-il ? », « Il va pleuvoir ce soir ? », « Quel temps fera-t-il à Lyon demain ? » : outil SAFE
`get_weather(location?, day?, moment?)` (jour : today / tomorrow / day_after_tomorrow ; moment : now / morning /
afternoon / evening / day), dates calculées sur l'horloge du système.

- Fournisseur : [Open-Meteo](https://open-meteo.com) — gratuit pour un usage personnel non commercial, sans clé
  ni compte, recherche de ville intégrée. Données météo : Open-Meteo.com, licence CC-BY 4.0.
- `jarvis/weather/` : modèles internes (°C, km/h, mm), interface `WeatherProvider` (changer de fournisseur =
  une nouvelle classe), `OpenMeteoProvider`, `WeatherService` (ville par défaut, périodes, cache court,
  événements `weather.requested / received / failed`, journalisés).
- Une ville n'est utilisée que si elle a été dite ; sinon, `[weather] default_location` (cherchée par son nom,
  ou placée exactement avec `latitude` / `longitude`). `temperature_unit` : `celsius` (défaut) ou `fahrenheit`.
- Cache : `current_cache_minutes` (temps actuel) et `forecast_cache_minutes` (prévisions) ; une donnée périmée
  est retirée et redemandée au fournisseur. Aucune clé ni variable d'environnement n'est nécessaire.
- Si la météo est indisponible, JARVIS le dit simplement (jamais de météo inventée) ; « cherche / recherche
  la météo » passe par la recherche Web.

```bash
python scripts/weather_check.py Lyon     # appel réel à Open-Meteo (manuel, hors tests automatiques)
```

### Notifications

```
événement (timer.finished, reminder.finished...) -> EventNotifications -> Notification (titre, message,
source, priorité LOW / NORMAL / HIGH) -> NotificationManager -> canaux actifs qui l'acceptent -> voix (file FIFO)
```

`jarvis/notifications/` : les fonctionnalités publient seulement leurs événements ; `EventNotifications`
les traduit en notifications, `NotificationManager` les remet aux canaux (un canal en panne est journalisé
sans bloquer les autres). Le canal vocal les prononce une à une avec le TTS de JARVIS. Un futur canal
(bureau, téléphone...) hérite de `NotificationChannel` (`send`, `start`, `stop`, priorité minimale) et
s'enregistre auprès du gestionnaire. Événements : `notification.created / sent / failed` (journal
d'activité) et `notification.started / finished`. `[notifications] voice_enabled = false` coupe les annonces.
- En mémoire : un redémarrage efface les échéances. Limites dans `[timers]` (durée maximale, nombre).

### Événements et journal d'activité

Les composants communiquent par un bus d'événements interne (`jarvis/events.py`) : `publish(Event)`,
`subscribe(type, handler)` (ou `"*"` pour tout recevoir), désabonnement. Un événement a un `type`
(`domaine.action`), une `source`, un `payload` et un horodatage ; un abonné en erreur est journalisé
sans bloquer les autres ni JARVIS.

Chaque demande d'outil publie `tool.started` puis `tool.executed` ou `tool.failed` (outil, étape,
décision, confirmation, erreur, durée, paramètres nettoyés ; jamais le résultat brut). Le visage écoute
`tool.started` ; le journal d'activité (`jarvis/activity.py`, section `[activity]`) enregistre
`tool.executed` et `tool.failed` dans `data/activity.jsonl` :

```bash
python -m jarvis --activity        # [14:32:01] tool.executed — open_application ...
```

### Test manuel (vraies actions)

Lancer `python -m jarvis`, puis dire « Jarvis… » avant chaque phrase et vérifier :

1. « Ouvre Discord » : Discord s'ouvre, JARVIS le confirme (sans question).
2. « Ouvre YouTube » : la page s'ouvre dans le navigateur par défaut.
3. « Mets le volume à 30 % » : le volume Windows passe à 30 (icône du son).
4. « Coupe le son », puis « Remets le son » : le son est coupé puis rétabli.
5. « Donne-moi les informations de cette machine » : processeur, mémoire, carte graphique réels.
6. « Ferme Discord » : JARVIS demande « Voulez-vous que je ferme Discord ? » ; « Oui » : Discord se ferme
   (« Non » : rien ne se passe).
7. « Verrouille le PC » : JARVIS demande confirmation ; « Oui » : l'écran de verrouillage s'affiche.

Dans le terminal, chaque action laisse une ligne `outil {...}` avec son résultat.

## Visage graphique

Au lancement, JARVIS ouvre son visage animé dans le navigateur (`http://127.0.0.1:8765/`) : anneaux
holographiques dessinés en direct (canvas, HTML/CSS/JS sans dépendance), qui suivent son état.

| JARVIS | Visage |
|---|---|
| en veille (`sleep`) | STANDBY : rotations lentes, faible lumière, pulsation lente du noyau |
| wake word, écoute, STT | LISTENING : plus lumineux, anneaux internes plus rapides, pulsation régulière |
| demande comprise, routage, recherche Web | THINKING : rotations rapides, segments en mouvement, particules vers le centre |
| lecture audio | SPEAKING : noyau et anneaux réagissent au niveau réel de la voix |

Le niveau audio est mesuré sur les échantillons réellement envoyés au haut-parleur (`MeteredSink`),
sans toucher à Piper. Le visage est non critique : port pris, page fermée ou erreur, JARVIS
continue en vocal. Réglages dans `[face]` de `config.toml` (`host = "0.0.0.0"` pour une tablette
ou un écran mural du réseau local).

```bash
python -m jarvis                  # JARVIS + visage
python -m jarvis --no-face        # sans visage
python -m jarvis --face-demo      # défilement des états, parole avec la vraie voix
```

Dans le navigateur : `?demo=1` (démo autonome, touches 1-4, T = thème), `?debug=1` (état et FPS),
`?theme=day|night` (thème imposé). API JS : `JarvisFace.setVisualState("thinking")`,
`JarvisFace.setAudioLevel(0.4)`, `JarvisFace.standby()`, `JarvisFace.setTheme("night")`.

**Thème nuit.** Entre `night_start` et `night_end` de `[face]` (22:00 et 07:00 par défaut), le même
visage passe en noir et blanc. L'heure est celle du Core : toutes les pages changent ensemble.

**Sur tous les appareils.** Avec `[face] host = "0.0.0.0"` sur le Core, le visage s'ouvre depuis
n'importe quel appareil du réseau local (`http://192.168.1.91:8765/` : PC, tablette, téléphone),
plusieurs à la fois. L'interface est en lecture seule. L'agent Windows l'ouvre de lui-même sur le PC
quand le Core se connecte (`[face] open_on_connect` de `windows_agent.toml`).

## Lumières (ampoules Tuya / LSC Smart Connect, en local)

« Allume la chambre à 30 % », « éteins les lumières », « mets l'entrée en vert », « remets la chambre en blanc
chaud », « la chambre à 4000 kelvins » : outils `light_on`, `light_off`, `light_toggle`, `set_brightness`,
`set_color`, `set_color_temperature`, exécutés par le Core en local (TinyTuya, sans cloud). La pièce vient des
mots de la demande (alias ; « les lumières » = toutes), jamais du LLM ; sans pièce reconnue, JARVIS demande
laquelle. Réponses sans LLM : « La lumière de la chambre est allumée à 30 %. »

Réglages dans `[lights]` (voir `config.toml`) : une pièce par ampoule dans `config.local.toml` (nom, device_id,
ip, version, alias), clé locale de chaque ampoule dans `.env` (`LIGHT_KEY_<PIÈCE>`), jamais dans Git. Les clés
s'obtiennent une fois depuis un compte Smart Life ; une IP fixe par ampoule (bail réservé dans la box) est
conseillée.

## Agent Windows (contrôle et audio du PC à distance)

Quand le Core JARVIS tourne sur le mini-PC, le PC Windows peut exécuter des actions à sa demande via
un petit agent HTTP du réseau local (`jarvis/winagent/`). Il n'expose que des actions explicitement
enregistrées, validées comme les outils du Core : aucune commande libre, aucun shell.

| Point d'entrée | Accès |
|---|---|
| `GET /health` → `{"status": "ok", "agent": "jarvis-windows"}` | IP autorisée |
| `POST /actions/<nom>` avec `{"parameters": {...}}` | IP autorisée + `Authorization: Bearer <jeton>` |
| `GET /audio/input?rate=16000&frame=1280` : flux continu du micro (PCM int16 mono) | idem |
| `POST /audio/output?rate=22050` (PCM int16 mono) puis `POST /audio/drain` : voix jouée sur le PC | idem |

Toute IP absente de `allowed_ips` reçoit 403, avant toute autre vérification. Aucune action n'est
encore enregistrée. Le micro n'est ouvert que pendant qu'un Core est connecté, et par un seul à la fois.

Réglages dans `windows_agent.toml` (adresse, port, IP autorisées, micro et sortie du PC). Le jeton partagé n'est jamais dans
le dépôt : `JARVIS_AGENT_TOKEN` dans l'environnement ou dans `.env` (32 caractères minimum, le même
côté Core). L'agent refuse de démarrer sans jeton ou sans IP autorisée.

Lancement sous Windows (PowerShell, à la racine du projet) :

```powershell
python -m venv .venv                          # une seule fois
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"   # génère un jeton
notepad .env                                  # ajouter la ligne JARVIS_AGENT_TOKEN=<jeton généré>
.venv\Scripts\python -m jarvis.winagent        # Ctrl+C pour arrêter ; -v pour le détail des requêtes
```

Le même jeton va dans le `.env` du Core (mini-PC). Le chemin des fichiers se change avec
`--config` et `--env`.

Démarrage automatique à l'ouverture de session, sans fenêtre (journal : `data\winagent.log`,
aucun droit administrateur) :

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1           # installer et démarrer
powershell -ExecutionPolicy Bypass -File scripts\install_winagent_startup.ps1 -Remove   # désinstaller
```

Ouvrir le port uniquement pour le Core (PowerShell administrateur, une seule fois) :

```powershell
New-NetFirewallRule -DisplayName "JARVIS Agent (TCP 8765)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress 192.168.1.91 -Profile Private,Domain
```

La règle vaut pour les réseaux « Privé » ou « Domaine » : si la connexion Windows est en « Public »,
passez-la en « Privé » (Paramètres > Réseau) plutôt que d'élargir la règle.

Test depuis le mini-PC : `curl http://192.168.1.128:8765/health`.

**Audio du PC, JARVIS sur le mini-PC.** Dans le `config.toml` du mini-PC :
`[audio] remote = "http://192.168.1.128:8765"` (et `JARVIS_AGENT_TOKEN` dans son `.env`). JARVIS
écoute alors le micro du PC et lui parle, à la place des périphériques locaux. Si le PC est éteint
ou l'agent arrêté, JARVIS attend et se reconnecte seul ; la voix qui ne peut être jouée est ignorée,
sans erreur. Le flux micro (~256 kbit/s) circule en clair sur le réseau local, réservé au Core.

Le visage écoute aussi sur 8765 (en local) : sur un même PC, l'agent et un Core avec visage
doivent utiliser des ports différents.

## Recherche Web

Pour les questions qui demandent une information actuelle (prix, dernière version, actualités,
« actuel », « en ce moment », « cherche-moi… »), le routeur choisit l'intention `web.search` :

```
STT -> routeur (web.search) -> SearXNG -> résultats structurés -> sélection des plus pertinents
    -> bloc de données NON FIABLES dans le message utilisateur -> Ollama -> réponse -> Piper
```

- Moteur interchangeable (`jarvis/web/base.py`, `WebSearchProvider` : `search`, `fetch`, `extract`),
  implémentation SearXNG dans `jarvis/web/searxng.py`.
- Résultats structurés (`title`, `url`, `snippet`, `source`, `position`, `published_at`), dédoublonnés ;
  seuls les 3 plus pertinents et un extrait de la meilleure page vont au LLM, sans URL.
- Pages : adresses http(s) publiques uniquement (jamais le réseau local), redirections vérifiées,
  délai et taille bornés, texte extrait sans scripts ni styles ; aucun code de page n'est exécuté.
- Tout contenu Web est traité comme une donnée non fiable, encadré par des balises et jamais placé
  dans le prompt système : une page qui dit « ignore tes instructions » n'est qu'un texte trouvé.
- Les URL ne sont jamais lues à voix haute ; les sources restent disponibles (`agent.last_sources`).
- SearXNG injoignable, aucun résultat, `[web] enabled = false` : JARVIS le dit simplement ou garde
  son comportement habituel.

Configuration dans `[web]` de `config.toml`. L'instance SearXNG doit autoriser le format JSON
(`search: formats: [html, json]` dans son `settings.yml`).

Sur le mini-PC (Ubuntu), une seule commande installe Docker, crée la clé et démarre SearXNG
(relançable sans risque) : `sudo bash scripts/install_docker.sh`.

SearXNG local (Docker, même procédure sur le PC et sur le mini-PC), accessible seulement depuis la
machine (`127.0.0.1:8080`). Réglages dans `searxng/config/settings.yml` ; la clé secrète, propre à
chaque machine, va dans `searxng/.env` (non versionné) :

```bash
python -c "import secrets; print('SEARXNG_SECRET=' + secrets.token_hex(32))" > searxng/.env
docker compose -f searxng/docker-compose.yml up -d        # démarrer (redémarre avec Docker)
docker compose -f searxng/docker-compose.yml down         # arrêter
```

Tester :

```bash
python -m jarvis --web-test "Combien coûte une RTX 3060 actuellement ?"   # sans micro ni voix
python -m jarvis --web-test "Quelle est la dernière version de Python ?" -v  # + données envoyées au LLM
python -m pytest tests/test_web.py -q      # sans Internet : moteur et pages simulés
```

## STT sur GPU NVIDIA

Le STT est configuré en `[stt] device = "cuda"` et `compute_type = "float16"`. Sur une RTX 3060,
une phrase de 5 s est transcrite en ~0,3 s, contre ~3 s sur CPU. Il faut la bibliothèque cuBLAS :

```bash
pip install -r requirements-gpu.txt
python -m jarvis --stt-test                  # diagnostic : GPU, device utilisé, temps
python -m jarvis --stt-test phrase.wav       # sur un enregistrement
```

Sans GPU NVIDIA, ou si CUDA échoue, le STT bascule automatiquement sur
`fallback_device` / `fallback_compute_type` (CPU / int8) et l'indique dans les logs.
Au démarrage, JARVIS affiche `STT device`, `STT compute type` et `STT model`.

## Voix ElevenLabs (cloud)

Par défaut, JARVIS parle avec une voix ElevenLabs : `[tts] engine = "elevenlabs"`, voix réglée
dans `[tts.elevenlabs] voice_id`. Chaque phrase prononcée est envoyée à l'API ElevenLabs, ce
qui demande une connexion Internet et une clé API ; le service est payant au-delà du quota
gratuit.

La clé ne doit jamais être écrite dans `config.toml` ni dans `.env.example` :

```bash
copy .env.example .env      # puis mettez la clé dans .env (fichier ignoré par git)
```

Si ElevenLabs est indisponible (pas de clé, pas de réseau, quota épuisé, erreur de l'API),
JARVIS bascule automatiquement sur la voix locale Piper (`fallback_to_piper = true`). Pour
revenir au tout-local : `engine = "piper"`.

```bash
python -m jarvis --tts-test "Bonjour monsieur. Je suis Jarvis."   # teste la voix configurée
```

## Choisir la voix Piper

Pour comparer plusieurs voix Piper à l'oreille, avec exactement le même chemin TTS → sortie audio
que JARVIS :

```bash
python -m jarvis --tts-voices-test                                   # candidats par défaut
python -m jarvis --tts-voices-test fr_FR-gilles-low fr_FR-upmc-medium:pierre
python -m jarvis --tts-test "Bonjour monsieur" --tts-voice fr_FR-upmc-medium:pierre
python -m jarvis --tts-voices-test --no-pronunciations               # sans le lexique (voir plus bas)
```

- **Candidats par défaut** (voix masculines) : `fr_FR-tom-medium`, `fr_FR-gilles-low` et
  `fr_FR-upmc-medium:pierre`.
- **Phrases jouées** : les quatre mêmes pour chaque voix. La dernière, « Jarvis. Bonjour monsieur.
  Je vais vous assister. », sert à vérifier le « s » final de Jarvis.
- **Logs** : pour chaque phrase, la voix, les phonèmes, la génération et la lecture.
- **Voix multi-locuteurs** : elles s'écrivent `voix:locuteur`, par exemple `fr_FR-upmc-medium:pierre`
  ou `:jessica`.
- **Voix absente de `models/piper/`** : le test indique la commande de téléchargement, par exemple :

```bash
python scripts/download_models.py --voice fr_FR-gilles-low --voice fr_FR-upmc-medium
```

Pour adopter une voix, modifiez `[tts]` dans `config.toml` :

```toml
[tts]
voice = "models/piper/fr_FR-upmc-medium.onnx"
speaker = "pierre"          # vide pour une voix à un seul locuteur
```

Voix françaises Piper disponibles :

| voix | locuteurs | remarques |
|---|---|---|
| `fr_FR-tom-medium` | homme | 44,1 kHz |
| `fr_FR-gilles-low` | homme | 16 kHz, qualité « low », parfois instable sur les textes courts |
| `fr_FR-upmc-medium` | `pierre` (homme), `jessica` (femme) | |
| `fr_FR-siwis-medium` | femme | |
| `fr_FR-mls-medium` | 125 locuteurs sans nom (hommes et femmes) | souvent incompréhensible sur les phrases courtes |
| `fr_FR-mls_1840-low` | femme | peu intelligible |

**Prononciation de « Jarvis ».** espeak-ng, qui convertit le texte en phonèmes pour toutes les voix
Piper, applique la règle française du « s » muet : « Jarvis » devient /ʒaʁvi/, quelle que soit la
voix. La section `[tts.pronunciations]` de `config.toml` impose des phonèmes pour certains mots,
sans changer leur orthographe :

```toml
[tts.pronunciations]
Jarvis = "ʒaʁvˈis"
```

Ce lexique est appliqué à tout texte prononcé, y compris les réponses du LLM, en conservant la
ponctuation qui suit le mot. On peut y ajouter d'autres mots mal prononcés.

## Réglages utiles (`config.toml`)

Les réglages propres à une machine (modèle, périphériques, adresse de l'agent…) vont dans
`config.local.toml`, à côté de `config.toml` et non versionné : chaque réglage présent y remplace
celui de `config.toml`, ce qui laisse `git pull` sans conflit. Exemple pour le mini-PC :

```toml
[llm]
model = "qwen2.5:3b"

[audio]
remote = "http://192.168.1.128:8765"

[face]
host = "0.0.0.0"
open_browser = false
```

**Agir sur les PC du réseau.** Avec `[tools.devices]` (voir l'exemple dans `config.toml`), les outils qui
agissent sur un PC (applications, pages Web, volume, verrouillage, description de la machine) sont confiés
à l'agent Windows de l'appareil visé ; le Core n'agit jamais sur sa propre machine. L'appareil vient des mots
de la demande (« ouvre Discord sur mon PC portable » ; l'alias le plus long l'emporte), sinon l'appareil
par défaut ; un appareil sans `url` (le mini-PC) est refusé avant toute confirmation. Le LLM ne choisit jamais
l'appareil. Chaque agent a ses réglages propres dans `windows_agent.local.toml` (non versionné).

**LLM sur une autre machine.** `[llm] host` peut viser un worker GPU du réseau (Ollama écoutant sur le LAN),
avec `fallback_host` / `fallback_model` pour un LLM de secours (par exemple Ollama en local) : avant chaque
appel, JARVIS vérifie en moins d'une seconde que le worker répond, sinon il bascule aussitôt sur le secours
et retente le worker 30 s plus tard. Les deux sont préchargés au lancement.

- `[llm] model` : modèle Ollama ; sur CPU seul, préférer un 3-4B.
- `[stt] model` : `small` (précis, ~2 s par phrase sur Ryzen 7) ou `base` (~0,7 s, moins fiable).
- `[wake_word] threshold` : 0,96 d'après l'évaluation (voir « Wake word ») ; à ajuster avec `--wake-test`.
- `[assistant] conversation_timeout` : durée d'écoute sans wake word après une réponse.
- `[audio] min_rms`, `speech_to_noise_ratio`, `end_of_speech_silence` : détection de parole.

## Wake word

### Moteur et modèle

- **Moteur :** openWakeWord (inférence ONNX locale, `jarvis/wakeword/openwakeword.py`).
  Deux étages : un extracteur générique (spectrogramme mel → embeddings, commun à tous
  les mots) et un petit **classifieur propre au mot**.
- **Modèle :** `models/openwakeword/jarvis_fr.onnx`, un classifieur entraîné dans ce dépôt
  pour « Jarvis » **prononcé à la française**. Il est versionné avec le projet ; ses
  mesures détaillées sont dans `models/openwakeword/jarvis_fr.json`.
- Le modèle pré-entraîné `hey_jarvis` d'openWakeWord ne convient pas : il a appris la
  prononciation anglaise de « hey jarvis ». En français, les scores plafonnent autour de 0,1.

### Tester le wake word

```bash
python -m jarvis --wake-test
```

L'outil affiche en direct le score, le seuil et chaque détection. Prononcez « Jarvis »
plusieurs fois, près du micro puis plus loin, et parlez normalement entre deux essais
pour vérifier qu'il ne se déclenche pas.

### Changer de wake word

Le code ne contient aucune référence au mot : tout passe par `config.toml`.

```toml
[wake_word]
phrase = "Jarvis"                              # mot affiché
model = "models/openwakeword/jarvis_fr.onnx"   # classifieur de ce mot
threshold = 0.5                                # issu de l'évaluation (voir jarvis_fr.json)
```

Pour un autre mot, entraînez un nouveau classifieur (voir la section suivante), puis
changez `phrase`, `model` et `threshold`.

### Entraîner un wake word

Les outils sont dans `wakeword_training/` et ne servent pas à l'exécution de JARVIS.
Chaque mot est décrit par un fichier TOML (`wakeword_training/specs/jarvis_fr.toml`) :
- voix à utiliser ;
- graphies du mot ;
- phrases porteuses ;
- motif de vérification ;
- mots pièges ;
- réglages d'augmentation et d'entraînement.

```bash
pip install -r requirements-train.txt          # ajoute seulement le paquet onnx
python -m wakeword_training all                 # télécharge, génère, entraîne, évalue
# ou étape par étape : download, generate, features, train, evaluate
python -m wakeword_training all --spec wakeword_training/specs/mon_mot.toml
```

1. **download** : voix françaises Piper (~130 locuteurs) et ~11 h d'embeddings négatifs
   publiés par openWakeWord (parole, musique, bruits). Environ 520 Mo dans `data/`, hors git.
2. **generate** : synthèse des exemples positifs et négatifs.
   - Les voix Piper sont instables sur un mot isolé. Le mot est donc aussi prononcé en
     fin de phrase longue, puis découpé grâce à l'alignement des phonèmes.
   - **Chaque « Jarvis » et chaque mot piège est vérifié par Whisper**, clip par clip. Un
     positif n'est gardé que si l'on y entend le mot ; un mot piège est écarté si on l'y
     entend. Les phrases ordinaires ne sont pas vérifiées : même mal prononcées, elles
     restent des négatifs valables.
   - Une partie des locuteurs est réservée au test.
   - L'étape est reprenable.
3. **features** : augmentation, puis extraction des embeddings par le même code que la
   détection en direct.
   - Bruits : blanc, rose, brun, ronflement électrique, brouhaha de conversations.
   - Distance simulée : réverbération, atténuation, perte des aigus.
   - Hauteur et débit de voix variés.
4. **train** : petit réseau de neurones (numpy), export ONNX au format openWakeWord.
5. **evaluate** : mesures sur des locuteurs jamais vus à l'entraînement, dans 3 conditions
   (calme, bruit, distance), et fausses alertes par heure sur plus de 3 h d'audio sans le mot.
   - Le **seuil recommandé** est le plus bas qui respecte le plafond de fausses alertes
     fixé dans la spécification.
   - `--model` permet de mesurer un autre modèle, par exemple `hey_jarvis`, pour comparer.

**Vos propres enregistrements** améliorent nettement le résultat avec votre voix et votre
pièce. Une prise sur deux sert à l'entraînement, l'autre à l'évaluation.

```bash
python -m wakeword_training record positive   # 30 × « Jarvis », à 3 distances
python -m wakeword_training record negative   # 30 phrases pièges
python -m wakeword_training record ambient    # 2 min d'ambiance de la pièce
python -m wakeword_training features && python -m wakeword_training train && python -m wakeword_training evaluate
```

Des enregistrements d'ambiance supplémentaires (TV, musique…) peuvent être déposés dans
`data/wakeword/noise/*.wav` : ils servent de bruit de fond pendant l'augmentation.

### Résultats mesurés (`models/openwakeword/jarvis_fr.json`)

Mesures sur des voix **jamais entendues à l'entraînement** (`fr_FR-tom`, `fr_FR-upmc` locuteur 1, et
quelques locuteurs `mls`), via le détecteur utilisé en direct :
- 277 « Jarvis » dans 3 conditions ;
- 922 mots pièges prononcés un par un ;
- 26 min de phrases ordinaires ;
- 3,2 h d'audio varié (parole, musique, bruits).

| seuil | calme | bruit (5-15 dB) | distance | fausses alertes/h | mots pièges déclencheurs |
|------:|------:|------:|------:|------:|------:|
| 0,50 | 98,9 % | 90,6 % | 79,4 % | 15,4 | 2,9 % |
| 0,80 | 97,1 % | 84,1 % | 65,7 % | 5,2 | 1,4 % |
| 0,90 | 96,8 % | 79,8 % | 57,4 % | 2,5 | 1,3 % |
| **0,96** | **95,3 %** | **70,0 %** | **45,8 %** | **0,27** | **0,4 %** |

**Seuil retenu : 0,96.** C'est le plus bas qui respecte les deux plafonds de la spécification :
- moins de 0,5 fausse alerte par heure ;
- moins de 3 % de mots pièges déclencheurs.

En dessous, les fausses alertes augmentent vite, surtout sur la parole française continue.

Deux points de lecture :
- **Les fausses alertes par heure sont pessimistes.** Le flux de test enchaîne des phrases sans
  pause, bien plus dense qu'une conversation dans une pièce.
- **Les mots qui déclenchent encore** sont les plus proches : « Service » (8 %) et « Jarry » (3 %).

Pour comparaison, sur les mêmes données, `hey_jarvis` détecte **0 %** des « Jarvis » français
au seuil 0,5.

### Limites connues

- **Les positifs sont synthétiques et viennent de peu de voix.** Les voix Piper françaises
  fiables sont rares : `siwis`, `gilles` et `upmc` pour l'entraînement.
  - La voix `mls` (125 locuteurs) ne prononce correctement « Jarvis » que dans ~14 % des
    essais : seuls ces essais, validés par Whisper, sont gardés.
  - La détection sur **votre** voix n'est donc pas garantie. Vérifiez-la avec `--wake-test` ;
    si elle est insuffisante, enregistrez vos échantillons (ci-dessus) et réentraînez :
    c'est le levier le plus efficace.
- **La détection à distance est faible au seuil retenu** (46 % en simulation). Si JARVIS est
  loin de vous, baissez le seuil, par exemple à 0,8, en acceptant davantage de fausses alertes.
- La distance au micro et le bruit sont simulés, pas enregistrés dans votre pièce.
- **La vérification Whisper est lente sur CPU** (~5 s par clip). Si une carte NVIDIA est
  présente et que cuBLAS 12 est dans le `PATH`, elle passe automatiquement sur GPU, environ
  10 fois plus vite. cuBLAS 12 s'obtient avec `pip install nvidia-cublas-cu12`, le CUDA Toolkit,
  ou le dossier `lib/ollama/cuda_v12` d'Ollama sous Windows. Comptez ~30 min sur GPU pour
  l'étape `generate`, plusieurs heures sur CPU.
- Un mot très proche, comme « Jervis », peut déclencher : c'est volontaire, pour ne pas
  rejeter les prononciations naturelles de « Jarvis ».

## Tests

```bash
python tests/test_units.py            # rapides, sans modèle ni matériel
python tests/test_wakeword.py         # wake word : config, chargement, flux, WAV de référence
python tests/test_pipeline_e2e.py     # pipeline complet sur tests/fixtures/scenario.wav (Ollama requis)
python -m pytest tests/test_winagent.py   # agent Windows (serveur local 127.0.0.1, aucun accès réseau)
```

Les fichiers de référence sont régénérables :
- `tests/fixtures/wakeword/` avec `python tests/make_wakeword_fixtures.py` (locuteurs de test uniquement) ;
- `tests/fixtures/scenario.wav` avec `python tests/make_scenario.py`.
"# Jarvis" 
