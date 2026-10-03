"""Serveur HTTP de l'agent Windows : le Core JARVIS y demande des actions explicitement enregistrées
et y relaie l'audio du PC.

- Toute requête d'une IP non autorisée est refusée (403), avant toute autre vérification.
- ``GET /health`` : état de l'agent (IP autorisée suffisante).
- Avec ``Authorization: Bearer <jeton>`` :
  - ``POST /actions/<nom>`` et ``{"parameters": {...}}`` : exécute l'action ``<nom>`` du registre, après
    la même validation stricte que les outils du Core ;
  - ``GET /audio/input?rate=16000&frame=1280`` : flux continu du micro (PCM int16 mono, blocs de ``frame``) ;
  - ``POST /audio/output?rate=22050`` et du PCM int16 mono : joue l'audio sur le PC ;
  - ``POST /audio/drain`` : attend la fin de la lecture en cours.

Il n'existe aucun autre point d'entrée : pas de commande libre, pas de shell.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from jarvis.face.server import ExclusiveServer
from jarvis.tools import ToolError, ToolRegistry, parse_request
from jarvis.tools.base import EXECUTION_FAILED, INVALID_PARAMETERS, TOOL_NOT_FOUND
from jarvis.winagent.audio import AudioBusy, AudioRelay
from jarvis.winagent.config import AgentConfig

log = logging.getLogger(__name__)

AGENT_NAME = "jarvis-windows"
MAX_BODY = 4096
MAX_AUDIO = 4 * 1024 * 1024
RATES = range(8000, 48001)
FRAMES = range(160, 16001)
STATUS = {TOOL_NOT_FOUND: 404, INVALID_PARAMETERS: 400}


def _client_ip(address: str) -> str:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return str(mapped or ip)


def _number(query: dict, name: str, allowed: range) -> int | None:
    values = query.get(name, [])
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) not in allowed:
        return None
    return int(values[0])


class _BaseHandler(BaseHTTPRequestHandler):
    server_version = AGENT_NAME
    sys_version = ""

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, error: str) -> None:
        self._json(status, {"status": "error", "error": error})

    def log_message(self, fmt, *args):
        log.debug("Agent : " + fmt, *args)


class AgentServer:
    def __init__(self, config: AgentConfig, actions: ToolRegistry | None = None, audio: AudioRelay | None = None):
        self.config = config
        self.actions = actions if actions is not None else ToolRegistry()
        self.audio = audio if audio is not None else AudioRelay(config.input_device, config.output_device)
        self._stopping = threading.Event()
        self._httpd: ExclusiveServer | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("Agent arrêté")
        return self._httpd.server_address[:2]

    def start(self) -> None:
        """Ouvre le port (OSError s'il est déjà pris) et sert les requêtes en arrière-plan."""
        self._stopping.clear()
        self._httpd = ExclusiveServer((self.config.host, self.config.port), self._handler())
        threading.Thread(target=self._httpd.serve_forever, name="agent-windows", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.audio.close()

    def _authorized(self, header: str) -> bool:
        scheme, _, token = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(token.encode(), self.config.token.encode())

    def _run(self, name: str, body: bytes) -> tuple[int, dict]:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = None
        try:
            if not isinstance(data, dict) or set(data) - {"parameters"}:
                raise ToolError(INVALID_PARAMETERS, "Requête mal formée.")
            request = parse_request({"tool": name, "parameters": data.get("parameters")}, self.actions)
            result = self.actions.get(request.tool).execute(dict(request.parameters))
        except ToolError as exc:
            return STATUS.get(exc.code, 422), {"status": "error", "error": exc.code, "message": exc.message}
        except Exception:
            log.exception("Action %s : erreur inattendue", name)
            return 500, {"status": "error", "error": EXECUTION_FAILED, "message": "L'action a échoué."}
        return 200, {"status": "ok", "action": name, "result": result}

    def _handler(self):
        agent = self

        class Handler(_BaseHandler):
            def do_GET(self):
                path, query = self._route()
                if path is None:
                    return
                if path == "/health":
                    return self._json(200, {"status": "ok", "agent": AGENT_NAME})
                if path != "/audio/input":
                    return self._error(404, "not_found")
                if self._authenticated():
                    self._stream_microphone(query)

            def do_POST(self):
                path, query = self._route()
                if path is None:
                    return
                if not (path.startswith("/actions/") or path in ("/audio/output", "/audio/drain")):
                    return self._error(404, "not_found")
                if not self._authenticated():
                    return
                body = self._body(MAX_AUDIO if path == "/audio/output" else MAX_BODY)
                if body is None:
                    return
                if path == "/audio/output":
                    return self._play(query, body)
                if path == "/audio/drain":
                    agent.audio.drain()
                    return self._json(200, {"status": "ok"})
                self._json(*agent._run(path.removeprefix("/actions/"), body))

            def _route(self) -> tuple[str | None, dict]:
                ip = _client_ip(self.client_address[0])
                if ip not in agent.config.allowed_ips:
                    log.warning("Agent : requête refusée depuis %s", ip)
                    self._error(403, "forbidden")
                    return None, {}
                parts = urlsplit(self.path)
                return parts.path, parse_qs(parts.query)

            def _authenticated(self) -> bool:
                if agent._authorized(self.headers.get("Authorization", "")):
                    return True
                log.warning("Agent : jeton refusé pour %s", self.client_address[0])
                self._error(401, "unauthorized")
                return False

            def _body(self, limit: int) -> bytes | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._error(400, "bad_request")
                    return None
                if not 0 <= length <= limit:
                    self._error(413, "body_too_large")
                    return None
                return self.rfile.read(length)

            def _play(self, query: dict, body: bytes) -> None:
                rate = _number(query, "rate", RATES)
                if rate is None or len(body) % 2:
                    return self._error(400, "bad_request")
                try:
                    agent.audio.play(body, rate)
                except Exception:
                    log.exception("Agent : lecture audio impossible")
                    return self._error(503, "audio_unavailable")
                self._json(200, {"status": "ok"})

            def _stream_microphone(self, query: dict) -> None:
                rate, frame = _number(query, "rate", RATES), _number(query, "frame", FRAMES)
                if rate is None or frame is None:
                    return self._error(400, "bad_request")
                started = False
                try:
                    with agent.audio.microphone(rate, frame) as mic:
                        started = True
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Sample-Rate", str(rate))
                        self.send_header("X-Frame-Samples", str(frame))
                        self.end_headers()
                        log.info("Agent : micro diffusé vers %s", self.client_address[0])
                        while not agent._stopping.is_set():
                            block = mic.read()
                            if block is None:
                                break
                            self.wfile.write(block.astype("<i2").tobytes())
                except AudioBusy:
                    self._error(409, "microphone_busy")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    log.info("Agent : flux micro interrompu par %s", self.client_address[0])
                except Exception:
                    log.exception("Agent : micro indisponible")
                    if not started:
                        self._error(503, "audio_unavailable")

        return Handler
