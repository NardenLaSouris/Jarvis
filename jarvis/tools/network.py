"""État du réseau d'ORION : worker LLM, agents des PC, ampoules, Internet (outil network_status, lecture seule).

Chaque vérification est une connexion TCP courte, toutes en parallèle : la réponse arrive en une ou deux secondes
même si une machine est éteinte. Rien n'est modifié.
"""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urlsplit

from jarvis.tools.base import Risk, Tool


def tcp_latency(host: str, port: int, timeout: float = 1.0) -> float | None:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return time.perf_counter() - started
    except OSError:
        return None


def url_target(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def network_tool(targets: list[tuple[str, str, int]], probe: Callable[[str, int], float | None] = tcp_latency) -> Tool:
    """``targets`` : (nom dit, hôte, port), par exemple (« le Katana », « 192.168.1.73 », 11434)."""

    def run() -> dict:
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            latencies = list(pool.map(lambda t: probe(t[1], t[2]), targets))
        items = [{"name": name, "online": latency is not None,
                  "latency_ms": None if latency is None else round(latency * 1000)}
                 for (name, _, _), latency in zip(targets, latencies, strict=True)]
        return {"items": items, "online": sum(i["online"] for i in items), "total": len(items)}

    def said(r: dict) -> str:
        down = [i["name"] for i in r["items"] if not i["online"]]
        if not down:
            return f"Tout répond : {', '.join(i['name'] for i in r['items'])}."
        up = r["online"]
        return (f"{up} élément{'s' if up > 1 else ''} sur {r['total']} répond{'ent' if up > 1 else ''}. "
                f"Sans réponse : {', '.join(down)}.")

    return Tool("network_status", "Vérifie le réseau : worker LLM, PC, ampoules et accès à Internet.", {},
                {"items": "liste de {name, online, latency_ms}"}, Risk.SAFE, run, say=said)
