"""Augmentation audio : bruit de fond, distance au micro, hauteur de voix.

Tout est généré localement avec numpy. Des enregistrements d'ambiance réels
peuvent être ajoutés dans data/wakeword/noise/*.wav : ils sont alors utilisés
comme bruits de fond en plus des bruits synthétiques.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from jarvis.audio.files import read_wav
from jarvis.audio.resample import resample

SR = 16000


def colored_noise(n: int, rng: np.random.Generator, exponent: float) -> np.ndarray:
    """Bruit 1/f^exponent : 0 = blanc, 1 = rose, 2 = brun."""
    spectrum = np.fft.rfft(rng.normal(size=n))
    freqs = np.fft.rfftfreq(n)
    freqs[0] = freqs[1]
    noise = np.fft.irfft(spectrum / freqs ** (exponent / 2), n)
    return noise / (np.std(noise) + 1e-9)


def hum(n: int, rng: np.random.Generator) -> np.ndarray:
    """Ronflement électrique / ventilation (50 Hz et harmoniques)."""
    t = np.arange(n) / SR
    base = rng.choice([50.0, 60.0, 100.0])
    signal = sum(rng.uniform(0.2, 1.0) * np.sin(2 * np.pi * base * k * t + rng.uniform(0, 6.28)) for k in range(1, 5))
    signal = signal + 0.3 * colored_noise(n, rng, 1.0)
    return signal / (np.std(signal) + 1e-9)


class NoiseBank:
    def __init__(self, babble_clips: list[np.ndarray], noise_dirs: list[Path], rng: np.random.Generator):
        self._babble = babble_clips
        self._recorded = [read_wav(p) for d in noise_dirs if d.exists() for p in sorted(d.glob("*.wav"))]
        self._rng = rng

    def _babble_noise(self, n: int) -> np.ndarray:
        """Brouhaha : plusieurs conversations superposées."""
        out = np.zeros(n, dtype=np.float32)
        for _ in range(self._rng.integers(2, 6)):
            clip = self._babble[self._rng.integers(len(self._babble))].astype(np.float32)
            start = self._rng.integers(-len(clip), n) if n > 0 else 0
            lo, hi = max(start, 0), min(start + len(clip), n)
            if hi > lo:
                out[lo:hi] += clip[lo - start : hi - start]
        return out / (np.std(out) + 1e-9)

    def sample(self, n: int) -> np.ndarray:
        kinds = ["white", "pink", "brown", "hum"] + (["babble"] * 2 if self._babble else []) + (["recorded"] * 3 if self._recorded else [])
        kind = self._rng.choice(kinds)
        if kind == "babble":
            return self._babble_noise(n)
        if kind == "hum":
            return hum(n, self._rng)
        if kind == "recorded":
            audio, rate = self._recorded[self._rng.integers(len(self._recorded))]
            audio = resample(audio, rate, SR).astype(np.float32)
            reps = int(np.ceil(n / max(len(audio), 1))) + 1
            audio = np.tile(audio, reps)
            start = self._rng.integers(0, len(audio) - n)
            seg = audio[start : start + n]
            return seg / (np.std(seg) + 1e-9)
        return colored_noise(n, self._rng, {"white": 0.0, "pink": 1.0, "brown": 2.0}[kind])


def room_impulse(rng: np.random.Generator) -> np.ndarray:
    """Réponse impulsionnelle synthétique d'une pièce (RT60 entre 0,2 et 0,7 s)."""
    rt60 = rng.uniform(0.2, 0.7)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    tail = rng.normal(size=n) * np.exp(-6.9 * t / rt60)
    ir = tail * rng.uniform(0.1, 0.4)
    ir[0] = 1.0
    return ir / np.sqrt(np.sum(ir**2))


def far_field(audio: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Simule une source éloignée : réverbération et perte des aigus."""
    ir = room_impulse(rng)
    n = len(audio) + len(ir) - 1
    wet = np.fft.irfft(np.fft.rfft(audio, n) * np.fft.rfft(ir, n), n)[: len(audio)]
    alpha = rng.uniform(0.3, 0.8)  # passe-bas à un pôle
    out = np.empty_like(wet)
    acc = 0.0
    for i, x in enumerate(wet):
        acc = alpha * acc + (1 - alpha) * x
        out[i] = acc
    return out


def pitch_shift(audio: np.ndarray, factor: float) -> np.ndarray:
    """Modifie hauteur et débit ensemble (variation de locuteur)."""
    return resample(audio, SR, int(SR / factor)).astype(np.float32)
