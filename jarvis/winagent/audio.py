"""Relais audio de l'agent : le micro du PC est diffusé au Core, la voix du Core est jouée sur le PC.

Le micro n'est ouvert que pendant qu'un Core est connecté, et par un seul à la fois. L'audio circule
en PCM int16 little-endian mono, comme dans le pipeline du Core.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator

import numpy as np


class AudioBusy(Exception):
    pass


class AudioRelay:
    def __init__(self, input_device: str = "", output_device: str = "",
                 open_microphone: Callable[[int, int], object] | None = None,
                 open_speaker: Callable[[], object] | None = None):
        self._open_microphone = open_microphone or (lambda rate, frame: _microphone(rate, frame, input_device))
        self._open_speaker = open_speaker or (lambda: _speaker(output_device))
        self._speaker = None
        self._capture = threading.Lock()
        self._playback = threading.Lock()

    @contextmanager
    def microphone(self, sample_rate: int, frame_samples: int) -> Iterator[object]:
        """Micro ouvert pour un seul Core (AudioBusy sinon), refermé à la déconnexion."""
        if not self._capture.acquire(blocking=False):
            raise AudioBusy
        try:
            mic = self._open_microphone(sample_rate, frame_samples)
            try:
                yield mic
            finally:
                mic.close()
        finally:
            self._capture.release()

    def play(self, pcm: bytes, sample_rate: int) -> None:
        with self._playback:
            if self._speaker is None:
                self._speaker = self._open_speaker()
            self._speaker.play(np.frombuffer(pcm, dtype="<i2").astype(np.int16), sample_rate)

    def drain(self) -> None:
        with self._playback:
            if self._speaker is not None:
                self._speaker.drain()

    def close(self) -> None:
        with self._playback:
            if self._speaker is not None:
                self._speaker.close()
                self._speaker = None


def _microphone(sample_rate: int, frame_samples: int, device: str):
    from jarvis.audio.devices import MicrophoneSource

    return MicrophoneSource(sample_rate, frame_samples, device)


def _speaker(device: str):
    from jarvis.audio.devices import SpeakerSink

    return SpeakerSink(device)
