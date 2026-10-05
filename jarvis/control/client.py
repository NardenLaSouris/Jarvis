"""Client de l'API d'administration du Core (jarvis.api) pour JARVIS Control. Le jeton ne quitte jamais ce client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

ERRORS = {
    "unauthorized": "Jeton refusé par le Core : vérifiez le jeton dans les paramètres.",
    "forbidden": "Le Core refuse ce PC : ajoutez son adresse IP à [api] allowed_ips.",
    "confirmation_required": "Cette action demande une confirmation vocale : elle n'est pas disponible ici.",
    "already_running": "Cette routine est déjà en cours.",
    "routine_not_found": "Routine introuvable (supprimée entre-temps ?).",
    "tool_not_found": "Outil inconnu du Core.",
}


class CoreError(Exception):
    pass


class CoreClient:
    def __init__(self, base_url: str, token: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout

    def _call(self, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("POST", "PUT") else None)
        request = urllib.request.Request(f"{self.base_url}/api{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                payload = {}
            code = payload.get("error", "")
            raise CoreError(payload.get("message") or ERRORS.get(code) or f"Erreur du Core ({exc.code} {code}).") from exc
        except (OSError, ValueError) as exc:
            raise CoreError(f"Core injoignable ({self.base_url}).") from exc

    def health(self) -> dict:
        return self._call("GET", "/health")

    def get_status(self) -> dict:
        return self._call("GET", "/status")

    def get_devices(self) -> list:
        return self._call("GET", "/devices")

    def get_face(self) -> dict:
        return self._call("GET", "/face")

    def get_tools(self) -> list:
        return self._call("GET", "/tools")

    def call_tool(self, name: str, parameters: dict) -> dict:
        return self._call("POST", f"/tools/{quote(name, safe='')}", {"parameters": parameters})

    def get_routines(self) -> list:
        return self._call("GET", "/routines")

    def create_routine(self, routine: dict) -> dict:
        return self._call("POST", "/routines", routine)

    def update_routine(self, routine_id: str, routine: dict) -> dict:
        return self._call("PUT", f"/routines/{quote(routine_id, safe='')}", routine)

    def delete_routine(self, routine_id: str) -> dict:
        return self._call("DELETE", f"/routines/{quote(routine_id, safe='')}")

    def set_routine_enabled(self, routine_id: str, enabled: bool) -> dict:
        return self._call("POST", f"/routines/{quote(routine_id, safe='')}/{'enable' if enabled else 'disable'}")

    def duplicate_routine(self, routine_id: str) -> dict:
        return self._call("POST", f"/routines/{quote(routine_id, safe='')}/duplicate")

    def run_routine(self, routine_id: str) -> dict:
        return self._call("POST", f"/routines/{quote(routine_id, safe='')}/run")

    def get_history(self, kind: str = "all", limit: int = 300) -> list:
        return self._call("GET", "/history?" + urlencode({"kind": kind, "limit": limit}))
