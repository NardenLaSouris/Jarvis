"""Client minimal pour l'API HTTP locale d'Ollama (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Iterator

from jarvis.interfaces import Message


PRIME_TIMEOUT = 900.0


class LLMError(RuntimeError):
    """Échec d'un appel au LLM. ``kind`` : « unreachable » (machine ou service injoignable), « timeout » (pas de
    réponse dans le délai) ou « error » (réponse d'erreur d'Ollama : modèle absent, requête refusée...)."""

    kind = "error"


class LLMUnreachable(LLMError):
    kind = "unreachable"


class LLMTimeout(LLMError):
    kind = "timeout"


def _network_error(url: str, exc: BaseException) -> LLMError:
    """URLError, délai dépassé ou connexion coupée -> erreur typée (injoignable ou délai dépassé)."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (TimeoutError, socket.timeout)) or isinstance(exc, (TimeoutError, socket.timeout)):
        return LLMTimeout(f"Ollama ne répond pas à temps sur {url}")
    return LLMUnreachable(f"Ollama injoignable sur {url} : {reason}")


class OllamaLLM:
    def __init__(
        self,
        host: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 200,
        keep_alive: str = "30m",
        timeout: float = 120.0,
        think: bool | None = None,
        num_ctx: int | None = None,
    ):
        self._url = host.rstrip("/")
        self._model = model
        self._options = {"temperature": temperature, "num_predict": max_tokens}
        if num_ctx:  # contexte du modèle : trop court, Ollama tronque les longs prompts (planificateur) sans le dire
            self._options["num_ctx"] = int(num_ctx)
        self._keep_alive = keep_alive
        self._timeout = timeout
        self._think = think
        self.last_stats: dict[str, float] = {}
        self.last_done_reason = ""

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        request = urllib.request.Request(
            self._url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LLMError(f"Ollama a répondu {exc.code} : {detail[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise _network_error(self._url, exc) from exc
        except ValueError as exc:
            raise LLMError("Réponse d'Ollama illisible") from exc

    def _chat_payload(self, messages: list[Message], stream: bool) -> dict:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": self._options,
        }
        if self._think is not None:  # modèles à réflexion (Qwen3...) : false pour répondre tout de suite
            payload["think"] = self._think
        return payload

    def _record_stats(self, data: dict) -> None:
        self.last_done_reason = data.get("done_reason", "")
        self.last_stats = {
            "chargement": data.get("load_duration", 0) / 1e9,
            "prompt": data.get("prompt_eval_duration", 0) / 1e9,
            "génération": data.get("eval_duration", 0) / 1e9,
            "jetons": data.get("eval_count", 0),
        }

    def chat(self, messages: list[Message]) -> str:
        data = self._post("/api/chat", self._chat_payload(messages, stream=False))
        self._record_stats(data)
        return data["message"]["content"].strip()

    def chat_json(self, messages: list[Message], schema: dict) -> dict:
        """Réponse JSON contrainte par un schéma (sortie structurée d'Ollama), température nulle."""
        payload = self._chat_payload(messages, stream=False)
        payload["format"] = schema
        payload["options"] = {**self._options, "temperature": 0}
        data = self._post("/api/chat", payload)
        self._record_stats(data)
        try:
            return json.loads(data["message"]["content"])
        except (KeyError, ValueError) as exc:
            raise LLMError("Réponse JSON d'Ollama illisible") from exc

    def stream(self, messages: list[Message]) -> Iterator[str]:
        """Fragments de la réponse au fil de la génération. Fermer l'itérateur interrompt Ollama."""
        request = urllib.request.Request(
            self._url + "/api/chat",
            data=json.dumps(self._chat_payload(messages, stream=True)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.last_done_reason = ""
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                for line in response:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if data.get("done"):
                        self._record_stats(data)
                        return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LLMError(f"Ollama a répondu {exc.code} : {detail[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise _network_error(self._url, exc) from exc

    def prime(self, prompts: list[list[Message]]) -> None:
        """Fait lire ces conversations à Ollama (un seul jeton généré) pour qu'il en garde le début en cache :
        les demandes qui commencent de la même façon ne paient plus cette lecture, très lente sur processeur."""
        for messages in prompts:
            payload = self._chat_payload(messages, stream=False)
            payload["options"] = {**self._options, "num_predict": 1}
            self._post("/api/chat", payload, timeout=PRIME_TIMEOUT)

    def warm_up(self) -> None:
        # Une requête sans prompt charge le modèle en mémoire sans rien générer.
        self._post("/api/generate", {"model": self._model, "keep_alive": self._keep_alive}, timeout=PRIME_TIMEOUT)
