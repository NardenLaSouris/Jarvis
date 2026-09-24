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
scripts/download_models.py   télécharge les modèles définis dans config.toml
tests/                        tests unitaires + test de bout en bout sur scenario.wav
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
- `[wake_word] threshold` : à calibrer avec `--wake-test`.
- `[assistant] conversation_timeout` : durée d'écoute sans wake word après une réponse.
- `[audio] min_rms`, `speech_to_noise_ratio`, `end_of_speech_silence` : détection de parole.

## Wake word

Le modèle pré-entraîné `hey_jarvis` d'openWakeWord réagit à « Jarvis » seul, mais il a
été entraîné sur la **prononciation anglaise** (« djar-vis »). Sur une prononciation
française (« jar-vis »), le score chute nettement. Si la détection est mauvaise avec
`--wake-test`, deux options : baisser le seuil, ou entraîner un modèle personnalisé
(outil d'entraînement d'openWakeWord) et pointer `[wake_word] model` dessus. Le mot
affiché (`phrase`) et le modèle sont indépendants du code de l'agent.

## Tests

```bash
python tests/test_units.py            # rapides, sans modèle ni matériel
python tests/test_pipeline_e2e.py     # pipeline complet sur tests/fixtures/scenario.wav (Ollama requis)
```
"# Jarvis" 
