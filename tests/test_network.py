"""Outil network_status et réponse « quelles sont tes sources ? » (sondes simulées, aucun accès réseau)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import SOURCES  # noqa: E402
from jarvis.personality import normalize  # noqa: E402
from jarvis.tools.network import network_tool, tcp_latency, url_target  # noqa: E402

TARGETS = [("le worker LLM", "192.168.1.73", 11434), ("votre PC", "192.168.1.128", 8765), ("Internet", "1.1.1.1", 443)]


def test_everything_answers():
    tool = network_tool(TARGETS, probe=lambda host, port: 0.008)
    result = tool.execute({})
    assert result["online"] == 3 and result["items"][0]["latency_ms"] == 8
    assert tool.say(result) == "Tout répond : le worker LLM, votre PC, Internet."


def test_down_machines_are_named():
    tool = network_tool(TARGETS, probe=lambda host, port: None if host == "192.168.1.73" else 0.01)
    result = tool.execute({})
    assert tool.say(result) == "2 éléments sur 3 répondent. Sans réponse : le worker LLM."


def test_helpers():
    assert url_target("http://192.168.1.73:11434") == ("192.168.1.73", 11434)
    assert url_target("https://exemple.fr") == ("exemple.fr", 443)
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert tcp_latency("127.0.0.1", port, 0.5) is None


def test_sources_questions_are_recognised():
    assert SOURCES.search(normalize("Quelles sont tes sources ?"))
    assert SOURCES.search(normalize("D'où vient cette info ?"))
    assert not SOURCES.search(normalize("Parle-moi des sources du Nil"))
