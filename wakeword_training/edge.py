"""Voix neuronales Microsoft Edge (module edge-tts) pour diversifier les exemples du mot de réveil.

Service en ligne, utilisé seulement pour fabriquer des exemples d'entraînement : on ne lui envoie que les textes de
la spécification (le mot, des phrases neutres, des mots pièges), jamais de donnée personnelle. Le modèle obtenu et
son utilisation restent locaux.

Chaque voix devient plusieurs « locuteurs » : un débit et une hauteur de base propres à chacun (tirés d'après le nom
et le numéro), puis de légères variations à chaque clip. Le mot final d'une phrase porteuse est découpé grâce aux
repères de mots que renvoie Edge (équivalent de l'alignement des phonèmes de Piper).
"""

from __future__ import annotations

import asyncio
import random
import re
import time

import numpy as np

PREFIX = "edge-"
SR = 16000


class EdgeVoice:
    has_alignment = True  # repères de mots : le mot final d'une phrase porteuse peut être découpé

    def __init__(self, name: str):
        self.name = name[len(PREFIX):] if name.startswith(PREFIX) else name

    def base(self, speaker: int) -> tuple[int, int]:
        """Débit (%) et hauteur (Hz) de base du « locuteur »."""
        rng = random.Random(f"{self.name}-{speaker}")
        return rng.randint(-15, 20), rng.randint(-12, 10)


async def _synth(text: str, voice: str, rate: int, pitch: int) -> tuple[bytes, list[tuple[float, float, str]]]:
    import edge_tts

    com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz", boundary="WordBoundary")
    mp3, words = bytearray(), []
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            mp3 += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            words.append((chunk["offset"] / 1e7, chunk["duration"] / 1e7, chunk["text"]))
    return bytes(mp3), words


def _norm(word: str) -> str:
    return re.sub(r"[^a-zàâçéèêëîïôûùüÿœ]", "", word.lower())


def render(voice: EdgeVoice, speaker: int, text: str, target: str | None, context_s: float,
           rng: random.Random) -> np.ndarray | None:
    """Synthèse 16 kHz int16 ; avec ``target``, seulement ce mot final (+ ``context_s`` de contexte)."""
    import miniaudio

    base_rate, base_pitch = voice.base(speaker)
    rate, pitch = base_rate + rng.randint(-8, 8), base_pitch + rng.randint(-4, 4)
    for attempt in range(4):  # service en ligne : quelques reprises en cas de coupure ou de limitation
        try:
            # Délai de garde : une connexion au service qui ne répond plus bloquait le processus pour toujours.
            mp3, words = asyncio.run(asyncio.wait_for(_synth(text, voice.name, rate, pitch), timeout=30))
            break
        except Exception:  # noqa: BLE001 - réseau, limitation : nouvel essai après une pause
            time.sleep(2 + 3 * attempt)
    else:
        return None
    if not mp3:
        return None
    decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=SR)
    audio = np.frombuffer(decoded.samples, dtype=np.int16).copy()
    if target is not None:
        # Expression de plusieurs mots (« Hey Orion ») : la dernière suite de mots qui lui correspond.
        wanted = [_norm(w) for w in target.split() if _norm(w)]
        spoken = [(s, d, _norm(w)) for s, d, w in words]
        match = None
        for i in range(len(spoken) - len(wanted), -1, -1):
            if [w for _, _, w in spoken[i:i + len(wanted)]] == wanted:
                last = spoken[i + len(wanted) - 1]
                match = (spoken[i][0], last[0] + last[1] - spoken[i][0])
                break
        if match is None:
            return None
        start, duration = match
        audio = audio[max(0, int((start - context_s) * SR)): int((start + duration + 0.12) * SR)]
    return audio
