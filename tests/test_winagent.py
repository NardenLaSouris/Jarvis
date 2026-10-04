"""Agent Windows : configuration sûre, filtrage par IP, jeton partagé et actions enregistrées uniquement.

Le serveur écoute sur 127.0.0.1 (port libre choisi par le système) : aucun accès réseau réel.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.tools import Param, Risk, Tool, ToolRegistry  # noqa: E402
from jarvis.tools.base import ToolError  # noqa: E402
from jarvis.winagent import AgentConfig, AgentServer, load_agent_config  # noqa: E402
from jarvis.winagent.__main__ import main  # noqa: E402

TOKEN = "t" * 40


@pytest.fixture(autouse=True)
def no_token_in_environment(monkeypatch):
    monkeypatch.delenv("JARVIS_AGENT_TOKEN", raising=False)


def write(tmp_path, toml="", env=f"JARVIS_AGENT_TOKEN={TOKEN}\n"):
    config, secrets = tmp_path / "agent.toml", tmp_path / ".env"
    config.write_text(toml, encoding="utf-8")
    secrets.write_text(env, encoding="utf-8")
    return config, secrets


def agent(allowed=("127.0.0.1",), actions=None):
    server = AgentServer(AgentConfig("127.0.0.1", 0, frozenset(allowed), TOKEN), actions)
    server.start()
    return server


def request(server, path, method="GET", body=None, token=None):
    host, port = server.address
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def echo_actions(calls):
    registry = ToolRegistry()
    registry.register(Tool("echo", "Renvoie le texte.", {"text": Param(str, "texte", max_length=20)},
                           {"text": "texte"}, Risk.SAFE, lambda text: calls.append(text) or {"text": text}))
    registry.register(Tool("broken", "Échoue.", {}, {}, Risk.SAFE, lambda: 1 / 0))
    registry.register(Tool("refused", "Erreur prévue.", {}, {}, Risk.SAFE,
                           lambda: (_ for _ in ()).throw(ToolError("application_not_found", "Introuvable."))))
    return registry


# --- Configuration --------------------------------------------------------------------------------

def test_configuration_is_read_from_toml_and_env_file(tmp_path):
    config = load_agent_config(*write(tmp_path, '[agent]\nport = 9000\nallowed_ips = ["192.168.1.91"]\n'))
    assert config == AgentConfig("0.0.0.0", 9000, frozenset({"192.168.1.91"}), TOKEN)


def test_environment_token_wins_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_AGENT_TOKEN", "e" * 40)
    assert load_agent_config(*write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n')).token == "e" * 40


@pytest.mark.parametrize("toml, env, message", [
    ('[agent]\nallowed_ips = ["192.168.1.91"]\n', "", "JARVIS_AGENT_TOKEN"),
    ('[agent]\nallowed_ips = ["192.168.1.91"]\n', "JARVIS_AGENT_TOKEN=court\n", "trop court"),
    ("[agent]\n", f"JARVIS_AGENT_TOKEN={TOKEN}\n", "allowed_ips"),
    ('[agent]\nallowed_ips = ["192.168.1.300"]\n', f"JARVIS_AGENT_TOKEN={TOKEN}\n", "IP invalide"),
    ('[agent]\nallowed_ips = ["192.168.1.91"]\nport = "8765"\n', f"JARVIS_AGENT_TOKEN={TOKEN}\n", "Port invalide"),
    ('[agent]\nallowed_ips = ["192.168.1.91"]\nshell = true\n', f"JARVIS_AGENT_TOKEN={TOKEN}\n", "inconnus"),
])
def test_unsafe_configuration_is_refused(tmp_path, toml, env, message):
    with pytest.raises(ValueError, match=message):
        load_agent_config(*write(tmp_path, toml, env))


def test_shipped_configuration_only_allows_the_core(tmp_path):
    _, secrets = write(tmp_path)
    config = load_agent_config(ROOT / "windows_agent.toml", secrets)
    assert (config.host, config.port, config.allowed_ips) == ("0.0.0.0", 8765, frozenset({"192.168.1.91"}))


def test_main_refuses_to_start_without_token(tmp_path, capsys):
    config, secrets = write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n', "")
    assert main(["--config", str(config), "--env", str(secrets)]) == 2
    assert "JARVIS_AGENT_TOKEN" in capsys.readouterr().err


# --- /health et filtrage par IP --------------------------------------------------------------------

def test_health_answers_the_allowed_core():
    server = agent()
    try:
        assert request(server, "/health") == (200, {"status": "ok", "agent": "jarvis-windows"})
    finally:
        server.stop()


def test_other_ips_are_refused_everywhere():
    server = agent(allowed=("192.168.1.91",), actions=echo_actions([]))
    try:
        assert request(server, "/health") == (403, {"status": "error", "error": "forbidden"})
        status, _ = request(server, "/actions/echo", "POST", {"parameters": {"text": "x"}}, token=TOKEN)
        assert status == 403
    finally:
        server.stop()


def test_unknown_paths_are_not_found():
    server = agent()
    try:
        assert request(server, "/")[0] == 404
        assert request(server, "/health/x")[0] == 404
        assert request(server, "/run", "POST", {}, token=TOKEN)[0] == 404
    finally:
        server.stop()


def test_port_already_used_is_reported():
    first = agent()
    try:
        host, port = first.address
        with pytest.raises(OSError):
            AgentServer(AgentConfig(host, port, frozenset({"127.0.0.1"}), TOKEN)).start()
    finally:
        first.stop()


# --- Actions : jeton et registre --------------------------------------------------------------------

@pytest.mark.parametrize("token", [None, "", "mauvais", TOKEN[:-1], TOKEN + "x"])
def test_actions_require_the_shared_token(token):
    calls = []
    server = agent(actions=echo_actions(calls))
    try:
        status, payload = request(server, "/actions/echo", "POST", {"parameters": {"text": "x"}}, token=token)
        assert (status, payload["error"]) == (401, "unauthorized") and calls == []
    finally:
        server.stop()


def test_registered_action_runs_with_validated_parameters():
    calls = []
    server = agent(actions=echo_actions(calls))
    try:
        status, payload = request(server, "/actions/echo", "POST", {"parameters": {"text": " bonjour "}}, token=TOKEN)
        assert (status, payload) == (200, {"status": "ok", "action": "echo", "result": {"text": "bonjour"}})
        assert calls == ["bonjour"]
    finally:
        server.stop()


def test_no_action_is_exposed_by_default():
    server = agent()
    try:
        for name in ("echo", "shell", "cmd", "powershell", "../health"):
            status, payload = request(server, f"/actions/{name}", "POST", {}, token=TOKEN)
            assert status in (400, 404) and payload["status"] == "error", name
    finally:
        server.stop()


@pytest.mark.parametrize("body, error", [
    ({"parameters": {"text": "x" * 21}}, "invalid_parameters"),
    ({"parameters": {"text": "x", "command": "calc"}}, "invalid_parameters"),
    ({"parameters": {}}, "invalid_parameters"),
    ({"parameters": {"text": "x"}, "confirmed": True}, "invalid_parameters"),
    ([], "invalid_parameters"),
])
def test_invalid_requests_are_refused(body, error):
    calls = []
    server = agent(actions=echo_actions(calls))
    try:
        status, payload = request(server, "/actions/echo", "POST", body, token=TOKEN)
        assert (status, payload["error"]) == (400, error) and calls == []
    finally:
        server.stop()


def test_malformed_json_and_oversized_bodies_are_refused():
    server = agent(actions=echo_actions([]))
    host, port = server.address
    try:
        for data, expected in ((b"{pas du json", 400), (b"x" * 5000, 413)):
            req = urllib.request.Request(f"http://{host}:{port}/actions/echo", data=data, method="POST",
                                         headers={"Authorization": f"Bearer {TOKEN}"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == expected
    finally:
        server.stop()


def test_action_errors_are_structured_without_internal_details():
    server = agent(actions=echo_actions([]))
    try:
        status, payload = request(server, "/actions/broken", "POST", {}, token=TOKEN)
        assert status == 500 and payload["error"] == "execution_failed" and "ZeroDivision" not in json.dumps(payload)
        status, payload = request(server, "/actions/refused", "POST", {}, token=TOKEN)
        assert (status, payload["error"], payload["message"]) == (422, "application_not_found", "Introuvable.")
    finally:
        server.stop()


def test_env_file_saved_with_a_bom_is_read(tmp_path):
    config, secrets = write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n')
    secrets.write_text(f"JARVIS_AGENT_TOKEN={TOKEN}\n", encoding="utf-8-sig")
    assert load_agent_config(config, secrets).token == TOKEN


# --- Visage du Core ouvert sur le PC ---------------------------------------------------------------

def test_core_face_opens_on_connection_but_not_on_quick_reconnections(tmp_path):
    opened, clock = [], {"t": 0.0}
    config = AgentConfig("127.0.0.1", 0, frozenset({"192.168.1.91"}), TOKEN, face_port=8765)
    server = AgentServer(config, open_page=opened.append, clock=lambda: clock["t"])
    server.core_connected("192.168.1.91")
    server.core_left()
    clock["t"] = 60
    server.core_connected("192.168.1.91")
    server.core_left()
    clock["t"] = 60 + 301
    server.core_connected("192.168.1.91")
    assert opened == ["http://192.168.1.91:8765/", "http://192.168.1.91:8765/"]
    AgentServer(replace(config, face_port=None), open_page=opened.append).core_connected("192.168.1.91")
    assert len(opened) == 2


def test_face_settings_are_read(tmp_path):
    toml = '[agent]\nallowed_ips = ["192.168.1.91"]\n[face]\nopen_on_connect = true\nport = 9000\n'
    assert load_agent_config(*write(tmp_path, toml)).face_port == 9000
    assert load_agent_config(*write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n')).face_port is None
    with pytest.raises(ValueError, match="inconnus"):
        load_agent_config(*write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n[face]\nurl = "http://x"\n'))


def test_errors_go_to_the_log_file_when_started_without_window(tmp_path):
    import logging

    config, secrets = write(tmp_path, '[agent]\nallowed_ips = ["192.168.1.91"]\n', "")
    log = tmp_path / "logs" / "winagent.log"
    root = logging.getLogger()
    handlers = root.handlers[:]
    root.handlers = []
    try:
        assert main(["--config", str(config), "--env", str(secrets), "--log-file", str(log)]) == 2
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = handlers
    assert "JARVIS_AGENT_TOKEN" in log.read_text(encoding="utf-8")


def test_refused_requests_with_a_body_get_their_error_not_a_reset():
    server = agent()
    try:
        big = {"parameters": {"x": "y" * 50_000}}
        for _ in range(20):
            assert request(server, "/run", "POST", big, token=TOKEN)[0] == 404
            assert request(server, "/actions/system_info", "POST", big, token="x" * 40)[0] == 401
    finally:
        server.stop()
