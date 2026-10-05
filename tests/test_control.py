"""JARVIS Control : client du Core, pont local (clé de session, hôte, jeton jamais exposé), paramètres."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.control.bridge import Bridge  # noqa: E402
from jarvis.control.client import CoreClient, CoreError  # noqa: E402
from jarvis.control.settings import Settings, SettingsStore  # noqa: E402
from test_api import TOKEN, World  # noqa: E402
from test_routines import SOIR  # noqa: E402


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.api.stop()


@pytest.fixture
def bridge(tmp_path, world):
    store = SettingsStore(tmp_path / "control", env_file=tmp_path / "absent.env")
    store.save(Settings(core_url=world.base.removesuffix("/api")))
    store.set_token(TOKEN)
    autostart = []
    b = Bridge(store, autostart.append)
    b.autostart_calls = autostart
    b.start()
    yield b
    b.stop()


def call(bridge, method, path, body=None, key=None, host=None):
    port = bridge._httpd.server_address[1]
    headers = {"X-Control-Key": bridge.key if key is None else key, "Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else (b"" if method in ("POST", "PUT") else None)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/bridge{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --- Client ---------------------------------------------------------------------------------------

def test_client_talks_to_the_core(world):
    client = CoreClient(world.base.removesuffix("/api"), TOKEN)
    assert client.health()["agent"] == "jarvis-core"
    created = client.create_routine(SOIR)
    assert client.get_routines()[0]["id"] == created["id"]
    assert client.set_routine_enabled(created["id"], False)["enabled"] is False
    assert client.call_tool("light_on", {"room": "chambre"})["message"] == "La lumière de la chambre est allumée."
    with pytest.raises(CoreError, match="confirmation vocale"):
        client.call_tool("lock_pc", {})
    with pytest.raises(CoreError, match="confirmation"):
        client.create_routine({**SOIR, "actions": [{"type": "tool", "tool": "lock_pc"}]})


def test_client_explains_errors(world):
    with pytest.raises(CoreError, match="Jeton refusé"):
        CoreClient(world.base.removesuffix("/api"), "x" * 40).get_status()
    with pytest.raises(CoreError, match="injoignable"):
        CoreClient("http://127.0.0.1:9", TOKEN, timeout=1).get_status()


# --- Pont local -----------------------------------------------------------------------------------

def test_bridge_serves_the_interface_without_key_but_never_the_api(bridge):
    port = bridge._httpd.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        page = response.read().decode()
        assert "JARVIS Control" in page and "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert call(bridge, "GET", "/status", key="")[0] == 403
    assert call(bridge, "GET", "/status", key="mauvaise")[0] == 403
    assert call(bridge, "GET", "/status", host="evil.example:80")[0] == 403
    assert bridge.url.startswith(f"http://127.0.0.1:{port}/#key=")


def test_bridge_relays_to_the_core(bridge, world):
    status, data = call(bridge, "GET", "/status")
    assert status == 200 and data["ok"] and data["data"]["core"]["online"]
    created = call(bridge, "POST", "/routines", SOIR)[1]["data"]
    rid = created["id"]
    assert call(bridge, "POST", f"/routines/{rid}/disable")[1]["data"]["enabled"] is False
    assert call(bridge, "PUT", f"/routines/{rid}", {**SOIR, "name": "Nuit"})[1]["data"]["name"] == "Nuit"
    assert call(bridge, "POST", f"/routines/{rid}/duplicate")[1]["ok"]
    assert len(call(bridge, "GET", "/routines")[1]["data"]) == 2
    assert call(bridge, "DELETE", f"/routines/{rid}")[1]["ok"]
    result = call(bridge, "POST", "/tools/set_brightness", {"parameters": {"room": "chambre", "brightness": 25}})[1]
    assert result["ok"] and world.driver.lights["chambre"]["brightness"] == 25
    refused = call(bridge, "POST", "/routines", {**SOIR, "name": ""})[1]
    assert refused == {"ok": False, "error": "Nom : obligatoire, 60 caractères au plus."}
    assert isinstance(call(bridge, "GET", "/history?kind=tool&limit=5")[1]["data"], list)
    assert call(bridge, "GET", "/inconnu")[0] == 404


def test_settings_never_expose_the_token(bridge, tmp_path):
    view = call(bridge, "GET", "/settings")[1]["data"]
    assert view["token_configured"] is True and TOKEN not in json.dumps(view)
    saved = call(bridge, "POST", "/settings", {"theme": "dark", "refresh_seconds": 30, "autostart": True,
                                               "token": "n" * 40})[1]["data"]
    assert saved["theme"] == "dark" and saved["refresh_seconds"] == 30 and "n" * 40 not in json.dumps(saved)
    assert bridge.autostart_calls == [True] and bridge.store.token() == "n" * 40
    assert call(bridge, "POST", "/settings", {"core_url": "pas une adresse"})[1]["ok"] is False
    assert call(bridge, "POST", "/settings", {"token": "court"})[1]["ok"] is False
    assert TOKEN not in (tmp_path / "control" / "settings.json").read_text(encoding="utf-8")


# --- Paramètres -----------------------------------------------------------------------------------

def test_settings_store_defaults_and_token_fallback(tmp_path):
    env = tmp_path / ".env"
    env.write_text("JARVIS_AGENT_TOKEN=" + "e" * 40 + "\n", encoding="utf-8")
    store = SettingsStore(tmp_path / "app", env_file=env)
    assert store.load() == Settings() and store.token() == "e" * 40
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "settings.json").write_text("{cassé", encoding="utf-8")
    assert store.load() == Settings()
    with pytest.raises(ValueError):
        Settings(refresh_seconds=1).validated()


def test_settings_written_with_a_bom_are_still_read(tmp_path):
    # PowerShell 5.1 (Set-Content -Encoding utf8) ajoute un BOM : les réglages ne doivent pas être perdus.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "settings.json").write_text('{"theme": "nuit", "pc_name": "PC fixe"}', encoding="utf-8-sig")
    loaded = SettingsStore(tmp_path / "app", env_file=tmp_path / "absent.env").load()
    assert loaded.theme == "nuit" and loaded.pc_name == "PC fixe"


@pytest.mark.skipif(sys.platform != "win32", reason="application Windows")
def test_autostart_shortcut_is_created_with_fixed_arguments(monkeypatch, tmp_path):
    import jarvis.control.app as app

    calls = []
    monkeypatch.setattr(app, "STARTUP_LINK", tmp_path / "JARVIS Control.lnk")
    monkeypatch.setattr(app.subprocess, "run", lambda argv, **kw: calls.append((argv, kw["env"])))
    app.set_autostart(True)
    argv, env = calls[0]
    assert argv[:2] == ["powershell", "-NoProfile"] and "--hidden" in argv[-1] and "$env:JC_LINK" in argv[-1]
    assert env["JC_LINK"] == str(tmp_path / "JARVIS Control.lnk") and env["JC_TARGET"].endswith("pythonw.exe")
    (tmp_path / "JARVIS Control.lnk").write_text("x")
    app.set_autostart(False)
    assert not (tmp_path / "JARVIS Control.lnk").exists()


def test_face_colour_is_read_through_the_bridge(bridge):
    # Le Core de test n'a pas de visage : la page l'indique, sans erreur.
    status, data = call(bridge, "GET", "/face")
    assert status in (200, 404, 502) and isinstance(data, dict)
