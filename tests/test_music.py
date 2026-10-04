"""Musique relayée comme la voix : lecteur de l'agent (flux factice), routes /audio/music, relais du Core.

Aucun son : le flux PortAudio est remplacé par un faux, l'agent écoute sur 127.0.0.1.
"""

from __future__ import annotations

import io
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.audio.music import CHUNK, DuckingSink, MusicRelay  # noqa: E402
from jarvis.winagent.config import AgentConfig  # noqa: E402
from jarvis.winagent.music import MusicPlayer  # noqa: E402
from jarvis.winagent.server import AgentServer  # noqa: E402

TOKEN = "t" * 40


class FakeStream:
    def __init__(self, rate, channels, device, callback):
        self.rate, self.channels, self.callback, self.started = rate, channels, callback, False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass


def player():
    streams = []
    music = MusicPlayer(open_stream=lambda *a: streams.append(FakeStream(*a)) or streams[-1], sleep=lambda s: None)
    return music, streams


def test_player_queues_stereo_and_mono_and_fills_silence():
    music, streams = player()
    stereo = np.array([[1000, -1000]] * 441, dtype="<i2").tobytes()
    music.feed(stereo, 44100, 2)
    music.feed(np.full(441, 500, dtype="<i2").tobytes(), 44100, 1)
    assert streams[0].rate == 44100 and streams[0].started and round(music.buffered_seconds, 2) == 0.02
    out = music.take(1000)
    assert out.shape == (1000, 2) and out[0].tolist() == [1000, -1000] and out[500].tolist() == [500, 500]
    assert not out[900:].any()  # file vide : silence


def test_duck_lowers_gradually_and_stop_empties_the_queue():
    music, _ = player()
    music.feed(np.full((44100, 2), 10000, dtype="<i2").tobytes(), 44100, 2)
    music.duck(0.15)
    levels = [int(music.take(1024)[-1, 0]) for _ in range(8)]
    assert levels[0] < 10000 and levels[-1] == 1500 and levels == sorted(levels, reverse=True)
    music.stop()
    assert music.buffered_seconds == 0 and not music.take(10).any()


class RecordingMusic:
    def __init__(self):
        self.fed, self.ducks, self.volumes, self.stops, self.buffered_seconds = [], [], [], 0, 0.0

    def feed(self, pcm, rate, channels):
        self.fed.append((len(pcm), rate, channels))

    def duck(self, level):
        self.ducks.append(level)

    def set_volume(self, level):
        self.volumes.append(level)

    def stop(self):
        self.stops += 1

    def close(self):
        pass


def agent_with(music):
    config = AgentConfig("127.0.0.1", 0, frozenset({"127.0.0.1"}), TOKEN)
    agent = AgentServer(config, music=music)
    agent.start()
    return agent, f"http://127.0.0.1:{agent.address[1]}"


def post(url, body=b"", token=TOKEN):
    request = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_agent_music_routes_need_the_token():
    music = RecordingMusic()
    agent, url = agent_with(music)
    try:
        assert post(f"{url}/audio/music?rate=44100&channels=2", b"\0" * 400) == 200
        assert post(f"{url}/audio/music/duck?percent=15") == 200 and music.ducks == [0.15]
        assert post(f"{url}/audio/music/stop") == 200 and music.stops == 1
        assert post(f"{url}/audio/music?rate=44100&channels=2", b"\0" * 3) == 400
        assert post(f"{url}/audio/music/stop", token="x" * 40) == 401 and music.stops == 1
        assert music.fed == [(400, 44100, 2)]
    finally:
        agent.stop()


def test_relay_sends_chunks_ducks_during_conversations_and_voice(tmp_path):
    music = RecordingMusic()
    agent, url = agent_with(music)
    try:
        relay = MusicRelay(tmp_path / "music.fifo", url, TOKEN, duck_percent=20, restore_after=0.05)
        relay._relay(io.BytesIO(b"\1" * (CHUNK * 2 + 100)))  # le reste incomplet attend la suite du tube
        assert music.fed == [(CHUNK, 44100, 2)] * 2
        relay.on_event("wake")
        relay.on_event("listening")
        relay.on_event("sleep")
        assert music.ducks == [0.2, 1.0]
        played = []
        sink = DuckingSink(type("Sink", (), {"play": lambda self, a, r: played.append(r), "drain": lambda self: None})(),
                           relay)
        sink.play(np.zeros(1600, np.int16), 16000)
        assert played == [16000] and music.ducks[-1] == 0.2
        time.sleep(0.4)
        assert music.ducks[-1] == 1.0  # fin de la phrase : la musique remonte
        relay.stop()
        assert music.stops == 1
    finally:
        agent.stop()


def test_preferred_spotify_device_is_used(tmp_path):
    from test_spotify import FakeSpotify, client

    fake = FakeSpotify(devices=[{"id": "pc", "name": "PC_FIXE", "type": "Computer", "is_active": True},
                                {"id": "jv", "name": "JARVIS", "type": "Speaker", "is_active": False}])
    spotify = client(tmp_path, fake)
    spotify.device = "JARVIS"
    spotify.play("Back in Black", "track")
    assert [u for u in fake.urls if "/me/player/play" in u][-1].endswith("device_id=jv")
