"""Audio distant : micro et sortie d'un PC relayés par son agent (serveur local 127.0.0.1, micro et
haut-parleur simulés : aucun périphérique ni accès réseau réel)."""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.factory as factory  # noqa: E402
from jarvis.audio.network import NetworkSink, NetworkSource  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.winagent import AgentConfig, AgentServer  # noqa: E402
from jarvis.winagent.audio import AudioRelay  # noqa: E402

TOKEN = "t" * 40
FRAME = 1280


class FakeMicrophone:
    def __init__(self, rate, frame):
        self.opened_with = (rate, frame)
        self.closed = threading.Event()
        self._count = 0

    def read(self):
        time.sleep(0.005)
        self._count += 1
        return np.full(self.opened_with[1], self._count, dtype=np.int16)

    def close(self):
        self.closed.set()


class FakeSpeaker:
    def __init__(self, error=None):
        self.played, self.drained, self.error = [], 0, error

    def play(self, audio, rate):
        if self.error:
            raise self.error
        self.played.append((audio.copy(), rate))

    def drain(self):
        self.drained += 1

    def close(self):
        pass


class Devices:
    def __init__(self, speaker=None):
        self.microphones, self.speaker = [], speaker or FakeSpeaker()

    def relay(self):
        def open_microphone(rate, frame):
            self.microphones.append(FakeMicrophone(rate, frame))
            return self.microphones[-1]

        return AudioRelay(open_microphone=open_microphone, open_speaker=lambda: self.speaker)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def agent(devices, port=0):
    server = AgentServer(AgentConfig("127.0.0.1", port, frozenset({"127.0.0.1"}), TOKEN), audio=devices.relay())
    server.start()
    return server


def url(server):
    host, port = server.address
    return f"http://{host}:{port}"


