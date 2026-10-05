"""API d'administration du Core (JARVIS Control) : HTTP sur le réseau local, IP autorisées et jeton partagé.

Toutes les actions passent par le Core : routines (moteur des routines) et outils (ToolCore : registre,
validation, permissions). Seuls les outils sans confirmation sont exécutables ici ; aucune commande libre.

  GET  /api/health                     (IP autorisée suffisante)
  GET  /api/status | /api/devices | /api/tools | /api/history?limit=&kind=routine|tool|all
  POST /api/tools/<outil>              {"parameters": {...}}
  GET  /api/routines                   POST /api/routines
  GET | PUT | DELETE /api/routines/<id>
  POST /api/routines/<id>/run | enable | disable | duplicate
"""

from __future__ import annotations

import json
import logging
import threading
from urllib.parse import parse_qs, urlsplit

from jarvis.api.status import CoreStatus, history_item
from jarvis.face.server import ExclusiveServer
from jarvis.net import JsonHandler, bearer_ok, client_ip
from jarvis.routines import RoutineEngine, RoutineError
from jarvis.tools import DONE, Risk, ToolCore

log = logging.getLogger(__name__)

API_NAME = "jarvis-core"
MAX_BODY = 64 * 1024
HISTORY_KINDS = {"routine": ("routine.",), "tool": ("tool.",), "all": ("",)}


def tool_info(tool) -> dict:
    return {"name": tool.name, "description": tool.description, "safe": tool.risk is Risk.SAFE,
            "parameters": {name: {"type": p.kind.__name__, "description": p.description, "required": p.required,
                                  "choices": list(p.choices), "minimum": p.minimum, "maximum": p.maximum,
                                  "hidden": p.hidden} for name, p in tool.parameters.items()}}


