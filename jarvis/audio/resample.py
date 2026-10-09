"""Rééchantillonnage audio sans dépendance externe."""

from __future__ import annotations

import numpy as np


def resample(audio: np.ndarray, src_rate: int, dst_rate: int, length: int | None = None) -> np.ndarray:
    """Rééchantillonnage linéaire, suffisant pour la voix. En réduction de fréquence (micro capturé à 48 kHz pour
    16 kHz), une moyenne glissante sur environ le rapport des fréquences précède l'interpolation : sans elle, les sons
    au-dessus de la nouvelle fréquence de Nyquist se repliaient dans la bande de la voix (STT et mot de réveil)."""
    if src_rate == dst_rate and (length is None or length == len(audio)):
        return audio
    n = length if length is not None else int(round(len(audio) * dst_rate / src_rate))
    samples = audio.astype(np.float32)
    width = int(round(src_rate / dst_rate))
    if width >= 2 and len(samples) >= width:
        padded = np.pad(samples, (width // 2, width - 1 - width // 2), mode="edge")
        samples = np.convolve(padded, np.ones(width, np.float32) / width, mode="valid").astype(np.float32)
    x_new = np.linspace(0, len(samples) - 1, n)
    return np.interp(x_new, np.arange(len(samples)), samples).astype(np.int16)
