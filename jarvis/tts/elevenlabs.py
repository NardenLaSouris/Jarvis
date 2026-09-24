"""Synthèse vocale via l'API ElevenLabs (service cloud)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import numpy as np

API_URL = "https://api.elevenlabs.io/v1/text-to-speech"


class TTSError(RuntimeError):
    pass


class ElevenLabsTTS:
    def __init__(
        self,
        voice_id: str,
        api_key: str,
        model: str = "eleven_multilingual_v2",
        output_format: str = "pcm_22050",
        voice_settings: dict | None = None,
        timeout: float = 30.0,
    ):
        if not api_key:
            raise TTSError("Clé API ElevenLabs absente (variable ELEVENLABS_API_KEY ou fichier .env).")
        if not output_format.startswith("pcm_"):
            raise TTSError(f"Format « {output_format} » non pris en charge : utilisez un format pcm_<fréquence>.")
        self._url = f"{API_URL}/{voice_id}?output_format={output_format}"
        self._api_key = api_key
        self._model = model
        self._settings = voice_settings or {}
        self._timeout = timeout
        self.sample_rate = int(output_format.split("_", 1)[1])
        self.voice_name = f"elevenlabs:{voice_id}"

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        payload = {"text": text, "model_id": self._model}
        if self._settings:
            payload["voice_settings"] = self._settings
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/pcm"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise TTSError(f"ElevenLabs a répondu {exc.code} : {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TTSError(f"ElevenLabs injoignable : {exc}") from exc
        audio = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.int16)
        return audio, self.sample_rate
