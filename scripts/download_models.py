"""Télécharge les modèles nécessaires à JARVIS dans le dossier ``models/``.

Usage : python scripts/download_models.py [--config config.toml]

Les chemins et noms de modèles sont lus depuis la configuration, de sorte que
changer de voix ou de modèle Whisper ne demande aucune modification de code.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config, secret  # noqa: E402

OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  déjà présent : {dest.relative_to(ROOT)}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  téléchargement : {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def piper_voice_url(voice: str) -> str:
    locale, name, quality = voice.split("-")
    return f"{PIPER_VOICES}/{locale.split('_')[0]}/{locale}/{name}/{quality}/{voice}.onnx"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    args = parser.parse_args()
    cfg = load_config(args.config)

    print("Wake word (openWakeWord)")
    ww = cfg.wake_word
    for path in (ww.melspectrogram_model, ww.embedding_model):
        fetch(f"{OWW_RELEASE}/{path.name}", path)
    if ww.model.exists():
        print(f"  présent : {ww.model.relative_to(ROOT)}")
    else:
        try:  # modèles pré-entraînés publiés par openWakeWord (ex. hey_jarvis_v0.1.onnx)
            fetch(f"{OWW_RELEASE}/{ww.model.name}", ww.model)
        except urllib.error.HTTPError:
            print(f"  ATTENTION : {ww.model.name} est un modèle personnalisé absent. "
                  "Entraînez-le avec `python -m wakeword_training all` (voir README).")

    if cfg.tts.engine == "neutts":
        n = cfg.tts.neutts
        print(f"TTS (NeuTTS : {n['backbone']}, {n['codec']})")
        from huggingface_hub import snapshot_download

        token = secret("HF_TOKEN", ROOT / ".env") or None
        snapshot_download(n["backbone"], allow_patterns=["*.gguf"], token=token)
        snapshot_download(n["codec"], token=token)
        voice = ROOT / n["voice"]
        if not voice.exists() or not voice.with_suffix(".txt").exists():
            print(f"  ATTENTION : voix de référence absente ({n['voice']} + .txt).")
    else:
        print(f"TTS (voix Piper '{cfg.tts.voice.name}')")
        if cfg.tts.voice.exists():  # voix versionnée dans le dépôt (fr_FR-orion-medium) ou déjà téléchargée
            print(f"  présent : {cfg.tts.voice.relative_to(ROOT)}")
        else:
            url = piper_voice_url(cfg.tts.voice.name.removesuffix(".onnx"))
            try:
                fetch(url, cfg.tts.voice)
                fetch(url + ".json", cfg.tts.voice.with_suffix(".onnx.json"))
            except urllib.error.HTTPError:
                print(f"  ATTENTION : {cfg.tts.voice.name} n'est pas une voix publiée par Piper. "
                      "Entraînez-la avec tts_training/train.sh (voir README).")

    print(f"STT (faster-whisper '{cfg.stt.model}')")
    from faster_whisper import download_model

    download_model(cfg.stt.model, cache_dir=str(cfg.stt.download_root))
    print("Terminé.")


if __name__ == "__main__":
    main()
