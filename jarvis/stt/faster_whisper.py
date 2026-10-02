"""Transcription locale avec faster-whisper (CTranslate2), sur GPU NVIDIA ou CPU."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from faster_whisper import WhisperModel  # noqa: E402

log = logging.getLogger(__name__)

NO_SPEECH_THRESHOLD = 0.6


def cuda_library_dirs() -> list[Path]:
    """Dossiers des bibliothèques CUDA installées par pip (ex. nvidia-cublas-cu12)."""
    import site

    roots = [Path(p) / "nvidia" for p in site.getsitepackages() + [site.getusersitepackages()]]
    sub = "bin" if sys.platform == "win32" else "lib"
    return [d for root in roots if root.is_dir() for d in root.glob(f"*/{sub}") if d.is_dir()]


def _expose_cuda_libraries() -> None:
    dirs = [str(d) for d in cuda_library_dirs()]
    if sys.platform == "win32" and dirs:
        path = os.environ.get("PATH", "")
        missing = [d for d in dirs if d not in path]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing + [path])


def cuda_device_count() -> int:
    import ctranslate2

    return ctranslate2.get_cuda_device_count()


def detect_device() -> tuple[str, str]:
    """GPU NVIDIA utilisable -> cuda/float16, sinon cpu/int8."""
    try:
        _expose_cuda_libraries()
        if cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


class FasterWhisperSTT:
    def __init__(
        self,
        model: str,
        language: str,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        download_root: Path | None = None,
        fallback_device: str = "cpu",
        fallback_compute_type: str = "int8",
        hotwords: str = "",
    ):
        self.model_name = model
        self.hotwords = hotwords
        self._language = language
        self._beam_size = beam_size
        self._download_root = str(download_root) if download_root else None
        started = time.perf_counter()
        if device == "auto":
            device, detected_type = detect_device()
            compute_type = detected_type if compute_type == "auto" else compute_type
            log.info("STT : matériel détecté -> %s/%s", device, compute_type)
        try:
            self._load(device, compute_type)
        except Exception as exc:
            if device == fallback_device:
                raise
            log.warning("STT : %s/%s indisponible (%s) -> repli sur %s/%s",
                        device, compute_type, exc, fallback_device, fallback_compute_type)
            self._load(fallback_device, fallback_compute_type)
        self.load_time = time.perf_counter() - started
        log.info("STT device: %s", self.device.upper())
        log.info("STT compute type: %s", self.compute_type)
        log.info("STT model: %s", self.model_name)
        log.info("STT chargement : %.2f s", self.load_time)

    def _load(self, device: str, compute_type: str) -> None:
        if device == "cuda":
            _expose_cuda_libraries()
            if cuda_device_count() == 0:
                raise RuntimeError("aucun GPU CUDA détecté")
        kwargs = dict(device=device, compute_type=compute_type, download_root=self._download_root)
        try:
            model = WhisperModel(self.model_name, local_files_only=True, **kwargs)
        except Exception:
            model = WhisperModel(self.model_name, **kwargs)
        # Les bibliothèques CUDA ne sont chargées qu'au premier calcul : on le déclenche ici
        # pour qu'une erreur provoque le repli maintenant, et non pendant une conversation.
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language=self._language, beam_size=1)[0])
        self._model = model
        self.device = device
        self.compute_type = compute_type

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if sample_rate != 16000:
            raise ValueError("faster-whisper attend de l'audio à 16 kHz")
        samples = audio.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
            hotwords=self.hotwords or None,
        )
        # Whisper « invente » parfois du texte sur du bruit : on écarte ces segments.
        kept = [s.text.strip() for s in segments if s.no_speech_prob < NO_SPEECH_THRESHOLD]
        return " ".join(kept).strip()
