"""Appareils du réseau : ORION agit sur un PC via l'agent qui y tourne (jarvis.winagent), jamais sur le Core.

Chaque appareil a un nom dit à voix haute, l'adresse de son agent et des alias (« mon pc portable »). Un
appareil sans adresse est interdit (le mini-PC du Core) : toute action y est refusée avant confirmation.
L'appareil visé vient des mots de la demande, jamais du LLM ; sans alias reconnu, c'est l'appareil par défaut.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from jarvis.personality import normalize
from jarvis.tools.base import EXECUTION_FAILED, Param, Tool, ToolError

log = logging.getLogger(__name__)

DEVICE_FORBIDDEN = "device_forbidden"
DEVICE_UNREACHABLE = "device_unreachable"
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class Device:
    key: str
    name: str
    url: str
    aliases: tuple[str, ...]
    default: bool = False

    @property
    def Name(self) -> str:  # noqa: N802 - nom en tête de phrase
        return self.name[:1].upper() + self.name[1:]


class AliasIndex:
    """Mots désignant une chose (appareil, pièce...) ; dans une phrase, l'alias le plus long l'emporte
    (« mon pc portable » avant « mon pc »)."""

    def __init__(self, pairs):
        self._pairs = sorted(((normalize(alias), value) for alias, value in pairs if normalize(alias)),
                             key=lambda item: -len(item[0]))

    def find(self, text: str):
        norm = f" {normalize(text)} "
        return next((value for alias, value in self._pairs if f" {alias} " in norm), None)


class Devices:
    def __init__(self, devices: list[Device]):
        defaults = [d for d in devices if d.default]
        if len(defaults) != 1 or not defaults[0].url:
            raise ValueError("[tools.devices] : un et un seul appareil par défaut, avec une adresse (url).")
        self._devices = {d.key: d for d in devices}
        self.default = defaults[0]
        self._aliases = AliasIndex((a, d) for d in devices for a in (d.name, *d.aliases))

    def __getitem__(self, key: str) -> Device:
        return self._devices[key]

    def keys(self) -> tuple[str, ...]:
        return tuple(self._devices)

    def find(self, text: str) -> Device | None:
        return self._aliases.find(text)


def load_devices(table: dict) -> Devices | None:
    """Section [tools.devices] -> appareils, ou None si elle est vide (le Core agit alors sur sa propre machine)."""
    if not table:
        return None
    devices = []
    for key, spec in table.items():
        if not KEY.match(key) or not isinstance(spec, dict):
            raise ValueError(f"[tools.devices.{key}] invalide")
        url = str(spec.get("url", "")).rstrip("/")
        if url and not re.match(r"^https?://[\w.\-]+(:\d+)?$", url):
            raise ValueError(f"[tools.devices.{key}] url invalide : {url!r}")
        devices.append(Device(key, str(spec.get("name", key)), url, tuple(str(a) for a in spec.get("aliases", [])),
                              bool(spec.get("default", False))))
    return Devices(devices)


class AgentClient:
    """Appelle ``POST /actions/<outil>`` sur l'agent d'un appareil, avec le jeton partagé."""

    def __init__(self, token: str, timeout: float = 12.0, reachable=None):
        if not token:
            raise ValueError("JARVIS_AGENT_TOKEN est requis pour agir sur les appareils ([tools.devices]).")
        self._headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}
        self._timeout = timeout
        if reachable is None:
            from jarvis.llm.failover import tcp_reachable

            reachable = lambda url: tcp_reachable(url, 1.0)  # noqa: E731
        self._reachable = reachable

    def call(self, device: Device, tool: str, parameters: dict) -> dict:
        # Vérification rapide : un PC éteint ou un agent arrêté est signalé en 1 s, pas au bout du délai complet.
        if not self._reachable(device.url):
            log.warning("Agent %s injoignable", device.url)
            raise ToolError(DEVICE_UNREACHABLE, f"{device.Name} ne répond pas.")
        body = json.dumps({"parameters": parameters}).encode()
        request = urllib.request.Request(f"{device.url}/actions/{tool}", data=body, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())["result"]
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except ValueError:
                data = {}
            if exc.code == 401:
                log.warning("Agent %s : jeton refusé", device.url)
            code = data.get("error") if isinstance(data, dict) else None
            message = data.get("message") if isinstance(data, dict) else None
            # Réponse d'agent non fiable : seuls un code et un message textuels courts sont repris.
            # Message prononcé seulement s'il est court et lisible (une réponse de 5 000 « x » était lue tronquée).
            spoken = message.strip() if isinstance(message, str) else ""
            if not spoken or len(spoken) > 200 or not spoken.isprintable() or not re.search(r"\s", spoken):
                spoken = f"{device.Name} a refusé l'action."
            raise ToolError(code[:48] if isinstance(code, str) and code else EXECUTION_FAILED, spoken) from exc
        except (OSError, ValueError, KeyError) as exc:
            log.warning("Agent %s injoignable : %s", device.url, type(exc).__name__)
            raise ToolError(DEVICE_UNREACHABLE, f"{device.Name} ne répond pas.") from exc


def remote_tool(tool: Tool, devices: Devices, client: AgentClient) -> Tool:
    """Même outil (paramètres, risque, phrases), exécuté par l'agent de l'appareil visé. Le résultat indique
    l'appareil (« device ») ; les phrases le précisent quand ce n'est pas l'appareil par défaut."""

    def allowed(key: str) -> str:
        if not devices[key].url:
            raise ToolError(DEVICE_FORBIDDEN, f"Je n'agis pas sur {devices[key].name}.")
        return key

    def run(device: str | None = None, **parameters) -> dict:
        target = devices[device or devices.default.key]
        result = client.call(target, tool.name, parameters)
        return {**result, "device": target.name} if isinstance(result, dict) else result

    def elsewhere(name: str | None) -> str:
        return f" sur {name}" if name and name != devices.default.name else ""

    def question(parameters: dict) -> str:
        own = {k: v for k, v in parameters.items() if k != "device"}
        target = devices[parameters.get("device") or devices.default.key]
        return tool.confirmation_question(own).rstrip(" ?") + f"{elsewhere(target.name)} ?"

    def say(result: dict) -> str:
        return tool.say(result).rstrip(".") + f"{elsewhere(result.get('device'))}."

    parameters = {**tool.parameters, "device": Param(str, "appareil visé", required=False, max_length=32,
                                                     choices=devices.keys(), check=allowed, hidden=True,
                                                     resolve=lambda text: (devices.find(text) or devices.default).key)}
    return replace(tool, parameters=parameters, run=run, question=question, say=say if tool.say else None)
