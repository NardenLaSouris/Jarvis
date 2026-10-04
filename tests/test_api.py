"""API d'administration du Core : sécurité (IP, jeton), état, appareils, outils sûrs, routines, historique.

Vraie API sur 127.0.0.1 ; ampoules, PC et LLM simulés (aucune action réelle).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jarvis.activity import ActivityLog, JsonlActivityStore  # noqa: E402
from jarvis.api import CoreApi, CoreStatus  # noqa: E402
from jarvis.events import EventBus  # noqa: E402
from jarvis.routines import MemoryRoutineStore, RoutineEngine  # noqa: E402
from jarvis.tools import PermissionManager, ToolCore, ToolRegistry, builtin_tools  # noqa: E402
from jarvis.tools.devices import load_devices  # noqa: E402
from jarvis.tools.lights import light_tools  # noqa: E402
from test_lights import FakeDriver, rooms_from  # noqa: E402
from test_routines import SOIR  # noqa: E402
from test_tools import FakeProcesses, FakeVolume, Launcher, Locker  # noqa: E402

TOKEN = "t" * 40


class World:
    def __init__(self, tmp_path, allowed=("127.0.0.1",), katana_up=True):
        self.driver = FakeDriver(unreachable={"entree"})
        rooms = rooms_from()
        registry = ToolRegistry()
        for tool in [*light_tools(rooms, self.driver),
                     *builtin_tools(processes=FakeProcesses(), launcher=Launcher(), locker=Locker(), volume=FakeVolume())]:
            registry.register(tool)
        self.events = EventBus()
        store = JsonlActivityStore(tmp_path / "activity.jsonl")
        ActivityLog(store).attach(self.events)
        self.core = ToolCore(registry, PermissionManager(), events=self.events)
        self.said = []
        self.routines = RoutineEngine(MemoryRoutineStore(), registry, self.core.submit,
                                      lambda title, text: self.said.append(text), self.events, sleep=lambda s: None)
        devices = load_devices({"pc": {"name": "votre PC", "url": "http://pc:8765", "default": True},
                                "mini": {"name": "le mini-PC"}})

        def fetch(url):
            if url.startswith("http://katana") and not katana_up:
                raise OSError("injoignable")
            if url.endswith("/api/ps"):
                return {"models": [{"name": "qwen2.5:7b"}]}
            return {"status": "ok"}

        status = CoreStatus(llm_url="http://katana:11434", llm_model="qwen2.5:7b", devices=devices, rooms=rooms,
                            driver=self.driver, routines=self.routines, activity=store, fetch=fetch)
        self.api = CoreApi("127.0.0.1", 0, frozenset(allowed), TOKEN, tools=self.core, routines=self.routines,
                           status=status, activity=store)
        self.api.start()
        host, port = self.api.address
        self.base = f"http://{host}:{port}/api"

    def call(self, method, path, body=None, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else (b"" if method in ("POST", "PUT") else None)
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.api.stop()


def test_security(tmp_path, world):
    assert world.call("GET", "/health", token=None) == (200, {"status": "ok", "agent": "jarvis-core"})
    assert world.call("GET", "/status", token=None)[0] == 401
    assert world.call("GET", "/status", token="faux")[0] == 401
    other = World(tmp_path, allowed=("192.168.1.128",))
    try:
        assert other.call("GET", "/health")[0] == 403 and other.call("GET", "/status")[0] == 403
    finally:
        other.api.stop()
    with pytest.raises(ValueError, match="JARVIS_AGENT_TOKEN"):
        CoreApi("127.0.0.1", 0, frozenset(), "", tools=None, routines=None, status=None)


def test_status_and_devices(world):
    status, data = world.call("GET", "/status")
    assert status == 200 and data["core"]["online"] and data["llm"]["loaded"] == ["qwen2.5:7b"]
    assert data["lights"]["online"] == 1 and data["lights"]["total"] == 2
    assert [a["id"] for a in data["agents"]] == ["pc"] and data["agents"][0]["online"] is True
    assert data["routines"] == {"total": 0, "enabled": 0, "next": []}
    devices = {d["id"]: d for d in world.call("GET", "/devices")[1]}
    assert devices["mini"]["kind"] == "core" and devices["mini"]["features"] == []
    assert devices["chambre"]["state"] == {"on": False, "brightness": 100, "mode": "white"}
    assert devices["entree"]["online"] is False


def test_status_survives_an_offline_llm(tmp_path):
    w = World(tmp_path, katana_up=False)
    try:
        status, data = w.call("GET", "/status")
        assert status == 200 and data["llm"]["online"] is False and data["llm"]["error"] == "OSError"
    finally:
        w.api.stop()


def test_only_safe_tools_run(world):
    tools = {t["name"]: t for t in world.call("GET", "/tools")[1]}
    assert tools["light_on"]["safe"] and not tools["lock_pc"]["safe"] and tools["light_on"]["parameters"]["room"]["hidden"]
    status, data = world.call("POST", "/tools/set_brightness", {"parameters": {"room": "chambre", "brightness": 40}})
    assert status == 200 and data["message"] == "La lumière de la chambre est à 40 %."
    assert world.driver.lights["chambre"]["brightness"] == 40
    assert world.call("POST", "/tools/lock_pc", {"parameters": {}})[1]["error"] == "confirmation_required"
    assert world.call("POST", "/tools/inconnu", {"parameters": {}})[0] == 404
    status, data = world.call("POST", "/tools/light_on", {"parameters": {"room": "entree"}})
    assert status == 422 and "ne répond pas" in data["message"]
    assert world.call("POST", "/tools/set_brightness", {"parameters": {"room": "chambre", "brightness": 400}})[0] == 422


def test_routine_lifecycle_and_history(world):
    status, created = world.call("POST", "/routines", {**SOIR, "actions": SOIR["actions"][:1] + SOIR["actions"][2:]})
    assert status == 201 and created["name"] == "Soir" and created["next_run"]
    rid = created["id"]
    assert world.call("GET", "/routines")[1][0]["id"] == rid
    assert world.call("POST", "/routines/" + rid + "/disable")[1]["enabled"] is False
    assert world.call("POST", "/routines/" + rid + "/enable")[1]["enabled"] is True
    status, copy = world.call("POST", "/routines/" + rid + "/duplicate")
    assert status == 201 and copy["name"] == "Soir (copie)"
    status, updated = world.call("PUT", "/routines/" + copy["id"], {**SOIR, "name": "Matin"})
    assert status == 200 and updated["name"] == "Matin"
    assert world.call("DELETE", "/routines/" + copy["id"])[0] == 200
    assert world.call("GET", "/routines/" + copy["id"])[0] == 404
    status, error = world.call("POST", "/routines", {**SOIR, "actions": [{"type": "tool", "tool": "lock_pc"}]})
    assert status == 400 and "confirmation" in error["message"]
    assert world.call("POST", "/routines", ["pas", "un", "objet"])[0] == 400

    assert world.call("POST", "/routines/" + rid + "/run")[0] == 202
    deadline = time.monotonic() + 5
    while world.routines.get(rid)["running"] or world.routines.get(rid)["last_status"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert world.said == ["Bonne soirée, monsieur."] and world.driver.lights["chambre"]["brightness"] == 30
    history = world.call("GET", "/history?kind=routine&limit=10")[1]
    assert [h["type"] for h in history][0] == "routine.finished" and history[0]["success"] is True
    assert history[-1]["type"] == "routine.started" and history[0]["routine"] == "Soir"
    assert all(h["type"].startswith("routine.") for h in history)
    assert any(h["type"].startswith("tool.") for h in world.call("GET", "/history?kind=all")[1])
    status, data = world.call("GET", "/status")
    assert data["routines"]["total"] == 1 and data["last_activity"]["type"] == "routine.finished"


def test_unknown_routes(world):
    assert world.call("GET", "/nope")[0] == 404
    assert world.call("POST", "/routines/abc/explode")[0] == 404


def test_many_simultaneous_clients_are_all_served():
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    from jarvis.api import CoreApi, CoreStatus

    api = CoreApi("127.0.0.1", 0, frozenset({"127.0.0.1"}), "t" * 40, tools=None, routines=None, status=CoreStatus())
    api.start()
    host, port = api.address

    def health(_):
        request = urllib.request.Request(f"http://{host}:{port}/api/health", headers={"Authorization": "Bearer " + "t" * 40})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status

    try:
        with ThreadPoolExecutor(40) as pool:
            assert set(pool.map(health, range(400))) == {200}
    finally:
        api.stop()
