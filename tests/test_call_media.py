"""Audio des appels par WebRTC (jarvis/call/media.py), de bout en bout en local : un client aiortc joue le téléphone.

Ignoré sans aiortc (environnement d'ORION) ; à lancer dans l'environnement de la passerelle :
    ~/orion-call-venv/bin/python -m pytest tests/test_call_media.py
"""

from __future__ import annotations

import asyncio
import fractions
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

aiortc = pytest.importorskip("aiortc")
av = pytest.importorskip("av")

from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

from jarvis.call.media import CallMedia, resample  # noqa: E402


class Tone(MediaStreamTrack):
    """Micro du téléphone simulé : un son de 440 Hz, 48 kHz, blocs de 20 ms au rythme réel."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._n = 0
        self._start = None

    async def recv(self):
        if self._start is None:
            self._start = time.time()
        else:
            wait = self._start + self._n / 48000 - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        t = (np.arange(960) + self._n) / 48000
        pcm = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 48000, self._n, fractions.Fraction(1, 48000)
        self._n += 960
        return frame


def test_resample_keeps_duration():
    pcm = (np.sin(np.arange(22050) / 22050 * 2 * np.pi * 300) * 8000).astype(np.int16)
    out = resample(pcm, 22050, 48000)
    assert abs(len(out) - 48000) < 200 and out.dtype == np.int16


def test_phone_audio_reaches_the_core_and_orion_voice_reaches_the_phone():
    connected, failed = threading.Event(), []
    media = CallMedia(lambda call_id: connected.set(), lambda call_id, why: failed.append(why))
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    run = lambda coro, timeout=15: asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)  # noqa: E731
    phone = None
    heard = []

    async def make_offer():
        nonlocal phone
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        phone.addTrack(Tone())

        @phone.on("track")
        def on_track(track):
            async def listen():
                try:
                    while True:
                        frame = await track.recv()
                        heard.append(np.abs(frame.to_ndarray()).max())
                except Exception:  # noqa: BLE001 - fin de l'appel
                    pass
            asyncio.ensure_future(listen())

        await phone.setLocalDescription(await phone.createOffer())
        return phone.localDescription

    async def accept(answer):
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    try:
        offer = run(make_offer())
        answer = media.answer("appel-test", offer.sdp, offer.type)
        assert answer["type"] == "answer" and "a=candidate" in answer["sdp"]
        run(accept(answer))
        assert connected.wait(10) and not failed
        chunk = media.mic.read(2 * 1280, timeout=5)  # le Core lit le micro du téléphone, en 16 kHz
        assert chunk and len(chunk) == 2560
        level = np.abs(np.frombuffer(chunk, dtype="<i2")).max()
        assert level > 3000  # le son de 440 Hz est bien arrivé
        voice = (np.sin(np.arange(22050) / 22050 * 2 * np.pi * 300) * 10000).astype(np.int16)
        media.play(voice, 22050)  # voix d'ORION vers le téléphone
        media.drain(timeout=5)
        time.sleep(0.5)
        assert max(heard, default=0) > 3000
    finally:
        media.close()
        if phone is not None:
            run(phone.close())
        loop.call_soon_threadsafe(loop.stop)
