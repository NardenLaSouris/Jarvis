"""Entrée micro et sortie haut-parleur via sounddevice (PortAudio)."""

from __future__ import annotations

import logging
import queue

import numpy as np
import sounddevice as sd

from jarvis.audio.resample import resample

log = logging.getLogger(__name__)

SILENT_PEAK = 30  # crête int16 en dessous de laquelle un micro est considéré muet


def _device(value: str) -> int | str | None:
    if not value:
        return None
    return int(value) if value.isdigit() else value


class MicrophoneSource:
    """Capture continue du micro, découpée en blocs de ``frame_samples`` à ``sample_rate``."""

    def __init__(self, sample_rate: int, frame_samples: int, device: str = "", max_buffered_s: float = 30.0):
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=int(max_buffered_s * sample_rate / frame_samples)
        )
        device_id = _device(device)
        try:
            self._stream = self._open(device_id, sample_rate)
        except sd.PortAudioError:
            # Le périphérique refuse 16 kHz : on capture à sa fréquence native et on rééchantillonne.
            native = int(sd.query_devices(device_id, "input")["default_samplerate"])
            log.info("Micro ouvert à %d Hz (rééchantillonné vers %d Hz)", native, sample_rate)
            self._stream = self._open(device_id, native)
        self._stream.start()
        name = sd.query_devices(self._stream.device)["name"]
        log.info("Micro : %s", name)
        self._check_signal(name)

    def _check_signal(self, name: str, seconds: float = 0.5) -> None:
        """Prévient si le micro choisi ne capte rien (périphérique virtuel, micro coupé...)."""
        frames = [self._queue.get() for _ in range(max(1, int(seconds * self.sample_rate / self.frame_samples)))]
        peak = int(np.abs(np.concatenate(frames).astype(np.int32)).max())
        if peak < SILENT_PEAK:
            log.warning(
                "Le micro « %s » semble muet (crête %d). Vérifiez son niveau ou choisissez-en un autre "
                "avec [audio] input_device (voir `python -m jarvis --list-devices`).", name, peak,
            )

    def _open(self, device, rate: int) -> sd.InputStream:
        blocksize = int(round(self.frame_samples * rate / self.sample_rate))

        def callback(indata, frames, time_info, status):
            if status:
                log.debug("Statut micro : %s", status)
            frame = resample(indata[:, 0].copy(), rate, self.sample_rate, self.frame_samples)
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                log.warning("Tampon micro plein, audio ignoré")

        return sd.InputStream(
            samplerate=rate, channels=1, dtype="int16", blocksize=blocksize,
            device=device, callback=callback,
        )

    def read(self) -> np.ndarray | None:
        return self._queue.get()

    def flush(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class SpeakerSink:
    def __init__(self, device: str = ""):
        self._device = _device(device)

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        if audio.size == 0:
            return
        try:
            sd.play(audio, sample_rate, device=self._device, blocking=True)
        except sd.PortAudioError:
            native = int(sd.query_devices(self._device, "output")["default_samplerate"])
            sd.play(resample(audio, sample_rate, native), native, device=self._device, blocking=True)
