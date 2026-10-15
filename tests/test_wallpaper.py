"""Mode fond d'écran : collecte en arrière-plan, sources en lecture seule, serveur réservé aux adresses permises."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.wallpaper.collector import Collector, CoreMachine, Source  # noqa: E402
from jarvis.wallpaper.server import WallpaperServer  # noqa: E402
from jarvis.wallpaper.sources import devices_source, mail_source, schedule_source  # noqa: E402


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_each_source_is_read_at_its_own_pace_and_a_failure_keeps_the_last_value():
    now, calls = [0.0], {"fast": 0, "slow": 0}
    broken = [False]

    def fast():
        calls["fast"] += 1
        if broken[0]:
            raise OSError("capteur débranché")
        return calls["fast"]

    def slow():
        calls["slow"] += 1
        return "météo"

    collector = Collector([Source("fast", 1.0, fast), Source("slow", 900.0, slow)], clock=lambda: now[0])
    collector.tick()
    wait_for(lambda: collector.snapshot()["slow"]["ok"] and collector.snapshot()["fast"]["ok"])
    for t in (1.0, 2.0, 3.0):
        now[0] = t
        collector.tick()
        wait_for(lambda: calls["fast"] == int(t) + 1 and not collector._sources["fast"].running)
    assert calls["slow"] == 1  # la météo n'est pas relue chaque seconde
    broken[0] = True
    now[0] = 4.0
    collector.tick()
    wait_for(lambda: not collector.snapshot()["fast"]["ok"])
    assert collector.snapshot()["fast"] == {"value": 4, "ok": False}  # dernière valeur gardée, marquée périmée
    collector.stop()


def test_a_slow_source_is_never_read_twice_at_the_same_time():
    release, started = threading.Event(), []

    def blocking():
        started.append(1)
        release.wait(2)
        return "ok"

    now = [0.0]
    collector = Collector([Source("lights", 1.0, blocking)], clock=lambda: now[0])
    collector.tick()
    wait_for(lambda: started)
    now[0] = 10.0
    collector.tick()
    time.sleep(0.05)
    assert len(started) == 1
    release.set()
    collector.stop()


def test_core_machine_reads_proc_without_dependencies(tmp_path):
    proc, sysd = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    zone = sysd / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "temp").write_text("47000\n")
    (proc / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 12000000 kB\n")
    (proc / "uptime").write_text("3600.5 100.0\n")
    net = "Inter-|\n face |\n    lo: 999 0 0 0 0 0 0 0 999 0\n  eth0: {rx} 0 0 0 0 0 0 0 {tx} 0\n"
    (proc / "net" / "dev").write_text(net.format(rx=1000, tx=500))
    (proc / "stat").write_text("cpu 100 0 100 800 0 0 0 0\n")
    machine = CoreMachine(proc, sysd, str(tmp_path))
    first = machine.read()
    assert first["cpu"] is None and first["ram_total"] == 16000000 * 1024 and first["temperature"] == 47.0
    (proc / "stat").write_text("cpu 150 0 150 900 0 0 0 0\n")  # 100 occupés sur 200 : 50 %
    (proc / "net" / "dev").write_text(net.format(rx=5000, tx=900))
    second = machine.read()
    assert second["cpu"] == 50.0 and second["cpu_history"] == [50.0] and second["uptime"] == 3600
    assert second["rx"] > 0 and second["tx"] > 0  # « lo » ignoré
    json.dumps(second)


def test_sources_only_read():
    watcher = SimpleNamespace(pending=[{"sender": "a"}, {"sender": "b"}])
    assert mail_source(watcher)() == {"important": 2} and len(watcher.pending) == 2  # la file n'est pas vidée
    now = datetime(2026, 10, 15, 20, 0)
    timers = SimpleNamespace(now=lambda: now,
                             timers=lambda: [SimpleNamespace(expires_at=now + timedelta(minutes=5))],
                             reminders=lambda: [SimpleNamespace(message="Sortir le linge",
                                                                expires_at=now + timedelta(minutes=2))])
    assert schedule_source(timers)() == [
        {"kind": "rappel", "label": "Sortir le linge", "seconds": 120, "at": "20:02"},
        {"kind": "minuteur", "label": "Minuteur", "seconds": 300}]
    devices = {"pc": SimpleNamespace(name="PC fixe", url="http://pc:8765"),
               "mini": SimpleNamespace(name="mini-PC", url="")}
    fake = type("Devices", (), {"keys": lambda self: list(devices), "__getitem__": lambda self, k: devices[k]})()
    presence = SimpleNamespace(engine=SimpleNamespace(snapshot=lambda: {"users": {"monsieur": {"phone": True}}}))
    assert devices_source(fake, presence, lambda url: False)() == [
        {"name": "PC fixe", "online": False, "core": False}, {"name": "mini-PC", "online": True, "core": True},
        {"name": "iPhone", "online": True, "core": False}]


@pytest.fixture
def server():
    collector = Collector([Source("core", 60.0, lambda: {"cpu": 12.5})])
    wallpaper = WallpaperServer(collector, "127.0.0.1", 0, face_port=8765, allowed_ips=("192.168.1.128",))
    url = wallpaper.start()
    yield url, wallpaper
    wallpaper.stop()


def get(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def test_the_page_its_files_and_the_stats_are_served(server):
    url, _ = server
    for path, kind in (("", "text/html"), ("wallpaper.css", "text/css"), ("wallpaper.js", "text/javascript"),
                       ("michroma.woff2", "font/woff2")):
        status, content_type, body = get(url + path)
        assert status == 200 and content_type.startswith(kind) and body
    assert json.loads(get(url + "config")[2]) == {"face_port": 8765}
    wait_for(lambda: json.loads(get(url + "stats")[2])["core"]["ok"])
    assert json.loads(get(url + "stats")[2])["core"]["value"] == {"cpu": 12.5}
    with pytest.raises(urllib.error.HTTPError) as error:
        get(url + "../config.toml")
    assert error.value.code == 404


def test_read_only_and_reserved_to_allowed_addresses(server):
    url, wallpaper = server
    request = urllib.request.Request(url + "stats", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=3)
    assert error.value.code == 501  # aucune commande possible
    assert wallpaper.allowed("192.168.1.128") and wallpaper.allowed("::ffff:192.168.1.128")
    assert wallpaper.allowed("127.0.0.1") and not wallpaper.allowed("192.168.1.50")


def test_the_page_embeds_the_existing_face_untouched():
    page = (ROOT / "jarvis" / "wallpaper" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "jarvis" / "wallpaper" / "static" / "wallpaper.js").read_text(encoding="utf-8")
    assert '<iframe id="face"' in page and "facePort" in script  # le visage, sur son propre port, tel quel
