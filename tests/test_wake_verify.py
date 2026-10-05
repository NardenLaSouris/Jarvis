"""Seconde vérification du wake word, captures des réveils, bilan et import pour l'entraînement.

Whisper remplacé par un faux vérificateur : aucun modèle chargé, aucun son.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.wakeword.captures import WakeCaptures, load_captures, report  # noqa: E402
from jarvis.wakeword.verify import heard_wake  # noqa: E402
from test_tools import SLEEP, PlannerLLM, run_agent  # noqa: E402

# Transcriptions réelles de whisper tiny et base sur les enregistrements de test (mini-PC, octobre 2026).
HEARD_JARVIS = ["J'arvisse.", "Dip, j'arvisse.", "J'avis.", "et Jervis.", "J'arvis.", "Bon ! Gervisse !",
                "D'y JARVIS !", "D'y J'ARVISS !", "J'avise.", "J'ervice !", "Jarvis, allume la lumière",
                "J'en vis !"]  # vrai « Jarvis » écarté en service le 5 octobre 2026
HEARD_OTHER = ["servisseur.", "j'aiurs réunir.", "et dire très vite.", "bonjour !", "Non, j'ai revis.",
               "J'ai déjà regardé.", "Samach.", "Service.", "J'avais.", "J'arrive !", "J'en vais.", "J'arvais.",
               "Jardin.", "C'est très très... très vite", "", "Merci d'avoir regardé cette vidéo !"]


@pytest.mark.parametrize("text", HEARD_JARVIS)
def test_jarvis_is_recognised_in_its_transcriptions(text):
    assert heard_wake(text)


@pytest.mark.parametrize("text", HEARD_OTHER)
def test_lookalike_words_are_not_taken_for_jarvis(text):
    assert not heard_wake(text)


class Verifier:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def check(self, audio, rate):
        self.calls += 1
        assert rate == 16000 and len(audio) > 0
        return self.answers.pop(0) if self.answers else (True, "Jarvis")


def test_a_rejected_wake_never_opens_the_conversation_and_is_kept(tmp_path):
    captures = WakeCaptures(tmp_path / "captures")
    verifier = Verifier((False, "Service."))
    spoken, events = run_agent(["Quelle heure est-il ?"], PlannerLLM(), wake_verifier=verifier,
                               wake_captures=captures)
    assert verifier.calls == 1 and spoken == [] and not any(k == "user" for k, _ in events)
    saved = load_captures(tmp_path / "captures")
    assert [c["outcome"] for c in saved] == ["rejected"] and saved[0]["heard"] == "Service."


def test_saying_jarvis_again_right_after_a_rejection_is_accepted(tmp_path):
    captures = WakeCaptures(tmp_path / "captures")
    verifier = Verifier((False, "J'en revise."))
    spoken, events = run_agent([SLEEP, "Quelle heure est-il ?"], PlannerLLM(), wake_verifier=verifier,
                               wake_captures=captures)
    assert verifier.calls == 1  # le second appel n'est pas revérifié
    assert any(k == "user" and "heure" in t for k, t in events)
    outcomes = {c["outcome"]: c for c in load_captures(tmp_path / "captures")}
    assert set(outcomes) == {"rejected", "used"} and outcomes["used"].get("retry") is True


def test_accepted_wake_outcome_follows_the_conversation(tmp_path):
    captures = WakeCaptures(tmp_path / "captures")
    run_agent([], PlannerLLM(), wake_verifier=Verifier(), wake_captures=captures)  # réveil, puis rien
    run_agent(["Quelle heure est-il ?"], PlannerLLM(), wake_verifier=Verifier(), wake_captures=captures)
    saved = load_captures(tmp_path / "captures")
    assert sorted(c["outcome"] for c in saved) == ["silent", "used"]
    assert all(c["verified"] is True and c["score"] > 0 for c in saved)


def test_report_and_rotation(tmp_path):
    captures = WakeCaptures(tmp_path, keep=4)
    audio = np.zeros(16000, np.int16)
    for score, outcome in ((0.99, "used"), (0.95, "used"), (0.72, "silent"), (0.81, "rejected"), (0.75, "silent")):
        captures.finish(captures.save(audio, 16000, {"score": score, "heard": "Service."}), outcome)
    assert len(load_captures(tmp_path)) <= 4
    text = report(tmp_path)
    assert "réveils" in text and "0.80" in text and "Service." in text
    assert report(tmp_path / "vide").startswith("Aucun réveil")


def test_captures_feed_the_training_set(tmp_path):
    from wakeword_training.captures import run

    source = tmp_path / "captures"
    captures = WakeCaptures(source)
    audio = np.zeros(16000, np.int16)
    for meta, outcome in (({"score": 0.9, "verified": True}, "used"), ({"score": 0.8}, "silent"),
                          ({"score": 0.8, "verified": False}, "rejected"), ({"score": 0.9, "retry": True}, "used")):
        captures.finish(captures.save(audio, 16000, meta), outcome)
    spec = SimpleNamespace(real_dir=tmp_path / "real")
    assert run(spec, source) == {"positive": 1, "negative": 2, "skipped": 1}
    assert run(spec, source)["negative"] == 0  # déjà importés
    assert json.loads(next(source.glob("*.json")).read_text(encoding="utf-8"))["outcome"]
