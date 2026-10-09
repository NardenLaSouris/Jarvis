# ORION V2 — assistant vocal local (univers Aureon)

ORION (anciennement JARVIS) : même assistant, nouvelle identité. Le nom, la voix, le visage (vert forêt, crème et or
d'Aureon), ORION Control et la documentation disent « ORION ». Restent « jarvis » les identifiants techniques, pour ne
casser aucune machine déjà installée : paquet Python (`python -m jarvis`), service systemd `jarvis`, dossier
`/opt/jarvis`, variables `JARVIS_AGENT_TOKEN` et `JARVIS_CALENDAR_ICS`. « Jarvis » reste compris comme ancien nom
(`former_names` dans `personality.toml`).

**Mot de réveil** : « Orion » (modèle `models/openwakeword/orion_fr.onnx`, spécification
`wakeword_training/specs/orion_fr.toml`). Tant que ce modèle n'est pas entraîné et validé, le réveil reste
« Jarvis » (`[wake_word] model` et `phrase`). La seconde vérification Whisper suit le mot configuré.

« Orion » → « Oui, monsieur ? » → demande → transcription → outil ou LLM → réponse vocale
→ quelques secondes d'écoute pour une relance → retour en veille.

Tout tourne en local, sur trois machines du réseau :

| Machine | Rôle |
|---|---|
| Mini-PC (Core, Linux, sans GPU) | `python -m jarvis` (service systemd utilisateur) : wake word, STT (faster-whisper sur processeur), routage, outils, Piper, routines, mémoire, API d'administration (8766), visage (8765), LLM de secours (Ollama local), SearXNG (Docker, 127.0.0.1:8080) |
| Katana (RTX 4070) | worker LLM : Ollama `qwen2.5:7b` sur GPU (11434, pare-feu : Core seulement) ; agent Windows |
| PC principal | terminal audio (micro et voix relayés par l'agent Windows, 8765) ; actions sur le PC ; ORION Control |

Le LLM ne fait que proposer : tout passe par le Core des outils (validation, permissions par profil,
confirmation, exécution, journal). Aucun shell, aucune commande libre.

## Nouveautés de la V2 (octobre 2026)

- **Worker LLM robuste** : état ONLINE / DEGRADED / OFFLINE, erreurs typées (injoignable, délai dépassé, erreur
  du modèle), sonde de reconnexion toutes les 10 s, métriques de latence (API, `--health`). Katana hors ligne :
  **mode dégradé** — commandes simples exécutées sans LLM, conversation par le LLM local avec un prompt court
  (1 à 2 s au lieu de 70 s), phrase claire sinon. Worker gelé (connexion acceptée, aucune réponse) : secours au
  bout de `first_token_timeout` (10 s), annoncé aussitôt (« Un instant, monsieur. »). Réglages `[llm] connect_timeout`, `fallback_timeout`, `probe_interval`,
  `slow_after`, `retry_after`, `first_token_timeout`.
- **Commandes simples sans LLM** (`jarvis/tools/quick.py`, `[tools] fast_path`) : lumières, son, applications,
  minuteurs, rappels, réveils, météo, musique, agenda, mémoire, routines, verrouillage, et leurs enchaînements
  (« allume la chambre à 30 %, en bleu »). 0,2 ms au lieu de 0,5 à 1,4 s de choix d'outil.
- **Contexte conversationnel** (`jarvis/context.py`) : « Allume la chambre. » → « À 30 %. » → « Non, l'entrée. »
  → « En bleu. » ; « Quelle heure est-il ? » → « Et demain ? ». Oublié au retour en veille.
- **Lumières** : ambiances (`set_scene` : cinéma, lecture, détente, nuit, travail, réveil ; `[lights.scenes]`),
  groupes (`[lights.groups]`), état lu sur l'ampoule (`light_status`), pièce inconnue signalée (« je n'ai pas de
  lumière configurée pour le bureau »), ampoules commandées en parallèle, une ampoule muette n'arrête pas les
  autres, état centralisé de la maison (`jarvis/home.py`, valeurs lues ou seulement commandées).
- **Mémoire explicite** (`jarvis/memory.py`) : `remember`, `recall`, `forget` (avec confirmation) ; rien n'est
  retenu automatiquement ; `data/memory.json`, `python -m jarvis --memory`, `GET/DELETE /api/memory`.
- **Actions programmées** : « Dans 10 minutes, allume la chambre », « Tous les jours à 21 h, mets la lumière à
  30 % », « Éteins tout à 23 h » → routine exécutée par le même Core ; actions à confirmer refusées. Rappels à
  heure précise, aujourd'hui, demain ou après-demain (une autre date renvoie au calendrier) ; « annule-le » vise
  le rappel ou le minuteur qui vient d'être créé ; minuteurs et rappels conservés au redémarrage
  (`[timers] persist`) ; `list_routines`, `run_routine`, `delete_routine`.
- **Calendrier** (`jarvis/agenda.py`) : aujourd'hui, demain, un jour de la semaine (« jeudi », « lundi
  prochain »), une date (« le 12 », « 2 janvier »), prochains, recherche, créneaux libres (aussi « jeudi
  après-midi »), ajout, suppression ; calendrier local + lecture d'un .ics ou d'une adresse iCal secrète (Google, Outlook).