def get(server, path, token=TOKEN):
    request = urllib.request.Request(url(server) + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "délai dépassé"
        time.sleep(0.01)


def test_remote_microphone_streams_frames_and_is_released_on_close():
    devices = Devices()
    server = agent(devices)
    source = NetworkSource(url(server), TOKEN, 16000, FRAME)
    try:
        frames = [source.read() for _ in range(3)]
        assert [f.dtype for f in frames] == [np.int16] * 3 and all(f.shape == (FRAME,) for f in frames)
        assert [int(f[0]) for f in frames] == [1, 2, 3]
        assert devices.microphones[0].opened_with == (16000, FRAME)
        source.flush()
    finally:
        source.close()
    assert devices.microphones[0].closed.wait(5)
    server.stop()


def test_microphone_is_given_to_one_core_only_and_never_without_token():
    devices = Devices()
    server = agent(devices)
    source = NetworkSource(url(server), TOKEN, 16000, FRAME)
    try:
        source.read()
        assert get(server, f"/audio/input?rate=16000&frame={FRAME}") == 409
        assert get(server, f"/audio/input?rate=16000&frame={FRAME}", token="faux") == 401
        assert len(devices.microphones) == 1
    finally:
        source.close()
        server.stop()


@pytest.mark.parametrize("query", ["", "?rate=16000", "?rate=100&frame=1280", "?rate=16000&frame=99999",
                                   "?rate=16000&frame=1280&rate=8000", "?rate=-1&frame=1280"])
def test_invalid_microphone_requests_are_refused(query):
    devices = Devices()
    server = agent(devices)
    try:
        assert get(server, "/audio/input" + query) == 400 and devices.microphones == []
    finally:
        server.stop()


def test_remote_speaker_plays_the_exact_audio():
    devices = Devices()
    server = agent(devices)
    try:
        sink = NetworkSink(url(server), TOKEN)
        audio = (np.arange(-500, 500) * 30).astype(np.int16)
        sink.play(audio, 22050)
        sink.play(np.zeros(0, np.int16), 22050)
        sink.drain()
        assert len(devices.speaker.played) == 1
        played, rate = devices.speaker.played[0]
        assert rate == 22050 and np.array_equal(played, audio) and devices.speaker.drained == 1
    finally:
        server.stop()


def test_invalid_audio_output_is_refused():
    devices = Devices()
    server = agent(devices)
    try:
        for path, body in (("/audio/output?rate=22050", b"\x00\x01\x02"), ("/audio/output?rate=1", b"\x00\x00"),
                           ("/audio/output", b"\x00\x00")):
            request = urllib.request.Request(url(server) + path, data=body, method="POST",
                                             headers={"Authorization": f"Bearer {TOKEN}"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(request, timeout=5)
            assert exc.value.code == 400, path
        assert devices.speaker.played == []
    finally:
        server.stop()


def test_speaker_failure_does_not_crash_the_core(caplog):
    devices = Devices(FakeSpeaker(error=OSError("carte son absente")))
    server = agent(devices)
    try:
        sink = NetworkSink(url(server), TOKEN)
        with caplog.at_level(logging.WARNING):
            sink.play(np.ones(100, np.int16), 22050)
            sink.play(np.ones(100, np.int16), 22050)
        assert sum("Sortie audio distante injoignable" in r.message for r in caplog.records) == 1
    finally:
        server.stop()


def test_unreachable_agent_never_crashes_the_sink(caplog):
    sink = NetworkSink(f"http://127.0.0.1:{free_port()}", TOKEN, timeout=2)
    with caplog.at_level(logging.WARNING):
        sink.play(np.ones(100, np.int16), 22050)
        sink.drain()
    assert any("injoignable" in r.message for r in caplog.records)


def test_source_waits_for_the_agent_and_reconnects(caplog):
    port = free_port()
    source = NetworkSource(f"http://127.0.0.1:{port}", TOKEN, 16000, FRAME, retry_s=0.05)
    devices = Devices()
    try:
        with caplog.at_level(logging.WARNING):
            wait(lambda: any("Micro distant injoignable" in r.message for r in caplog.records))
        first = agent(devices, port)
        assert int(source.read()[0]) == 1
        first.stop()
        source.flush()
        second = agent(devices, port)
        try:
            wait(lambda: len(devices.microphones) == 2)
            assert source.read().shape == (FRAME,)
        finally:
            second.stop()
    finally:
        source.close()


def test_wrong_token_is_reported(caplog):
    devices = Devices()
    server = agent(devices)
    source = NetworkSource(url(server), "x" * 40, 16000, FRAME, retry_s=0.05)
    try:
        with caplog.at_level(logging.WARNING):
            wait(lambda: any("jeton refusé" in r.message for r in caplog.records))
        assert devices.microphones == []
    finally:
        source.close()
        server.stop()


def test_configuration_selects_remote_audio(monkeypatch):
    cfg = load_config(ROOT / "config.toml")
    assert cfg.audio.remote == ""
    remote = replace(cfg, audio=replace(cfg.audio, remote="http://127.0.0.1:9"))
    monkeypatch.setattr(factory, "secret", lambda name, env_file: TOKEN if name == "JARVIS_AGENT_TOKEN" else "")
    assert isinstance(factory.build_sink(remote), NetworkSink)
    source = factory.build_source(remote)
    try:
        assert isinstance(source, NetworkSource) and (source.sample_rate, source.frame_samples) == (16000, FRAME)
    finally:
        source.close()
    monkeypatch.setattr(factory, "secret", lambda name, env_file: "")
    with pytest.raises(ValueError, match="JARVIS_AGENT_TOKEN"):
        factory.build_sink(remote)


def test_token_never_appears_in_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        source = NetworkSource(f"http://127.0.0.1:{free_port()}", TOKEN + "\r", 16000, FRAME, retry_s=0.05)
        try:
            wait(lambda: any("injoignable" in r.message for r in caplog.records))
        finally:
            source.close()
        NetworkSink(f"http://127.0.0.1:{free_port()}", TOKEN, timeout=2).play(np.ones(10, np.int16), 22050)
    assert TOKEN not in caplog.text
    with pytest.raises(ValueError, match="invalides") as exc:
        NetworkSink("http://127.0.0.1:9", "abc def" + "x" * 40)
    assert "abc def" not in str(exc.value)