class CoreApi:
    def __init__(self, host: str, port: int, allowed_ips: frozenset[str], token: str, *, tools: ToolCore | None,
                 routines: RoutineEngine | None, status: CoreStatus, activity=None, memory=None, face_themes=None,
                 presence=None):
        if not token:
            raise ValueError("JARVIS_AGENT_TOKEN est requis pour l'API d'administration ([api]).")
        self._address = (host, port)
        self._allowed, self._token = allowed_ips, token
        self.tools, self.routines, self.status, self._activity = tools, routines, status, activity
        self.memory = memory
        self.face_themes = face_themes
        self.presence = presence
        self._httpd: ExclusiveServer | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("API arrêtée")
        return self._httpd.server_address[:2]

    def start(self) -> None:
        self._httpd = ExclusiveServer(self._address, self._handler())
        threading.Thread(target=self._httpd.serve_forever, name="api-core", daemon=True).start()
        log.info("API d'administration : http://%s:%s/api (IP autorisées : %s)", *self.address[:2],
                 ", ".join(sorted(self._allowed)) or "aucune")

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # --- Traitements ---------------------------------------------------------------------------------

    def history(self, query: dict) -> list[dict]:
        try:
            limit = max(1, min(int(query.get("limit", ["200"])[0]), 1000))
        except ValueError:
            limit = 200
        prefixes = HISTORY_KINDS.get(query.get("kind", ["all"])[0], ("",))
        records = self._activity.read(limit * 3) if self._activity is not None else []
        items = [history_item(r) for r in reversed(records) if str(r.get("type", "")).startswith(prefixes)]
        return items[:limit]

    def call_tool(self, name: str, parameters) -> tuple[int, dict]:
        if self.tools is None or not self.tools.registry.exists(name):
            return 404, {"status": "error", "error": "tool_not_found"}
        if self.tools.registry.get(name).risk is not Risk.SAFE:
            return 403, {"status": "error", "error": "confirmation_required",
                         "message": "Cet outil demande une confirmation vocale."}
        outcome = self.tools.submit({"type": "tool_call", "tool": name, "parameters": parameters or {}})
        result = outcome.result
        ok = outcome.status == DONE and result is not None and result.success
        return (200 if ok else 422), {"status": "ok" if ok else "error", "tool": name,
                                      "message": result.message if result else "", "result": result.result if ok else None,
                                      "error": None if ok else (result.error if result else outcome.status)}

    def presence_action(self, method: str, parts: list[str], data) -> tuple[int, object]:
        """GET /api/presence : état et journal ; POST /api/presence/simulate {"sensor", "value", "celsius"?,
        "name"?} : signal simulé, par le même chemin qu'un vrai capteur."""
        if self.presence is None:
            return 404, {"status": "error", "error": "presence_disabled"}
        if method == "GET" and not parts:
            return 200, self.presence.snapshot()
        if method == "POST" and parts == ["simulate"]:
            from jarvis.presence.sensors import SensorError

            if not isinstance(data, dict) or not isinstance(data.get("sensor"), str) \
                    or not isinstance(data.get("value"), str):
                return 400, {"status": "error", "error": "bad_request", "message": "sensor et value attendus."}
            extra = {k: data[k] for k in ("celsius", "name") if k in data}
            try:
                event = self.presence.simulate(data["sensor"], data["value"], extra or None)
            except SensorError as exc:
                return 400, {"status": "error", "error": "refused", "message": str(exc)}
            return 200, {"status": "ok", "event": event.as_dict(), "presence": self.presence.engine.snapshot()}
        return 404, {"status": "error", "error": "not_found"}

    def routine_action(self, method: str, parts: list[str], data) -> tuple[int, object]:
        engine = self.routines
        if engine is None:
            return 404, {"status": "error", "error": "routines_disabled"}
        try:
            if not parts:
                if method == "GET":
                    return 200, engine.list()
                return 201, engine.create(data)
            routine_id, action = parts[0], (parts[1] if len(parts) > 1 else "")
            if method == "GET" and not action:
                return 200, engine.get(routine_id)
            if method == "PUT" and not action:
                return 200, engine.update(routine_id, data)
            if method == "DELETE" and not action:
                engine.delete(routine_id)
                return 200, {"status": "ok"}
            if method == "POST" and action == "run":
                started = engine.run(routine_id)
                return (202, {"status": "started"}) if started else (409, {"status": "error", "error": "already_running"})
            if method == "POST" and action in ("enable", "disable"):
                return 200, engine.set_enabled(routine_id, action == "enable")
            if method == "POST" and action == "duplicate":
                return 201, engine.duplicate(routine_id)
        except KeyError:
            return 404, {"status": "error", "error": "routine_not_found"}
        except RoutineError as exc:
            return 400, {"status": "error", "error": "invalid_routine", "message": str(exc)}
        return 404, {"status": "error", "error": "not_found"}

    def _handler(self):
        api = self

        class Handler(JsonHandler):
            server_version = API_NAME

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def _dispatch(self, method: str):
                ip = client_ip(self.client_address[0])
                if ip not in api._allowed:
                    log.warning("API : requête refusée depuis %s", ip)
                    return self._error(403, "forbidden")
                parts = urlsplit(self.path)
                segments = [s for s in parts.path.split("/") if s]
                if segments[:1] != ["api"]:
                    return self._error(404, "not_found")
                route = segments[1:]
                if route == ["health"] and method == "GET":
                    return self._json(200, {"status": "ok", "agent": API_NAME})
                if not bearer_ok(self.headers.get("Authorization", ""), api._token):
                    log.warning("API : jeton refusé pour %s", ip)
                    return self._error(401, "unauthorized")
                data = None
                if method in ("POST", "PUT"):
                    body = self._body(MAX_BODY)
                    if body is None:
                        return
                    try:
                        data = json.loads(body or b"{}")
                    except ValueError:
                        return self._error(400, "bad_request", "JSON invalide.")
                try:
                    status, payload = self._route(method, route, parse_qs(parts.query), data)
                except Exception:
                    log.exception("API : erreur sur %s %s", method, parts.path)
                    status, payload = 500, {"status": "error", "error": "internal_error"}
                self._json(status, payload)

            def _route(self, method: str, route: list[str], query: dict, data):
                if method == "GET" and route == ["status"]:
                    return 200, api.status.snapshot()
                if method == "GET" and route == ["devices"]:
                    return 200, api.status.devices()
                if method == "GET" and route == ["tools"]:
                    return 200, [tool_info(t) for t in api.tools.registry.list()] if api.tools else []
                if method == "POST" and len(route) == 2 and route[0] == "tools":
                    parameters = data.get("parameters") if isinstance(data, dict) else None
                    return api.call_tool(route[1], parameters)
                if route[:1] == ["presence"]:
                    return api.presence_action(method, route[1:], data)
                if method == "GET" and route == ["face"]:
                    if api.face_themes is None:
                        return 404, {"status": "error", "error": "face_disabled"}
                    from jarvis.face.themes import COLORS, SEASONS, THEMES, label

                    return 200, {"theme": api.face_themes.get(),
                                 "themes": [{"id": t, "label": label(t), "hue": COLORS.get(t),
                                             "hues": SEASONS.get(t, {}).get("hues")} for t in THEMES]}
                if method == "GET" and route == ["history"]:
                    return 200, api.history(query)
                if route[:1] == ["memory"]:
                    if api.memory is None:
                        return 404, {"status": "error", "error": "memory_disabled"}
                    if method == "GET" and len(route) == 1:
                        return 200, api.memory.all()
                    if method == "DELETE" and len(route) == 2:
                        removed = api.memory.remove([route[1]])
                        return (200, {"status": "ok"}) if removed else (404, {"status": "error", "error": "not_found"})
                if route[:1] == ["routines"] and len(route) <= 3:
                    edits = (method == "POST" and len(route) == 1) or (method == "PUT" and len(route) == 2)
                    if edits and not isinstance(data, dict):
                        return 400, {"status": "error", "error": "bad_request"}
                    return api.routine_action(method, route[1:], data)
                return 404, {"status": "error", "error": "not_found"}

        return Handler
