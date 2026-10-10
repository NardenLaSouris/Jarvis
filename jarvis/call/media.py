"""Transport audio d'un appel par WebRTC (aiortc), dans la passerelle d'appel seulement (environnement Python à
part : aiortc, PyAV, numpy).

- micro du téléphone (Opus 48 kHz) -> blocs PCM int16 mono 16 kHz, lus par le Core sur ``/audio/input`` ;
- voix d'ORION (PCM int16 mono, 22 050 Hz en général) -> 48 kHz, envoyée au téléphone au rythme réel (silence
  entre les phrases) ;
- réseau local ou VPN seulement : aucun serveur STUN ni TURN (aucune adresse ne sort de la maison).
aiortc est asynchrone : il tourne dans son propre fil, à côté du serveur HTTP de la passerelle.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import threading
import time
from typing import Callable

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

log = logging.getLogger(__name__)

RATE = 48000
SAMPLES = 960  # 20 ms
MIC_RATE = 16000
MAX_MIC_BYTES = 2 * MIC_RATE * 10  # 10 s gardées au plus (le Core lit en continu)


def resample(pcm: np.ndarray, rate: int, target: int) -> np.ndarray:
    """PCM int16 mono -> autre fréquence, par le rééchantillonneur de PyAV (filtré, sans repliement)."""
    if rate == target or pcm.size == 0:
        return pcm.astype(np.int16)
    frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=target)
    out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    out += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]  # fin du flux : reste du filtre
    return np.concatenate(out).astype(np.int16) if out else np.zeros(0, np.int16)


class ByteStream:
    """Octets du micro de l'appel, du fil d'aiortc vers la réponse HTTP qui les sert au Core."""

    def __init__(self, limit: int = MAX_MIC_BYTES):
        self._data = bytearray()
        self._limit = limit
        self._cond = threading.Condition()
        self.closed = False

    def write(self, data: bytes) -> None:
        with self._cond:
            self._data += data
            if len(self._data) > self._limit:
                del self._data[: len(self._data) - self._limit]
            self._cond.notify_all()

    def read(self, size: int, timeout: float) -> bytes | None:
        """``size`` octets, ou b"" si rien n'est venu à temps, ou None une fois fermé."""
        until = time.monotonic() + timeout
        with self._cond:
            while len(self._data) < size and not self.closed and (left := until - time.monotonic()) > 0:
                self._cond.wait(left)  # réveillé à chaque bloc de 20 ms : on attend d'en avoir assez
            if len(self._data) >= size:
                chunk = bytes(self._data[:size])
                del self._data[:size]
                return chunk
            return None if self.closed else b""

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class VoiceTrack(MediaStreamTrack):
    """Voix d'ORION vers le téléphone : blocs de 20 ms au rythme réel, silence quand ORION se tait."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._buffer = np.zeros(0, np.int16)
        self._lock = threading.Lock()
        self._start: float | None = None
        self._timestamp = 0

    def push(self, pcm48: np.ndarray) -> None:
        with self._lock:
            self._buffer = np.concatenate([self._buffer, pcm48.astype(np.int16)])

    def clear(self) -> None:
        with self._lock:
            self._buffer = np.zeros(0, np.int16)

    def pending(self) -> int:
        with self._lock:
            return int(self._buffer.size)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.time()
        else:
            self._timestamp += SAMPLES
            wait = self._start + self._timestamp / RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        with self._lock:
            chunk, self._buffer = self._buffer[:SAMPLES], self._buffer[SAMPLES:]
        if chunk.size < SAMPLES:
            chunk = np.concatenate([chunk, np.zeros(SAMPLES - chunk.size, np.int16)])
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


class CallMedia:
    """Une connexion WebRTC à la fois (un seul appel). ``on_connected`` / ``on_failed`` : appelés avec l'appel."""

    def __init__(self, on_connected: Callable[[str], None], on_failed: Callable[[str, str], None]):
        self._on_connected, self._on_failed = on_connected, on_failed
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="webrtc", daemon=True).start()
        self._pc: RTCPeerConnection | None = None
        self._call_id = ""
        self.voice: VoiceTrack | None = None
        self.mic: ByteStream | None = None
        self._out_rate = 0
        self._out_resampler = None

    def _run(self, coro, timeout: float = 15.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # --- Signalisation ---------------------------------------------------------------------------------------

    def answer(self, call_id: str, sdp: str, kind: str = "offer") -> dict:
        """Offre SDP du téléphone -> réponse SDP (candidats ICE inclus : pas d'échange de candidats à part)."""
        return self._run(self._answer(call_id, sdp, kind))

    async def _answer(self, call_id: str, sdp: str, kind: str) -> dict:
        await self._close()
        pc = self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._call_id = call_id
        self.voice, self.mic = VoiceTrack(), ByteStream()
        self._out_rate, self._out_resampler = 0, None
        pc.addTrack(self.voice)
        mic = self.mic

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                asyncio.ensure_future(self._pump(track, mic))

        @pc.on("connectionstatechange")
        async def on_state():
            state = pc.connectionState
            log.info("Appel %s : audio %s", call_id, state)
            if state == "connected":
                self._on_connected(call_id)
            elif state == "failed" and self._pc is pc:
                self._on_failed(call_id, "connexion audio perdue")

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
        await pc.setLocalDescription(await pc.createAnswer())  # attend la collecte des candidats ICE
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def _pump(self, track, mic: ByteStream) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=MIC_RATE)
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    mic.write(out.to_ndarray().astype("<i2").tobytes())
        except MediaStreamError:
            pass
        finally:
            mic.close()

    # --- Voix d'ORION --------------------------------------------------------------------------------------------

    def play(self, pcm: np.ndarray, rate: int) -> None:
        """Voix d'ORION en morceaux successifs : un seul rééchantillonneur par fréquence, gardé d'un morceau à
        l'autre (repartir de zéro à chaque morceau faisait des clics aux jointures)."""
        voice = self.voice
        if voice is None or pcm.size == 0:
            return
        if rate == RATE:
            voice.push(pcm.astype(np.int16))
            return
        if self._out_rate != rate or self._out_resampler is None:
            self._out_rate, self._out_resampler = rate, av.AudioResampler(format="s16", layout="mono", rate=RATE)
        frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = rate
        for out in self._out_resampler.resample(frame):
            voice.push(out.to_ndarray().reshape(-1))

    def drain(self, timeout: float = 120.0) -> None:
        """Attend que la voix en attente soit partie vers le téléphone."""
        until = time.monotonic() + timeout
        while (voice := self.voice) is not None and voice.pending() and time.monotonic() < until:
            time.sleep(0.05)

    def stop_voice(self) -> None:
        voice = self.voice
        if voice is not None:
            voice.clear()

    # --- Fin -----------------------------------------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._run(self._close(), timeout=5)
        except Exception:  # noqa: BLE001 - fermeture au mieux
            log.debug("Fermeture WebRTC incomplète", exc_info=True)

    async def _close(self) -> None:
        pc, self._pc = self._pc, None
        if self.mic is not None:
            self.mic.close()
        if self.voice is not None:
            self.voice.stop()
        if pc is not None:
            await pc.close()
