"""Rééchantillonnage audio sans dépendance externe."""

from __future__ import annotations

import numpy as np


def resample(audio: np.ndarray, src_rate: int, dst_rate: int, length: int | None = None) -> np.ndarray:
    """Rééchantillonnage linéaire, suffisant pour la voix."""
    if src_rate == dst_rate and (length is None or length == len(audio)):
        return audio
    n = length if length is not None else int(round(len(audio) * dst_rate / src_rate))
    x_new = np.linspace(0, len(audio) - 1, n)
    return np.interp(x_new, np.arange(len(audio)), audio.astype(np.float32)).astype(np.int16)
