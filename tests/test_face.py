"""Visage graphique : état visuel, niveau audio mesuré, branchement non critique et serveur local."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import Agent, AgentSettings  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource, RecordingSink  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.face import AudioActivity, FaceBridge, FaceServer, MeteredSink, VisualState  # noqa: E402
from jarvis.face.state import envelope  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402

SR = 16000


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def tone(seconds, amplitude, rate=SR):
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


# --- État visuel ---------------------------------------------------------------------------------

def test_states_and_standby():
    visual = VisualState()
    assert visual.snapshot()["state"] == "standby"
    for state in ("listening", "THINKING", "speaking", "standby"):
        visual.set_state(state)
        assert visual.snapshot()["state"] == state.lower()
    visual.set_state("thinking")
    visual.standby()
    assert visual.snapshot()["state"] == "standby"
    with pytest.raises(ValueError):
        visual.set_state("dancing")


def test_snapshot_fields_and_transition_time():
    clock = Clock()
    visual = VisualState(clock=clock)
    visual.set_state("thinking")
    clock.t += 2.5
    snap = visual.snapshot()
    assert set(snap) == {"state", "audio_level", "audio_source", "activity", "transition"}
    assert snap["transition"] == 2.5 and snap["activity"] > VisualState(clock=clock).snapshot()["activity"]


def test_audio_level_is_absent_without_audio():
    snap = VisualState().snapshot()
    assert snap["audio_level"] == 0.0 and snap["audio_source"] == "none"


def test_external_audio_level_is_clamped_and_expires():
    clock = Clock()
    visual = VisualState(clock=clock)
    for given, expected in ((0.42, 0.42), (3.0, 1.0), (-1.0, 0.0), (float("nan"), 0.0)):
        visual.set_audio_level(given)
        assert visual.snapshot()["audio_level"] == expected
    visual.set_audio_level(0.5)
    clock.t += 1.5
    assert visual.snapshot()["audio_source"] == "none"


# --- Niveau audio mesuré -------------------------------------------------------------------------

def test_envelope_follows_loudness():
    quiet, normal, loud = (float(envelope(tone(0.2, a), SR).mean()) for a in (300, 3000, 20000))
    assert 0 < quiet < normal < loud <= 1
    assert envelope(np.zeros(SR // 5, np.int16), SR).max() == 0
    assert len(envelope(np.zeros(0, np.int16), SR)) == 0


def test_audio_activity_is_scheduled_on_playback_time():
    clock = Clock()
    activity = AudioActivity(clock)
    activity.feed(tone(1.0, 20000), SR)
    activity.feed(tone(1.0, 500), SR)
    assert activity.level(100.5) > activity.level(101.5) > 0
    assert activity.level(99.0) == 0 and activity.level(102.5) == 0
    assert activity.busy(101.9) and not activity.busy(102.1)


def test_speaking_follows_real_audio_and_returns_to_previous_state():
    clock = Clock()
    visual = VisualState(clock=clock)
    visual.set_state("thinking")
    visual.speak(tone(1.0, 12000), SR)
    snap = visual.snapshot()
    assert snap["state"] == "speaking" and snap["audio_source"] == "measured" and snap["audio_level"] > 0.5
    clock.t += 1.0 + 0.5
    assert visual.snapshot()["state"] == "thinking"


def test_an_event_during_speech_wins_over_the_automatic_return():
    clock = Clock()
    visual = VisualState(clock=clock)
    visual.set_state("thinking")
    visual.speak(tone(0.5, 12000), SR)
    visual.set_state("listening")
    clock.t += 2
    assert visual.snapshot()["state"] == "listening"


# --- Branchement non critique --------------------------------------------------------------------

def test_bridge_maps_agent_events_and_forwards_them():
    forwarded, visual = [], VisualState()
    bridge = FaceBridge(visual, forward=lambda kind, text: forwarded.append(kind))
    expected = {"wake": "listening", "listening": "listening", "user": "thinking", "web": "thinking",
                "stt": "listening", "sleep": "standby"}
    for kind, state in expected.items():
        bridge.on_event(kind, "")
        assert visual.snapshot()["state"] == state, kind
    bridge.on_event("timing", "STT 0.3 s")
    assert visual.snapshot()["state"] == "standby"
    assert forwarded == [*expected, "timing"]


def test_metered_sink_plays_unchanged_and_survives_face_errors():
    class BrokenVisual:
        def speak(self, audio, rate):
            raise RuntimeError("visage en panne")

    inner, audio = RecordingSink(), tone(0.1, 5000)
    sink = MeteredSink(inner, BrokenVisual())
    sink.play(audio, SR)
    assert np.array_equal(inner.played[0][0], audio) and inner.played[0][1] == SR
    assert not hasattr(sink, "drain")

    class Drainable(RecordingSink):
        drained = False

        def drain(self):
            self.drained = True

    inner = Drainable()
    MeteredSink(inner, VisualState()).drain()
    assert inner.drained


def test_bridge_survives_face_errors():
    class BrokenVisual:
        def set_state(self, state):
            raise RuntimeError("visage en panne")

    seen = []
    FaceBridge(BrokenVisual(), forward=lambda kind, text: seen.append(kind)).on_event("wake", "")
    assert seen == ["wake"]


# --- Serveur -------------------------------------------------------------------------------------

@pytest.fixture
def server():
    visual = VisualState()
    face = FaceServer(visual, "127.0.0.1", 0, rate_hz=50)
    url = face.start()
    yield face, visual, url
    face.stop()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def test_server_serves_the_face(server):
    face, visual, url = server
    status, kind, body = get(url)
    assert status == 200 and kind.startswith("text/html") and b"face.js" in body and b"JARVIS</title>" in body
    import re

    visible = re.sub(r"<[^>]+>", "", body.decode().split("<body>", 1)[1])
    assert b"<canvas" in body and visible.strip() == ""
    for name, expected in (("face.js", "javascript"), ("face.css", "css")):
        status, kind, body = get(url + name)
        assert status == 200 and expected in kind and body
    js = get(url + "face.js")[2].decode()
    for api in ("setVisualState", "setAudioLevel", "standby", "EventSource"):
        assert api in js


def test_server_streams_state_changes(server):
    face, visual, url = server
    visual.set_state("thinking")
    with urllib.request.urlopen(url + "events", timeout=5) as response:
        assert response.headers.get("Content-Type") == "text/event-stream"
        first = json.loads(response.readline().decode().removeprefix("data: "))
        assert first["state"] == "thinking"
        visual.set_state("speaking")
        states = set()
        for _ in range(20):
            line = response.readline().decode()
            if line.startswith("data: "):
                states.add(json.loads(line[6:])["state"])
            if "speaking" in states:
                break
        assert "speaking" in states
    assert json.loads(get(url + "state")[2])["state"] == "speaking"


def test_server_refuses_unknown_paths(server):
    face, visual, url = server
    for path in ("secret.txt", "../jarvis/config.py", "..%2F..%2Fconfig.toml", "static/face.js"):
        with pytest.raises(urllib.error.HTTPError) as err:
            get(url + path)
        assert err.value.code == 404


def test_busy_port_does_not_stop_jarvis(server):
    face, visual, url = server
    port = int(url.rstrip("/").rsplit(":", 1)[1])
    assert FaceServer(VisualState(), "127.0.0.1", port).start() is None


def test_face_can_be_disabled_or_unavailable():
    from dataclasses import replace

    from jarvis.__main__ import start_face

    cfg = load_config(ROOT / "config.toml")
    assert cfg.face.enabled and cfg.face.host == "127.0.0.1"
    assert start_face(replace(cfg, face=replace(cfg.face, enabled=False))) is None
    blocker = FaceServer(VisualState(), "127.0.0.1", 0)
    url = blocker.start()
    try:
        port = int(url.rstrip("/").rsplit(":", 1)[1])
        assert start_face(replace(cfg, face=replace(cfg.face, port=port, open_browser=False))) is None
    finally:
        blocker.stop()


# --- Agent complet : le visage suit la conversation sans la modifier -----------------------------

class RecordingVisual(VisualState):
    def __init__(self):
        super().__init__()
        self.history = []

    def set_state(self, state):
        super().set_state(state)
        self._note()

    def speak(self, audio, rate):
        super().speak(audio, rate)
        self._note()

    def _note(self):
        state = self._state
        if not self.history or self.history[-1] != state:
            self.history.append(state)


def run_agent(visual=None):
    class Stt:
        def __init__(self):
            self.replies = iter(["Quelle est la capitale de l'Australie ?", "Merci Jarvis"])

        def transcribe(self, audio, rate):
            return next(self.replies)

    class Llm:
        def chat(self, messages):
            return "Canberra, monsieur."

    class Tts:
        def synthesize(self, text):
            return tone(0.3, 8000), SR

    class Wake:
        def process(self, frame):
            return 1.0 if np.abs(frame).max() > 20000 else 0.0

        def reset(self):
            pass

    speech = lambda s: (3000 * np.sin(np.arange(int(s * SR)) / SR * 1400)).astype(np.int16)  # noqa: E731
    silence = lambda s: np.zeros(int(s * SR), np.int16)  # noqa: E731
    audio = np.concatenate([silence(1), np.full(3200, 30000, np.int16), silence(0.5),
                            speech(1), silence(1.5), speech(1), silence(3)])
    source, sink, events = ArraySource(audio, SR, 1280), RecordingSink(), []
    on_event = lambda kind, text: events.append(kind)  # noqa: E731
    if visual is not None:
        sink = MeteredSink(sink, visual)
        on_event = FaceBridge(visual, forward=on_event).on_event
    router = IntentRouter(load_personality(ROOT / "personality.toml"), CapabilityRegistry())
    settings = AgentSettings("JARVIS", "Jarvis", 0.5, ("Oui, monsieur ?",), 3.0, 2.0, 6)
    Agent(settings, source, sink, Wake(), UtteranceRecorder(source, EndpointerSettings()),
          Stt(), Llm(), Tts(), router, on_event).run()
    played = sink._sink.played if visual is not None else sink.played
    return events, [len(a) for a, _ in played]


def test_face_follows_a_whole_conversation():
    visual = RecordingVisual()
    run_agent(visual)
    assert visual.history == ["standby", "listening", "speaking", "listening", "thinking", "speaking",
                              "listening", "thinking", "speaking", "standby"]


def test_face_has_no_impact_on_the_voice_pipeline():
    assert run_agent(RecordingVisual()) == run_agent(None)