- **Fichiers** (sur le PC, par l'agent) : recherche, lecture de texte, création dans un dossier de travail,
  copie ; déplacement et suppression avec confirmation, suppression récupérable (corbeille d'ORION).
- **Musique** : touches multimédia du PC ; Spotify par son API Web (après `--spotify-login`), joué par ORION
  lui-même : librespot sur le Core (appareil « ORION », `deploy/jarvis-librespot.service`), son relayé vers le PC
  comme la voix, baissé pendant qu'ORION écoute ou parle ; « est-ce que la musique joue ? » lu sur Spotify
  (`spotify_status`). Si l'appareil « ORION » disparaît (librespot perd sa session après des heures
  d'inactivité), ORION relance le service `[spotify] player_service` puis joue.
- **Profils et terminaux** (`jarvis/profiles.py`) : rôles owner, adult, child, guest ; terminal → pièce et
  utilisateur ; par défaut un propriétaire, comportement inchangé.
- **Mails** (`jarvis/mail`, voir « Mails » plus bas) : lecture, tri, recherche, résumé sans rien marquer lu ;
  archiver, supprimer, envoyer, répondre toujours confirmés ; contenu des mails traité comme donnée non fiable.
- **Liens de téléchargement** : `open_url` reste sans confirmation, mais n'ouvre jamais un lien qui télécharge
  directement un programme (.exe, .msi, .ps1, .bat...).
- **Réseau** : `network_status` ; sources des recherches Web journalisées et citées sur demande.
- **Diagnostics** : `python -m jarvis --health`, `--diagnostics`, `--benchmark` (non destructifs).
- **Tests silencieux** : garde-fous contre tout son, navigateur, ampoule ou touche réels (`tests/conftest.py`).
- **Données abîmées** (`jarvis/persist.py`) : un `data/*.json` illisible (routines, souvenirs, échéances,
  calendrier local) est renommé `<nom>.illisible-<date>` — jamais écrasé ni supprimé — et ORION démarre à vide.
- **Garde-fous du choix d'outil** : une action doit être nommée par la demande (une lumière par un mot de
  l'éclairage ou une pièce, ouvrir par « ouvre / lance »…) ; une vraie question n'appelle que des outils de
  lecture ; une demande de rappel n'exécute jamais son contenu ; les réponses du LLM qui prétendent avoir agi
  sans outil sont remplacées. La suite d'une demande enchaînée attend la confirmation de la première action.
- **Configuration vérifiée** : type de chaque réglage (message avec la clé fautive), durées et fréquences > 0.

## Architecture

```
jarvis/
  __main__.py          point d'entrée (CLI : --health, --diagnostics, --benchmark, --memory, --spotify-login...)
  config.py            configuration centrale (config.toml + config.local.toml non versionné)
  interfaces.py        contrats : AudioSource, AudioSink, WakeWordDetector, SpeechToText, LanguageModel, TextToSpeech
  agent.py             boucle veille -> écoute -> réflexion -> réponse ; outils, mode dégradé, programmation
  factory.py           assemble les composants à partir de la config (seul endroit qui les connaît)
  router.py, prompts.py, personality.py   routage des intentions, prompts (complet et court), personnalité
  context.py           contexte conversationnel temporaire (compléments courts)
  memory.py            mémoire explicite (remember / recall / forget)
  profiles.py          profils (rôles) et terminaux (pièce, utilisateur)
  agenda.py            calendrier (local, iCal en lecture)
  spotify.py           API Web Spotify (PKCE)
  home.py              état centralisé de la maison
  diagnostics.py       --health, --diagnostics, --benchmark
  llm/                 ollama.py (client, erreurs typées), failover.py (worker distant, états, secours)
  tools/               core (validation, permissions, confirmation), registre, planificateur LLM, quick (sans LLM),
                       système, applications, audio, fichiers, médias, lumières, minuteurs, réveils, routines, réseau
  routines/            moteur des routines (heure, date précise, intervalle), sonnerie, annonces
  scheduling/          minuteurs et rappels (persistants), heures et durées dites, actions programmées
  audio/ stt/ tts/ wakeword/ web/ weather/ notifications/ face/ api/ control/ winagent/
wakeword_training/     entraînement d'un wake word personnalisé
tts_training/          entraînement de la voix Piper d'ORION (fr_FR-orion-medium)
tests/                 tests silencieux (garde-fous dans conftest.py)
```

L'agent ne dépend que de `interfaces.py`. Pour changer de moteur, ajouter un module et le brancher dans
`factory.py`. Pour ajouter un outil : une fonction qui rend des `Tool` (paramètres typés, niveau de risque, phrase
de réponse), enregistrée dans la fabrique ; le planificateur et le prompt la présentent automatiquement au LLM.

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

Sur le mini-PC, ORION tourne en service utilisateur systemd (démarrage automatique, relance en cas
d'arrêt inattendu) : voir l'en-tête de `deploy/jarvis.service` pour l'installer ;
`systemctl --user restart jarvis` après une mise à jour, `journalctl --user -u jarvis -f` pour le journal.

## Utilisation

```bash
python -m jarvis                    # lancement normal
python -m jarvis --list-devices     # périphériques audio (pour [audio] input_device / output_device)
python -m jarvis --wake-test        # affiche en direct le score du wake word pour régler le seuil
python -m jarvis --input-wav f.wav --output-dir out/   # simule le micro avec un fichier
python -m jarvis --tts-test "Bonjour monsieur."        # voix d'ORION sur la sortie audio configurée
```

## Réponse en flux (LLM → TTS → audio)

ORION commence à parler dès que la première phrase de la réponse est prête, pendant qu'Ollama
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
  les files et n'en joue plus rien. Dire « Jarvis » pendant une réponse l'interrompt (`[assistant] barge_in`).
- **Voix Piper** : chargée et préchauffée au démarrage, jamais rechargée entre deux phrases. Le TTS
  reste interchangeable (`[tts] engine` : piper ou neutts).

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
3. **capacités** enregistrées (outils, recherche Web) : elles remplacent les replis correspondants ;
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

ORION agit sur le PC via des outils explicitement enregistrés, jamais via un shell. Le LLM ne fait que
proposer un appel structuré ; ORION le valide, applique le niveau de risque de l'outil et rapporte le
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
- ORION n'annonce une action réussie que si l'outil a réellement réussi (sinon : message d'erreur).
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
- `TimerManager` (`jarvis/scheduling/`) : un seul fil pour toutes les échéances, arrêté avec ORION ;
  publie `timer.created / cancelled / finished` et `reminder.*` sur le bus (journal d'activité inclus).
- À l'échéance, une notification est créée (phrase `timer_finished` / `reminder_finished` de
  `personality.toml`, sans LLM) et ORION la prononce dès qu'il est libre, en veille comme en conversation.

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
- Si la météo est indisponible, ORION le dit simplement (jamais de météo inventée) ; « cherche / recherche
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
sans bloquer les autres). Le canal vocal les prononce une à une avec le TTS d'ORION. Un futur canal
(bureau, téléphone...) hérite de `NotificationChannel` (`send`, `start`, `stop`, priorité minimale) et
s'enregistre auprès du gestionnaire. Événements : `notification.created / sent / failed` (journal
d'activité) et `notification.started / finished`. `[notifications] voice_enabled = false` coupe les annonces.
- En mémoire : un redémarrage efface les échéances. Limites dans `[timers]` (durée maximale, nombre).

### Événements et journal d'activité

Les composants communiquent par un bus d'événements interne (`jarvis/events.py`) : `publish(Event)`,
`subscribe(type, handler)` (ou `"*"` pour tout recevoir), désabonnement. Un événement a un `type`
(`domaine.action`), une `source`, un `payload` et un horodatage ; un abonné en erreur est journalisé
sans bloquer les autres ni ORION.

Chaque demande d'outil publie `tool.started` puis `tool.executed` ou `tool.failed` (outil, étape,
décision, confirmation, erreur, durée, paramètres nettoyés ; jamais le résultat brut). Le visage écoute
`tool.started` ; le journal d'activité (`jarvis/activity.py`, section `[activity]`) enregistre
`tool.executed` et `tool.failed` dans `data/activity.jsonl` :

```bash
python -m jarvis --activity        # [14:32:01] tool.executed — open_application ...
```

### Test manuel (vraies actions)

Lancer `python -m jarvis`, puis dire « Jarvis… » avant chaque phrase et vérifier :

1. « Ouvre Discord » : Discord s'ouvre, ORION le confirme (sans question).
2. « Ouvre YouTube » : la page s'ouvre dans le navigateur par défaut.
3. « Mets le volume à 30 % » : le volume Windows passe à 30 (icône du son).
4. « Coupe le son », puis « Remets le son » : le son est coupé puis rétabli.
5. « Donne-moi les informations de cette machine » : processeur, mémoire, carte graphique réels.
6. « Ferme Discord » : ORION demande « Voulez-vous que je ferme Discord ? » ; « Oui » : Discord se ferme
   (« Non » : rien ne se passe).
7. « Verrouille le PC » : ORION demande confirmation ; « Oui » : l'écran de verrouillage s'affiche.

Dans le terminal, chaque action laisse une ligne `outil {...}` avec son résultat.

## Autorité du Core et politiques Cedar (`[tools.policy]`)

Le LLM ne fait que **proposer** un appel d'outil ; le Core seul décide et exécute :

```
LLM -> proposition (non fiable) -> parse_request (outil du registre, paramètres connus, aucun champ en plus)
    -> contexte de confiance construit par le Core : utilisateur et rôle (terminal, profils), risque et catégorie
       (registre), confirmation (recueillie par le Core, liée à l'action exacte, 30 s), origine (conversation,
       routine, API), modèle de confiance ou non ([llm] trusted)
    -> porte de décision (jarvis/tools/policy.py) : PermissionManager, et Cedar selon le mode
    -> exécuter / demander confirmation / refuser -> journal sans valeur de paramètre
```

- `engine = "builtin"` (défaut) : PermissionManager seul, comportement historique.
- `engine = "shadow"` : PermissionManager seul décide ; Cedar (`policies/orion.cedar`) est évalué et chaque décision,
  écarts compris, est écrite dans `data/policy_decisions.jsonl`. Aucune action ne dépend de Cedar.
- `engine = "both"` : double verrou. Permis seulement si les deux permettent ; la confirmation exigée par l'un
  reste exigée ; refus si l'un refuse, si Cedar est en erreur (une règle en erreur est un refus), trop lent
  (0,5 s) ou indisponible (cedarpy absent, politique invalide : tout est refusé).
- Une demande issue d'un contenu (mail, page, fichier) n'est jamais exécutée, quel que soit le mode.
- Une routine lancée par la voix (`run_routine`) s'exécute avec les droits de celui qui la demande ; programmée,
  avec ceux du propriétaire.

Installation de Cedar (seulement pour `shadow` et `both`) : `pip install -r requirements-policy.txt`.
Tests : `python -m pytest tests/test_policy.py` (les tests Cedar sont ignorés sans cedarpy).

**Essayer un modèle non éprouvé (abliterated) sans accès sensible**

1. `pip install -r requirements-policy.txt`, puis `python -m pytest -q tests/test_policy.py` : tout doit passer.
2. Dans `config.local.toml` : `[tools.policy] engine = "both"` et, dans `[llm]`, `model = "<modèle à essayer>"`,
   `trusted = false`. Ses propositions sont alors limitées aux outils d'information, de maison, de médias et de
   minuteurs : ni mail, ni fichier, ni application, ni PC, ni mémoire.
3. Un modèle Qwen3 a besoin de répondre sans réflexion (sinon réponses vides, voir
   `research/rapport_llm_avp.html`).
4. Relire `data/policy_decisions.jsonl` (refus, écarts) et le journal du service.

**Retour arrière** : `engine = "builtin"` (et `trusted = true`, modèle d'origine) dans `config.local.toml`, puis
redémarrer ORION. Rien d'autre à défaire.

## Visage graphique

Au lancement, ORION ouvre son visage animé dans le navigateur (`http://127.0.0.1:8765/`) : anneaux
holographiques dessinés en direct (canvas, HTML/CSS/JS sans dépendance), qui suivent son état.

| ORION | Visage |
|---|---|
| en veille (`sleep`) | STANDBY : rotations lentes, faible lumière, pulsation lente du noyau |
| wake word, écoute, STT | LISTENING : plus lumineux, anneaux internes plus rapides, pulsation régulière |
| demande comprise, routage, recherche Web | THINKING : rotations rapides, segments en mouvement, particules vers le centre |
| lecture audio | SPEAKING : noyau et anneaux réagissent au niveau réel de la voix |

Le niveau audio est mesuré sur les échantillons réellement envoyés au haut-parleur (`MeteredSink`),
sans toucher à Piper. Le visage est non critique : port pris, page fermée ou erreur, ORION
continue en vocal. Réglages dans `[face]` de `config.toml` (`host = "0.0.0.0"` pour une tablette
ou un écran mural du réseau local).

```bash
python -m jarvis                  # ORION + visage
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

## Réveils

« Réveille-moi à 7 h 30 », « mets un réveil demain à 6 heures », « quels réveils ? », « annule mon réveil » :
la sonnerie joue `[alarms] sound` (votre fichier MP3, WAV ou FLAC, à copier dans `data/` du Core ; une sonnerie
de secours sinon) jusqu'à ce que vous disiez « Jarvis » ou « arrête », puis ORION annonce la journée (date,
météo, rappels, réveils et routines du jour). Un réveil par la voix sonne une fois ; les réveils récurrents se
créent dans ORION Control (routine avec l'action « Réveil »). Les routines peuvent aussi « Annoncer » l'heure,
la date, la météo ou la journée.

Dire « Jarvis » pendant qu'ORION parle l'interrompt (`[assistant] barge_in`) ; « arrête », « tais-toi »,
« ta gueule »... le renvoient en veille.

## ORION Control (application Windows)

Interface de contrôle et de configuration : tableau de bord (Core, LLM, agents, recherche, lumières, routines,
minuteurs, dernière activité), routines (création, modification, activation, duplication, test, suppression),
appareils (états, commandes des lumières), historique filtrable, paramètres. Icône dans la zone de
notification, Ctrl+Shift+J pour ouvrir ou masquer, démarrage automatique en option.

Elle ne fait rien elle-même : elle demande tout au Core par son API d'administration (`[api]`, port 8766,
IP autorisées + jeton `JARVIS_AGENT_TOKEN`). Les routines n'utilisent que les outils enregistrés, sans
confirmation requise ; aucune commande libre. Le jeton reste dans le processus Python (pont local
127.0.0.1 protégé par une clé de session) : l'interface ne le voit jamais.

```powershell
.venv\Scripts\python -m pip install -r requirements-control.txt
.venv\Scripts\pythonw -m jarvis.control           # ou --hidden pour démarrer dans la zone de notification
```

Côté Core (mini-PC) : `[api] enabled = true` et `allowed_ips = ["<IP du PC>"]` dans `config.local.toml`,
puis ouvrir le port au seul PC : `sudo ufw allow from <IP du PC> to any port 8766 proto tcp`.
Paramètres de l'application et journal : `%APPDATA%\ORION Control`.

## Présence à la maison (arrivées, départs, « Bon retour »)

`jarvis/presence/` : ORION sait qui est à la maison et confirme un départ ou un retour à partir de **plusieurs
signaux** (téléphone, porte d'entrée, mouvement), jamais d'une porte seule. Sortir les poubelles ou récupérer un
colis ne déclenche ni « départ » ni « Bon retour ».

- **Capteurs** (`[presence.sensors.<id>]` dans config.local.toml) : `door`, `motion`, `presence` (téléphone d'un
  utilisateur), `leak`, `smoke`, `temperature`, `appliance`. Ils produisent des **événements normalisés**
  (`door.open`, `presence.away`, `motion.detected`...) sur le bus ; le moteur ne connaît aucun fabricant.
- **Moteur** : un état par utilisateur suivi — HOME, POSSIBLE_DEPARTURE, AWAY, POSSIBLE_ARRIVAL. Confiance
  déterministe : téléphone 50, porte ouverte 30, refermée 10, mouvement dans l'entrée 20 ; confirmé à 90.
  Fenêtres (`departure_window`, `arrival_window`), seuil et délais dans `[presence]`. Téléphone parti sans
  porte : départ seulement après `absence_minutes` sans aucun mouvement. Plusieurs personnes : la maison peut
  être occupée sans que vous soyez « rentré ».
- **Journal de la maison** (`data/house_journal.jsonl`) : priorités CRITICAL (fuite, fumée), IMPORTANT (appareil
  terminé, porte restée ouverte, mouvement pendant l'absence), NORMAL ; mouvements et signaux techniques ignorés.
- **Routine « Bon retour »** (créée à la première mise en service, modifiable dans ORION Control) : déclencheur
  « Événement de la maison : retour confirmé », annonce « Bon retour, monsieur. » et le résumé de l'absence
  (l'important seulement, sinon « Rien de particulier pendant votre absence. »).
- **Simulateur** (sans matériel ; même chemin qu'un vrai capteur, par l'API d'administration : ajouter
  `"127.0.0.1"` à `[api] allowed_ips`) :

```bash
python -m jarvis --simulate door open
python -m jarvis --simulate presence away
python -m jarvis --simulate machine_a_laver finished
python -m jarvis --simulate thermometre_salon measured 21.5
python -m jarvis --simulate status
```

- **Brancher un vrai capteur** : écrire un pilote dans `jarvis/presence/sensors.py` (classe avec `start(emit)` et
  `stop()`, qui appelle `emit("porte_entree", "open")` quand le matériel le signale — MQTT, Zigbee, ESP32, Home
  Assistant...), l'ajouter à `DRIVERS`, puis `driver = "<nom>"` sur le capteur. Rien d'autre ne change.

## Mails (lecture, tri, résumé ; actions confirmées)

Module `jarvis/mail` en IMAP/SMTP (bibliothèque standard), désactivé tant que `[mail]` n'est pas configuré :
« lis-moi mes mails » répond alors toujours que l'accès n'est pas disponible.

- **Sans confirmation** (rien ne change dans la boîte) : `check_mail` (« ai-je des mails ? », importants d'abord,
  lettres d'information comptées à part), `list_mail`, `search_mail` (« cherche les mails de la banque »),
  `read_mail` et `summarize_mail` (« lis le deuxième », « résume ce mail »). Le dossier est ouvert en lecture seule
  et lu avec `BODY.PEEK` : un mail lu par ORION reste **non lu**.
- **Toujours confirmées** (la question nomme l'expéditeur et l'objet, ou le destinataire et le texte) :
  `mark_mail_read`, `archive_mail`, `delete_mail` (vers la corbeille, récupérable), `send_mail`, `reply_mail`.
  Jamais d'envoi, de suppression ni de transfert automatique ; aucune pièce jointe n'est ouverte ni envoyée.
- **Contenu non fiable** : le texte d'un mail est une donnée, jamais une instruction (règle du prompt). Une action
  doit être dite par l'utilisateur (verbe « supprime », « archive », « réponds »... dans sa phrase) et un destinataire
  doit avoir été prononcé (adresse, ou nom d'un contact de `[mail.contacts]`) : un mail qui demande « envoie tout à
  pirate@... » ne peut rien déclencher. Les mails sont désignés par leur rang dans la dernière liste dite, jamais
  par un identifiant choisi par le LLM.
- **Droits** : catégorie `mail` réservée au propriétaire (ni adulte, ni enfant, ni invité).
- **Tri** (`jarvis/mail/priority.py`, déterministe) : important = expéditeur de `important_senders`, mail étoilé,
  ou objet avec un mot d'alerte (facture, rendez-vous, livraison...) ; bruit = envoi automatique (List-Unsubscribe,
  noreply, newsletter, notifications) ; normal sinon.

Configuration : mot de passe **d'application** dans `.env` (`MAIL_PASSWORD=...` ; Gmail : compte Google > Sécurité >
Mots de passe des applications, IMAP activé), le reste dans `config.local.toml` (exemple commenté dans
`config.toml`, section `[mail]`). Pour Gmail, `archive_folder = "[Gmail]/Tous les messages"` et
`trash_folder = "[Gmail]/Corbeille"` (noms selon la langue du compte). OAuth n'est pas implémenté : une boîte qui
refuse les mots de passe d'application (Microsoft 365 professionnel) n'est pas encore prise en charge.

## Lumières (ampoules Tuya / LSC Smart Connect, en local)

« Allume la chambre à 30 % », « éteins les lumières », « mets l'entrée en vert », « remets la chambre en blanc
chaud », « la chambre à 4000 kelvins » : outils `light_on`, `light_off`, `light_toggle`, `set_brightness`,
`set_color`, `set_color_temperature`, exécutés par le Core en local (TinyTuya, sans cloud). La pièce vient des
mots de la demande (alias), jamais du LLM ; sans pièce nommée (« allume la lumière »), toutes les lumières
sont visées. Réponses sans LLM : « La lumière de la chambre est allumée à 30 %. »

Réglages dans `[lights]` (voir `config.toml`) : une pièce par ampoule dans `config.local.toml` (nom, device_id,
ip, version, alias), clé locale de chaque ampoule dans `.env` (`LIGHT_KEY_<PIÈCE>`), jamais dans Git. Les clés
s'obtiennent une fois depuis un compte Smart Life ; une IP fixe par ampoule (bail réservé dans la box) est
conseillée.

Ambiances : « mets les lumières en mode cinéma », « ambiance détente dans la chambre » (`set_scene` : cinéma,
lecture, détente, nuit, travail, réveil ; `[lights.scenes.<nom>]` pour en modifier ou en ajouter, avec brightness,
temperature ou color). Groupes : `[lights.groups.<nom>]` (name, rooms, aliases). « Est-ce que la lumière de la
chambre est allumée ? » : `light_status`, lu sur l'ampoule. Une pièce nommée sans lumière configurée est signalée
au lieu d'allumer toute la maison. Plusieurs ampoules sont commandées en parallèle ; si l'une ne répond pas, les
autres sont réglées et ORION le dit.

## Agent Windows (contrôle et audio du PC à distance)

Quand le Core ORION tourne sur le mini-PC, le PC Windows peut exécuter des actions à sa demande via
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
New-NetFirewallRule -DisplayName "ORION Agent (TCP 8765)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress 192.168.1.91 -Profile Private,Domain
```

La règle vaut pour les réseaux « Privé » ou « Domaine » : si la connexion Windows est en « Public »,
passez-la en « Privé » (Paramètres > Réseau) plutôt que d'élargir la règle.

Test depuis le mini-PC : `curl http://192.168.1.128:8765/health`.

**Audio du PC, ORION sur le mini-PC.** Dans le `config.toml` du mini-PC :
`[audio] remote = "http://192.168.1.128:8765"` (et `JARVIS_AGENT_TOKEN` dans son `.env`). ORION
écoute alors le micro du PC et lui parle, à la place des périphériques locaux. Si le PC est éteint
ou l'agent arrêté, ORION attend et se reconnecte seul ; la voix qui ne peut être jouée est ignorée,
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
- SearXNG injoignable, aucun résultat, `[web] enabled = false` : ORION le dit simplement ou garde
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
Au démarrage, ORION affiche `STT device`, `STT compute type` et `STT model`.

## Choisir la voix Piper

Pour comparer plusieurs voix Piper à l'oreille, avec exactement le même chemin TTS → sortie audio
qu'ORION :

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

Voix par défaut : `fr_FR-orion-medium`, la voix propre à ORION, entraînée dans ce dépôt et versionnée dans
`models/piper/` (voir « Entraîner la voix d'ORION »). Même architecture que `fr_FR-siwis-medium` (femme, CC-BY 4.0),
dont elle est un fine-tuning : même coût de calcul sur le mini-PC (0,1 s par seconde de voix, contre 0,17 s pour
`fr_FR-tom-medium`). Pour revenir à siwis : `voice = "models/piper/fr_FR-siwis-medium.onnx"` dans
`config.local.toml`, puis `python scripts/download_models.py`.

**Signature synthétique.** `[tts.effect]` applique en mémoire, à chaque phrase, une chaîne légère en numpy :
coupe des graves sourds, clarté (`presence_db`), brillance (`air_db`), texture électronique discrète
(`shimmer`, `shimmer_ms` : léger écho de moins d'une milliseconde), compression douce et limite de crête.
Coût mesuré : ~2 ms par seconde de voix, intelligibilité Whisper inchangée. `enabled = false` rend la voix
d'origine ; si le traitement échoue, la phrase est jouée sans effet.

Voix françaises Piper disponibles :

| voix | locuteurs | remarques |
|---|---|---|
| `fr_FR-orion-medium` | femme | voix d'ORION, entraînée ici (défaut) |
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

## Entraîner la voix d'ORION

`models/piper/fr_FR-orion-medium.onnx` est une voix Piper « medium » obtenue par fine-tuning de
`fr_FR-siwis-medium` sur ~5 min d'enregistrements d'ORION (95 phrases). Les outils sont dans `tts_training/` et
ne servent pas à l'exécution d'ORION.

- **Corpus** : `tts_training/corpus_orion_fr.md`. Une section `## …` par enregistrement : la section N est lue
  dans un WAV nommé `N <titre>.wav` (phrases dites dans l'ordre, séparées par un court silence).
- **Découpage** : Whisper horodate chaque mot de l'enregistrement long ; le texte du corpus est aligné sur ces
  mots et sert de transcription (Whisper ne fait que placer les coupes). Les nombres que Whisper écrit en
  chiffres (« 7h15 ») sont tolérés. `dataset.py check` re-transcrit chaque segment pour repérer une coupe ratée.
- **Entraînement** : [piper1-gpl](https://github.com/OHF-Voice/piper1-gpl), poids de départ siwis
  (`rhasspy/piper-checkpoints`), 1000 époques. Environ 1 h 05 sur une RTX 4070 Laptop (8 Go, lot de 16).
- **Mesures** (`train.sh evaluate`, 8 phrases hors corpus re-transcrites par Whisper) : 93 % des mots retrouvés,
  contre 97,5 % pour siwis ; même coût de calcul (0,04 s par seconde de voix sur le PC d'entraînement).
- **Environnement** : Linux ou WSL2 avec GPU NVIDIA ; tout s'installe dans `~/orion_tts` sans droits root
  (uv, cmake et ninja par pip). Les enregistrements bruts et le dossier de travail restent hors git.

```bash
bash tts_training/train.sh setup                     # piper1-gpl, PyTorch, Whisper, checkpoint siwis
bash tts_training/train.sh dataset "/chemin/des/wav"  # découpe, alignement, contrôle
bash tts_training/train.sh train                     # EPOCHS=1000 BATCH=16 par défaut
bash tts_training/train.sh export                    # écrit models/piper/fr_FR-orion-medium.onnx(.json)
bash tts_training/train.sh evaluate                  # intelligibilité comparée à siwis, WAV dans ~/orion_tts/evaluate
python -m jarvis --tts-test "Bonjour monsieur."      # écoute avec la chaîne réelle d'ORION
```

Pour améliorer la voix : enregistrer de nouvelles sections (ajoutées au corpus avec le WAV correspondant),
relancer `dataset`, `train` et `export`.

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
appel, ORION vérifie en moins d'une seconde que le worker répond, sinon il bascule aussitôt sur le secours
et retente le worker 30 s plus tard. Les deux sont préchargés au lancement.

- `[llm] model` : modèle Ollama ; sur CPU seul, préférer un 3-4B.
- `[stt] model` : `small` (précis, ~2 s par phrase sur Ryzen 7) ou `base` (~0,7 s, moins fiable).
- `[wake_word] threshold` et `patience` : 0,55 tenu 2 images (80 ms chacune) d'affilée ; bas parce que la seconde vérification (`verify`) écarte les faux réveils ; à ajuster avec `--wake-test` et `--wake-report`.
- `[assistant] conversation_timeout` : durée d'écoute sans wake word après une réponse.
- `[audio] min_rms`, `speech_to_noise_ratio`, `end_of_speech_silence` : détection de parole.

## Wake word

### Contre les faux réveils : vérification, captures, réentraînement

Relevé sur trois jours : environ un réveil sur deux sans aucune demande ensuite (voix de jeu, Discord, TV).

1. **Seconde vérification** (`[wake_word] verify`, `jarvis/wakeword/verify.py`) : après la détection, un petit
   Whisper (`tiny`, 0,4 s sur le mini-PC) relit les deux dernières secondes ; la conversation ne s'ouvre que s'il
   y entend « Jarvis » (« Jervis », « J'avise » compris ; « service », « j'arrive », « j'avais » écartés). Un
   « Jarvis » écarté à tort se rattrape en le redisant entre 1 et 8 s plus tard (accepté sans vérification).
2. **Captures** (`[wake_word] captures`, `captures_keep`) : l'audio et la fiche de chaque réveil (score,
   transcription, issue : demande, sans suite, écarté) dans `data/wakeword/captures`. Bilan et effet d'un autre
   seuil : `python -m jarvis --wake-report`.
3. **Réentraînement** : `python -m wakeword_training import-captures [dossier]` verse les faux réveils dans les
   mots pièges réels et vos « Jarvis » confirmés dans les positifs réels, puis `features`, `train`, `evaluate`.
   Vos propres enregistrements (`record positive`) restent le meilleur complément.

### Moteur et modèle

- **Moteur :** openWakeWord (inférence ONNX locale, `jarvis/wakeword/openwakeword.py`).
  Deux étages : un extracteur générique (spectrogramme mel → embeddings, commun à tous
  les mots) et un petit **classifieur propre au mot**.
- **Modèle :** `models/openwakeword/orion_fr.onnx`, un classifieur entraîné dans ce dépôt
  pour « Orion » (voix Piper et 23 voix neuronales Edge, de loin, dans le bruit, mal articulé).
  Il est versionné avec le projet ; ses mesures sont dans `models/openwakeword/orion_fr.json`
  (évaluation) et `orion_fr_e2e.json` (banc de bout en bout). L'ancien modèle
  `jarvis_fr.onnx` reste disponible.
- **Vérification :** un déclenchement est relu par Whisper « base » sur les 2 dernières
  secondes, dont 0,32 s écoutées après le déclenchement pour entendre le mot en entier ; un
  réveil franc (`sure_score`, `sure_patience`) est accepté sans elle, car Whisper reconnaît
  mal une voix lointaine. Un mot écarté à tort se rattrape en le redisant dans les 8 s.
- **Banc silencieux :** `python -m wakeword_training e2e --spec wakeword_training/specs/orion_fr.toml`
  (voix de test jamais vues, bruit, distance, mots pièges, faux réveils par heure ; aucun son joué).
- Le modèle pré-entraîné `hey_jarvis` d'openWakeWord ne convient pas : il a appris la
  prononciation anglaise de « hey jarvis ». En français, les scores plafonnent autour de 0,1.

### Tester le wake word

```bash
python -m jarvis --wake-test
```

L'outil affiche en direct le score, le seuil et chaque détection. Prononcez « Orion »
plusieurs fois, près du micro puis plus loin, et parlez normalement entre deux essais
pour vérifier qu'il ne se déclenche pas.

### Changer de wake word

Le code ne contient aucune référence au mot : tout passe par `config.toml`.

```toml
[wake_word]
phrase = "Orion"                               # mot affiché (et entendu par la vérification)
model = "models/openwakeword/orion_fr.onnx"    # classifieur de ce mot
threshold = 0.5                                # choisi par le banc de bout en bout (orion_fr_e2e.json)
```

Pour un autre mot, entraînez un nouveau classifieur (voir la section suivante), puis
changez `phrase`, `model` et `threshold`.

### Entraîner un wake word

Les outils sont dans `wakeword_training/` et ne servent pas à l'exécution d'ORION.
Chaque mot est décrit par un fichier TOML (`wakeword_training/specs/orion_fr.toml`, ou `jarvis_fr.toml`) :
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
- **La détection à distance est faible au seuil retenu** (46 % en simulation). Si ORION est
  loin de vous, baissez le seuil, par exemple à 0,8, en acceptant davantage de fausses alertes.
- La distance au micro et le bruit sont simulés, pas enregistrés dans votre pièce.
- **La vérification Whisper est lente sur CPU** (~5 s par clip). Si une carte NVIDIA est
  présente et que cuBLAS 12 est dans le `PATH`, elle passe automatiquement sur GPU, environ
  10 fois plus vite. cuBLAS 12 s'obtient avec `pip install nvidia-cublas-cu12`, le CUDA Toolkit,
  ou le dossier `lib/ollama/cuda_v12` d'Ollama sous Windows. Comptez ~30 min sur GPU pour
  l'étape `generate`, plusieurs heures sur CPU.
- Un mot très proche, comme « Jervis », peut déclencher : c'est volontaire, pour ne pas
  rejeter les prononciations naturelles de « Jarvis ».

## Diagnostics

```bash
python -m jarvis --health        # chaque composant en quelques secondes (code 1 si un indispensable échoue)
python -m jarvis --diagnostics   # + configuration, outils et risques, derniers échecs
python -m jarvis --benchmark     # wake word, STT, LLM, choix d'outil, outils, voix (rien n'est joué)
python -m jarvis --memory        # ce qu'ORION a retenu
python -m jarvis --spotify-login # relier Spotify (une fois)
```

Mesures sur le mini-PC (octobre 2026) : wake word 2,4 ms par image, STT 2,9 s pour 4 s de parole (whisper small
sur processeur, principal délai restant), premier mot du LLM 0,08 s (Katana), choix d'outil 0,56 s par le LLM ou
0,2 ms sans LLM, lecture des deux ampoules 0,57 s, synthèse Piper 0,46 s pour 4 s de voix.

## Tests

```bash
python -m pytest -q tests                       # toute la suite, silencieuse : aucun son, micro, navigateur,
                                                # ampoule, volume, application ou touche réels
JARVIS_E2E=1 python -m pytest tests/test_pipeline_e2e.py   # vrais moteurs sur scenario.wav (voix synthétisée, non jouée)
```

`tests/conftest.py` fait échouer tout accès réel au son, au navigateur, aux ampoules Tuya et aux touches
multimédia ; les tests passent par des faux (RecordingSink, FakeDriver...).

Les fichiers de référence sont régénérables :
- `tests/fixtures/wakeword/` avec `python tests/make_wakeword_fixtures.py` (locuteurs de test uniquement) ;
- `tests/fixtures/scenario.wav` avec `python tests/make_scenario.py`.

## Étapes manuelles restantes

- Sonnerie du réveil : copier votre fichier (ex. « Back in Black ») dans `data/alarm.mp3` sur le Core.
- Spotify : application sur developer.spotify.com (retour `http://127.0.0.1:8888/callback`),
  `SPOTIFY_CLIENT_ID` dans `.env`, puis `python -m jarvis --spotify-login` (compte Premium).
- Agenda existant : adresse iCal secrète dans `.env` (`JARVIS_CALENDAR_ICS=...`).
- Profils : `[users]` et `[terminals]` dans `config.local.toml` si plusieurs personnes utilisent ORION.
