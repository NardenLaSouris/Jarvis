"""Client minimal pour l'API HTTP locale d'Ollama (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from jarvis.interfaces import Message


class LLMError(RuntimeError):
    pass


class OllamaLLM:
    def __init__(
        self,
        host: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 200,
        keep_alive: str = "30m",
        timeout: float = 120.0,
    ):
        self._url = host.rstrip("/")
        self._model = model
        self._options = {"temperature": temperature, "num_predict": max_tokens}
        self._keep_alive = keep_alive
        self._timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self._url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LLMError(f"Ollama a répondu {exc.code} : {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"Ollama injoignable sur {self._url} : {exc}") from exc

    def chat(self, messages: list[Message]) -> str:
        data = self._post(
            "/api/chat",
            {
                "model": self._model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "stream": False,
                "keep_alive": self._keep_alive,
                "options": self._options,
            },
        )
        return data["message"]["content"].strip()

    def warm_up(self) -> None:
        # Une requête sans prompt charge le modèle en mémoire sans rien générer.
        self._post("/api/generate", {"model": self._model, "keep_alive": self._keep_alive})
