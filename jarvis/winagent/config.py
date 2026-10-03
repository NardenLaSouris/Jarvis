"""Configuration de l'agent Windows : réglages dans un fichier TOML, jeton partagé hors du dépôt (.env)."""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import secret

TOKEN_NAME = "JARVIS_AGENT_TOKEN"
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class AgentConfig:
    host: str
    port: int
    allowed_ips: frozenset[str]
    token: str
    input_device: str = ""
    output_device: str = ""


def load_agent_config(path: str | Path, env_file: str | Path) -> AgentConfig:
    """Lit ``path`` ([agent], [audio]) et le jeton ``JARVIS_AGENT_TOKEN`` (environnement, sinon ``env_file``).

    Lève ValueError si la configuration n'est pas sûre : aucune IP autorisée, IP invalide, jeton absent ou court.
    """
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    raw, audio = data.get("agent", {}), data.get("audio", {})
    unknown = (set(data) - {"agent", "audio"}) | (set(raw) - {"host", "port", "allowed_ips"}) | (
        set(audio) - {"input_device", "output_device"})
    if unknown:
        raise ValueError(f"Réglages inconnus dans {path.name} : {sorted(unknown)}")
    port = raw.get("port", 8765)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError(f"Port invalide : {port!r}")
    ips = raw.get("allowed_ips", [])
    if not isinstance(ips, list) or not ips:
        raise ValueError("allowed_ips doit lister au moins une adresse IP (celle du Core JARVIS).")
    try:
        allowed = frozenset(str(ipaddress.ip_address(ip)) for ip in ips)
    except ValueError as exc:
        raise ValueError(f"Adresse IP invalide dans allowed_ips : {exc}") from exc
    token = secret(TOKEN_NAME, Path(env_file))
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"{TOKEN_NAME} absent ou trop court ({MIN_TOKEN_LENGTH} caractères minimum) : "
                         f"définissez-le dans l'environnement ou dans {Path(env_file).name}.")
    return AgentConfig(str(raw.get("host", "0.0.0.0")), port, allowed, token,
                       str(audio.get("input_device", "")), str(audio.get("output_device", "")))
