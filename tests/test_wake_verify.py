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

from orion.wakeword.captures import WakeCaptures, load_captures, report  # noqa: E402
from orion.wakeword.verify import heard_wake  # noqa: E402
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


HEARD_ORION = ["Orion.", "Orion !", "Oryon ?", "Aurion.", "O'Rion !", "Bon, Orion, allume la lumière", "Orillon.",
               "Ouyon.", "Oyon !"]
HEARD_NOT_ORION = ["Nous aurions dû partir.", "Aurions-nous le temps ?", "Marion arrive.", "Horizon.", "Oreille.",
                   "Avion.", "Oriane.", "Orange.", "Champion.", "Jarvis.", "Oignon.", "Lyon.", "Oui, on y va.", "Rayon.",
                   "", "Merci d'avoir regardé cette vidéo !"]


@pytest.mark.parametrize("text", HEARD_ORION)
def test_orion_is_recognised_in_its_transcriptions(text):
    assert heard_wake(text, "Orion")


@pytest.mark.parametrize("text", HEARD_NOT_ORION)
def test_lookalike_words_are_not_taken_for_orion(text):
    assert not heard_wake(text, "Orion")


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


def test_verification_hears_the_end_of_the_word_and_the_request_keeps_it():
    from orion.agent import WAKE_TAIL, Agent, AgentSettings
    from orion.audio.endpointing import EndpointerSettings, UtteranceRecorder
    from orion.audio.files import ArraySource, RecordingSink
    from orion.capabilities import CapabilityRegistry
    from orion.personality import load_personality
    from orion.router import IntentRouter

    rate, frame = 16000, 1280
    silence = np.zeros(frame * 12, np.int16)
    burst = np.full(frame, 30000, np.int16)
    request = (5000 * np.sin(np.arange(rate) / rate * 2 * np.pi * 300)).astype(np.int16)
    request[: frame * 2] = 7777
    audio = np.concatenate([silence, burst, request, np.zeros(3 * rate, np.int16)])
    heard = {}

    class Wake:
        def process(self, f):
            return 1.0 if f.max() > 20000 else 0.0

        def reset(self):
            pass

    class Check:
        def check(self, clip, r):
            heard["verify"] = clip
            return True, "Orion"

    class Stt:
        def transcribe(self, clip, r):
            heard.setdefault("request", clip)
            return "Merci"

    class Llm:
        def chat(self, messages):
            return "Je vous en prie."

    class Tts:
        def synthesize(self, text):
            return np.zeros(10, np.int16), rate

    source = ArraySource(audio, rate, frame)
    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry())
    settings = AgentSettings("ORION", "Orion", 0.5, ("Oui, monsieur ?",), 2.0, 1.0, 4)
    Agent(settings, source, RecordingSink(), Wake(), UtteranceRecorder(source, EndpointerSettings()), Stt(), Llm(),
          Tts(), router, lambda kind, text: None, wake_verifier=Check()).run()
    tail = int(WAKE_TAIL * rate) // frame * frame
    assert heard["verify"][-tail:].max() == 7777 and 30000 in heard["verify"]
    assert heard["request"][: frame * 2].tolist().count(7777) >= frame


def run_burst(frames, verifier, sure_score):
    from orion.agent import Agent, AgentSettings
    from orion.audio.endpointing import EndpointerSettings, UtteranceRecorder
    from orion.audio.files import ArraySource, RecordingSink
    from orion.capabilities import CapabilityRegistry
    from orion.personality import load_personality
    from orion.router import IntentRouter

    rate, frame = 16000, 1280
    request = (5000 * np.sin(np.arange(rate) / rate * 2 * np.pi * 300)).astype(np.int16)
    audio = np.concatenate([np.zeros(frame * 12, np.int16), np.full(frame * frames, 30000, np.int16), request,
                            np.zeros(3 * rate, np.int16)])
    heard = []

    class Wake:
        def process(self, f):
            return 0.97 if f.max() > 20000 else 0.0

        def reset(self):
            pass

    class Stt:
        def transcribe(self, clip, r):
            heard.append(clip)
            return "Merci"

    class Llm:
        def chat(self, messages):
            return "Je vous en prie."

    class Tts:
        def synthesize(self, text):
            return np.zeros(10, np.int16), rate

    source = ArraySource(audio, rate, frame)
    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry())
    settings = AgentSettings("ORION", "Orion", 0.5, ("Oui, monsieur ?",), 2.0, 1.0, 4, wake_patience=2,
                             wake_sure_score=sure_score, wake_sure_patience=4)
    Agent(settings, source, RecordingSink(), Wake(), UtteranceRecorder(source, EndpointerSettings()), Stt(), Llm(),
          Tts(), router, lambda kind, text: None, wake_verifier=verifier).run()
    return heard


def test_a_sure_wake_skips_the_whisper_check():
    verifier = Verifier((False, "Au revoir."))
    assert run_burst(5, verifier, 0.95) and verifier.calls == 0


def test_a_short_or_weaker_wake_is_still_checked():
    for frames, sure_score in ((3, 0.95), (6, 0.99), (6, 0.0)):
        verifier = Verifier((False, "Au revoir."))
        assert run_burst(frames, verifier, sure_score) == [] and verifier.calls == 1


@pytest.mark.parametrize("text", ["Hey Orion.", "Hé Orion !", "Et Orion, allume la lumière", "Hey, Oryon",
                                  "Eh Aurion", "Hey Orion, quelle heure est-il ?"])
def test_hey_orion_is_recognised(text):
    assert heard_wake(text, "Hey Orion")


@pytest.mark.parametrize("text", ["Orion.", "Bon, Orion", "Hey Marion", "Hey Siri", "Nous aurions dû",
                                  "On prie. J'arrête.", "Hey, ça va ?", "La constellation d'Orion"])
def test_orion_alone_or_lookalikes_never_wake_hey_orion(text):
    assert not heard_wake(text, "Hey Orion")


@pytest.mark.parametrize("phrase, hint, woken", [("Hey Orion", "Hey Orion", True), ("Orion", None, True),
                                                 ("Jarvis", None, False)])
def test_whisper_gets_the_hey_orion_hint_only_for_that_wake_word(monkeypatch, phrase, hint, woken):
    """Sans indice, Whisper entend « Et on y va » pour votre « Hey Orion » ; l'indice n'est donné qu'à ce mot."""
    import faster_whisper

    seen = {}

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, samples, **kwargs):
            seen.update(kwargs)
            return iter([SimpleNamespace(text=" Hey Orion.")]), None

    monkeypatch.setattr(faster_whisper, "WhisperModel", Model)
    from orion.wakeword.verify import WakeVerifier

    verifier = WakeVerifier(phrase=phrase)
    confirmed, _ = verifier.check(np.zeros(16000, dtype=np.int16), 16000)
    assert seen["hotwords"] == hint and confirmed is woken
