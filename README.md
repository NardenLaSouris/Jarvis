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

## Utilisation

```bash
python -m jarvis                    # lancement normal
python -m jarvis --list-devices     # périphériques audio (pour [audio] input_device / output_device)
python -m jarvis --wake-test        # affiche en direct le score du wake word pour régler le seuil
python -m jarvis --input-wav f.wav --output-dir out/   # simule le micro avec un fichier
```

## Réglages utiles (`config.toml`)

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
```

Les fichiers de référence sont régénérables :
- `tests/fixtures/wakeword/` avec `python tests/make_wakeword_fixtures.py` (locuteurs de test uniquement) ;
- `tests/fixtures/scenario.wav` avec `python tests/make_scenario.py`.
"# Jarvis" 
