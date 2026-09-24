"""Test de bout en bout du pipeline avec les vrais moteurs (wake word, Whisper, Ollama, Piper).

Le micro est remplacé par ``fixtures/scenario.wav`` et le haut-parleur par un
enregistreur. Scénario :
  « Jarvis » -> « Quelle est la capitale de l'Australie ? »
  -> relance sans wake word : « Et combien d'habitants compte cette ville ? »
  -> 10 s de silence (retour en veille)
  -> « Jarvis » -> « Peux-tu ouvrir Firefox et envoyer un mail à Paul ? »
  -> 10 s de silence -> phrase sans wake word, qui doit être ignorée.

Nécessite les modèles téléchargés et Ollama démarré.
Lancement : python tests/test_pipeline_e2e.py   (ou pytest)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.files import ArraySource, RecordingSink, read_wav  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_agent  # noqa: E402


def run_scenario(output_dir: Path | None = None) -> tuple[list[tuple[str, str]], RecordingSink]:
    cfg = load_config(ROOT / "config.toml")
    audio, rate = read_wav(ROOT / "tests" / "fixtures" / "scenario.wav")
    source = ArraySource(audio, cfg.audio.sample_rate, cfg.audio.frame_samples, source_rate=rate)
    sink = RecordingSink(output_dir)
    events: list[tuple[str, str]] = []
    agent = build_agent(cfg, source, sink, on_event=lambda kind, text: events.append((kind, text)))
    agent.run()
    return events, sink


def test_full_pipeline():
    events, sink = run_scenario()
    kinds = [k for k, _ in events if k != "timing"]
    users = [t.lower() for k, t in events if k == "user"]
    replies = [t for k, t in events if k == "assistant"]

    assert kinds.count("wake") == 2, kinds
    assert len(users) == 3, users
    assert "australie" in users[0]
    assert "habitants" in users[1]          # relance sans répéter le wake word
    assert "ouvrir" in users[2] and "mail" in users[2]  # nom propre parfois mal transcrit (« Faye Fox »)
    assert "canberra" in replies[0].lower()
    assert all(replies), replies
    assert not any("concerne" in u for u in users)  # hors conversation : ignoré
    # 2 accusés de réception + 3 réponses prononcées
    assert len(sink.played) == 5
    # Chaque conversation se termine par un retour en veille.
    assert kinds.count("sleep") >= 3


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    test_full_pipeline() if out is None else run_scenario(out)
    print("OK")
