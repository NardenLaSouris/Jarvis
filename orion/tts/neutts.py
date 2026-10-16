from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_RATE = 24000
CACHE_SIZE = 64
ENCODER_REPO = "neuphonic/neucodec"


def _to_int16(audio) -> np.ndarray:
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)


def load_reference(voice: Path) -> tuple[np.ndarray, str]:
    text_path = voice.with_suffix(".txt")
    if not voice.exists() or not text_path.exists():
        raise FileNotFoundError(f"Voix de référence introuvable : {voice} (+ {text_path.name}).")
    cache = voice.with_suffix(".codes.npy")
    if cache.exists():
        codes = np.load(cache)
    else:
        codes = encode_reference(voice)
        np.save(cache, codes)
    return codes, text_path.read_text(encoding="utf-8").strip()


def encode_reference(voice: Path) -> np.ndarray:
    import librosa
    import torch
    from neucodec import NeuCodec

    codec = NeuCodec.from_pretrained(ENCODER_REPO).eval()
    wav, _ = librosa.load(str(voice), sr=16000, mono=True)
    with torch.no_grad():
        codes = codec.encode_code(audio_or_path=torch.from_numpy(wav).float()[None, None])
    return codes.squeeze().cpu().numpy()


def respell(text: str, pronunciations: dict[str, str]) -> str:
    for word, spoken in pronunciations.items():
        text = re.sub(rf"\b{re.escape(word)}\b", spoken, text, flags=re.IGNORECASE)
    return text


class NeuTTSEngine:
    def __init__(self, backbone: str, codec: str, voice: Path, hf_token: str = "",
                 pronunciations: dict[str, str] | None = None):
        if hf_token:
            os.environ.setdefault("HF_TOKEN", hf_token)
        from neutts import NeuTTS

        self._ref_codes, self._ref_text = load_reference(voice)
        self._tts = NeuTTS(backbone_repo=backbone, backbone_device="cpu", codec_repo=codec, codec_device="cpu")
        self._pronunciations = pronunciations or {}
        self._cache: dict[str, np.ndarray] = {}
        self.sample_rate = SAMPLE_RATE
        self.voice_name = f"neutts:{voice.stem}"

    def warm_up(self) -> None:
        self.synthesize("Bonjour.")

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        audio = self._cache.pop(text, None)
        if audio is None:
            audio = _to_int16(self._tts.infer(respell(text, self._pronunciations), self._ref_codes, self._ref_text))
        self._cache[text] = audio
        while len(self._cache) > CACHE_SIZE:
            del self._cache[next(iter(self._cache))]
        return audio, self.sample_rate

    def stream(self, text: str) -> Iterator[np.ndarray]:
        for chunk in self._tts.infer_stream(respell(text, self._pronunciations), self._ref_codes, self._ref_text):
            audio = _to_int16(chunk)
            if audio.size:
                yield audio
