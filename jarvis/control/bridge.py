"""Pont local d'ORION Control : sert l'interface et relaie ses demandes au Core.

N'écoute que sur 127.0.0.1. Chaque appel /bridge/* exige la clé de session (aléatoire à chaque lancement,
transmise à la fenêtre seulement) et l'hôte 127.0.0.1 : ni un autre site ouvert dans un navigateur ni un autre
programme sans la clé ne peut s'en servir. Seul ce pont détient le jeton du Core ; l'interface ne le voit jamais.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from jarvis.control.client import CoreClient, CoreError
from jarvis.control.settings import Settings, SettingsStore
from jarvis.net import JsonHandler

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
MAX_BODY = 64 * 1024


class Bridge:
    def __init__(self, store: SettingsStore, set_autostart: Callable[[bool], None] = lambda enabled: None,
                 client_factory: Callable[[str, str], CoreClient] = CoreClient):
        self.store = store
        self.key = secrets.token_urlsafe(24)
        self._set_autostart = set_autostart
        self._client_factory = client_factory
        self.settings = store.load()
        self.client = client_factory(self.settings.core_url, store.token())
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/#key={self.key}"

    def start(self, port: int = 0) -> str:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="control-bridge", daemon=True).start()
        return self.url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # --- Actions -------------------------------------------------------------------------------------

    def settings_view(self) -> dict:
        return {**asdict(self.settings), "token_configured": bool(self.store.token())}

    def save_settings(self, data: dict) -> dict:
        token = str(data.pop("token", "") or "")
        current = asdict(self.settings)
        settings = Settings(**{**current, **{k: v for k, v in data.items() if k in current}}).validated()
        if token:
            self.store.set_token(token)
        if settings.autostart != self.settings.autostart:
            self._set_autostart(settings.autostart)
        self.settings = self.store.save(settings)
        self.client = self._client_factory(self.settings.core_url, self.store.token())
        return self.settings_view()

    def handle(self, method: str, route: list[str], query: dict, data) -> object:
        c = self.client
        if method == "GET":
            simple = {("status",): c.get_status, ("devices",): c.get_devices, ("tools",): c.get_tools,
                      ("face",): c.get_face,
                      ("routines",): c.get_routines, ("settings",): self.settings_view}
            if tuple(route) in simple:
                return simple[tuple(route)]()
            if route == ["history"]:
                return c.get_history(query.get("kind", ["all"])[0], int(query.get("limit", ["300"])[0]))
        if method == "POST":
            if route == ["settings"]:
                return self.save_settings(dict(data or {}))
            if route == ["routines"]:
                return c.create_routine(data)
            if len(route) == 2 and route[0] == "tools":
                return c.call_tool(route[1], (data or {}).get("parameters") or {})
            if len(route) == 3 and route[0] == "routines":
                actions = {"run": c.run_routine, "duplicate": c.duplicate_routine,
                           "enable": lambda rid: c.set_routine_enabled(rid, True),
                           "disable": lambda rid: c.set_routine_enabled(rid, False)}
                if route[2] in actions:
                    return actions[route[2]](route[1])
        if method == "PUT" and len(route) == 2 and route[0] == "routines":
            return c.update_routine(route[1], data)
        if method == "DELETE" and len(route) == 2 and route[0] == "routines":
            return c.delete_routine(route[1])
        raise LookupError("/".join(route))

    def _handler(self):
        bridge = self

        class Handler(JsonHandler):
            server_version = "jarvis-control"

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in STATIC:
                    name, kind = STATIC[path]
                    body = (WEB / name).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._bridge("GET")

            def do_POST(self):
                self._bridge("POST")

            def do_PUT(self):
                self._bridge("PUT")

            def do_DELETE(self):
                self._bridge("DELETE")

            def _bridge(self, method: str):
                port = bridge._httpd.server_address[1]
                parts = urlsplit(self.path)
                route = [unquote(s) for s in parts.path.split("/") if s]
                if route[:1] != ["bridge"]:
                    return self._error(404, "not_found")
                if self.headers.get("Host") != f"127.0.0.1:{port}" or not hmac.compare_digest(
                        self.headers.get("X-Control-Key", "").encode(), bridge.key.encode()):
                    return self._error(403, "forbidden")
                data = None
                if method in ("POST", "PUT"):
                    body = self._body(MAX_BODY)
                    if body is None:
                        return
                    try:
                        data = json.loads(body or b"null")
                    except ValueError:
                        return self._error(400, "bad_request")
                try:
                    result = bridge.handle(method, route[1:], parse_qs(parts.query), data)
                    self._json(200, {"ok": True, "data": result})
                except CoreError as exc:
                    self._json(200, {"ok": False, "error": str(exc)})
                except (ValueError, TypeError) as exc:
                    self._json(200, {"ok": False, "error": str(exc) or "Valeur invalide."})
                except LookupError:
                    self._error(404, "not_found")
                except Exception:
                    log.exception("Pont : erreur sur %s %s", method, parts.path)
                    self._json(200, {"ok": False, "error": "Erreur interne d'ORION Control (voir le journal)."})

        return Handler
