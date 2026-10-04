"""Musique jouée sur le PC par l'agent, à côté de la voix de JARVIS (même sortie audio, flux séparé).

Le Core envoie de la musique PCM int16 stéréo (Spotify, via librespot sur le Core) par petits morceaux ;
l'agent les met en file et un flux PortAudio en rappel (callback) les joue sans jamais bloquer. Quand JARVIS écoute
ou parle, la musique est baissée (``duck``), puis remise au niveau. La file est bornée : quand elle est pleine,
l'envoi du Core attend, ce qui cale la lecture sur le temps réel.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable

import numpy as np

MAX_BUFFER_S = 2.0
FADE = 0.05  # vitesse de variation du gain par bloc (évite les claquements)


class MusicPlayer:
    def __init__(self, output_device: str = "", open_stream: Callable[..., object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._device = output_device
        self._open_stream = open_stream
        self._sleep = sleep
        self._lock = threading.Lock()
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._buffered = 0
        self._stream = None
        self._rate = 0
        self._channels = 2
        self.volume = 1.0
        self.duck_level = 1.0
        self._gain = 1.0

    # --- File d'attente ------------------------------------------------------------------------------

    @property
    def buffered_seconds(self) -> float:
        return self._buffered / self._rate if self._rate else 0.0

    def feed(self, pcm: bytes, rate: int, channels: int) -> None:
        """Ajoute un morceau ; attend (au plus quelques secondes) tant que la file dépasse MAX_BUFFER_S."""
        frames = np.frombuffer(pcm, dtype="<i2").astype(np.int16).reshape(-1, channels)
        if channels == 1:
            frames = np.repeat(frames, 2, axis=1)
        with self._lock:
            if self._stream is None or self._rate != rate:
                self._open(rate)
        deadline = time.monotonic() + 5.0
        while self.buffered_seconds > MAX_BUFFER_S and time.monotonic() < deadline:
            self._sleep(0.05)
        with self._lock:
            self._chunks.append(frames)
            self._buffered += len(frames)

    def stop(self) -> None:
        """Vide la file : la musique s'arrête aussitôt (pause demandée)."""
        with self._lock:
            self._chunks.clear()
            self._buffered = 0

    def duck(self, level: float) -> None:
        self.duck_level = min(1.0, max(0.0, float(level)))

    def set_volume(self, level: float) -> None:
        self.volume = min(1.0, max(0.0, float(level)))

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            self._chunks.clear()
            self._buffered = 0
        if stream is not None:
            stream.stop()
            stream.close()

    # --- Lecture ---------------------------------------------------------------------------------------

    def _open(self, rate: int) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        opener = self._open_stream or _sounddevice_stream
        self._stream = opener(rate, 2, self._device, self._callback)
        self._rate = rate
        self._stream.start()

    def take(self, count: int) -> np.ndarray:
        """``count`` images stéréo (silence si la file est vide), au gain courant."""
        out = np.zeros((count, 2), dtype=np.int16)
        filled = 0
        with self._lock:
            while filled < count and self._chunks:
                chunk = self._chunks[0]
                n = min(count - filled, len(chunk))
                out[filled:filled + n] = chunk[:n]
                if n == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[n:]
                filled += n
                self._buffered -= n
        target = self.volume * self.duck_level
        start, self._gain = self._gain, self._gain + max(-FADE * 4, min(FADE * 4, target - self._gain))
        if start == 1.0 and self._gain == 1.0:
            return out
        ramp = np.linspace(start, self._gain, count, dtype=np.float32)[:, None]
        return np.clip(out.astype(np.float32) * ramp, -32768, 32767).astype(np.int16)

    def _callback(self, outdata, frames, time_info, status) -> None:
        outdata[:] = self.take(frames)


def _sounddevice_stream(rate: int, channels: int, device: str, callback):
    import sounddevice as sd

    from jarvis.audio.devices import _device

    return sd.OutputStream(samplerate=rate, channels=channels, dtype="int16", device=_device(device),
                           callback=callback, blocksize=1024, latency="high")
