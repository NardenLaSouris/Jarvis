"""État de JARVIS pour l'API d'administration : services, appareils, lumières, échéances, routines.

Chaque vérification réseau a un délai court et toutes partent en parallèle : l'état répond en quelques
secondes au pire, même si une machine est éteinte.
"""

from __future__ import annotations

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from jarvis.scheduling.durations import spoken_remaining

TIMEOUT = 2.0


def http_json(url: str, timeout: float = TIMEOUT) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _safely(check: Callable[[], dict]) -> dict:
    try:
        return check()
    except Exception as exc:
        return {"online": False, "error": type(exc).__name__}


class CoreStatus:
    def __init__(self, *, llm_url: str = "", llm_model: str = "", fallback_model: str = "", web=None, devices=None,
                 rooms=None, driver=None, timers=None, routines=None, activity=None, worker=None, home=None,
                 fetch: Callable[[str], dict] = http_json, clock: Callable[[], datetime] = datetime.now):
        self._llm_url, self._llm_model, self._fallback_model = llm_url.rstrip("/"), llm_model, fallback_model
        self._web, self._devices, self._rooms, self._driver = web, devices, rooms, driver
        self._timers, self._routines, self._activity = timers, routines, activity
        self._worker = worker
        self._home = home
        self._fetch, self._clock = fetch, clock
        self._started = time.time()

    # --- Vérifications -------------------------------------------------------------------------------

    def _llm(self) -> dict:
        worker = self._worker.status() if self._worker is not None else {}
        if not self._llm_url:
            return {"online": False, "model": self._llm_model, **({"worker": worker} if worker else {})}
        try:
            loaded = [m.get("name") for m in self._fetch(f"{self._llm_url}/api/ps").get("models", [])]
        except Exception as exc:
            if not worker:
                raise
            return {"online": False, "model": self._llm_model, "url": self._llm_url, "error": type(exc).__name__,
                    "fallback_model": self._fallback_model or None, "state": worker.get("state"), "worker": worker}
        return {"online": True, "model": self._llm_model, "loaded": loaded, "url": self._llm_url,
                "fallback_model": self._fallback_model or None,
                **({"state": worker.get("state"), "worker": worker} if worker else {})}

    def _search(self) -> dict:
        if self._web is None:
            return {"online": False, "enabled": False}
        return {"online": bool(getattr(self._web.provider, "available", lambda: True)()), "enabled": True}

    def _agent(self, device) -> dict:
        data = self._fetch(f"{device.url}/health")
        return {"online": data.get("status") == "ok"}

    def _light(self, room) -> dict:
        return {"online": True, **self._driver.state(room)}

    def _checks(self) -> dict[str, Callable[[], dict]]:
        checks = {"llm": self._llm, "search": self._search}
        if self._devices is not None:
            for key in self._devices.keys():
                device = self._devices[key]
                if device.url:
                    checks[f"device:{key}"] = lambda device=device: self._agent(device)
        if self._rooms is not None and self._driver is not None:
            for key in self._rooms.keys():
                if key != "all":
                    checks[f"light:{key}"] = lambda room=self._rooms[key]: self._light(room)
        return checks

    def _run_checks(self) -> dict[str, dict]:
        checks = self._checks()
        with ThreadPoolExecutor(max_workers=max(1, len(checks))) as pool:
            futures = {name: pool.submit(_safely, check) for name, check in checks.items()}
            return {name: future.result() for name, future in futures.items()}

    # --- Vues ----------------------------------------------------------------------------------------

    def devices(self, results: dict[str, dict] | None = None) -> list[dict]:
        results = results if results is not None else self._run_checks()
        items = []
        if self._devices is not None:
            for key in self._devices.keys():
                device = self._devices[key]
                state = results.get(f"device:{key}", {"online": None})
                items.append({"id": key, "kind": "pc" if device.url else "core", "name": device.name,
                              "address": device.url or None, "default": device.default, "aliases": list(device.aliases),
                              "online": state.get("online"),
                              "features": ["applications", "pages Web", "volume", "verrouillage", "description"]
                              if device.url else []})
        if self._rooms is not None:
            for key in self._rooms.keys():
                if key == "all":
                    continue
                room = self._rooms[key]
                state = results.get(f"light:{key}", {"online": None})
                items.append({"id": key, "kind": "light", "name": room.name, "address": room.ip,
                              "aliases": list(room.aliases), "online": state.get("online"),
                              "state": {k: state[k] for k in ("on", "brightness", "mode") if k in state},
                              "features": ["allumer", "éteindre", "luminosité", "couleur", "température"]})
        return items

    def _schedule(self) -> dict:
        if self._timers is None:
            return {"timers": [], "reminders": []}
        now = self._timers.now()
        return {"timers": [{"id": t.id, "remaining": spoken_remaining(t.remaining(now)), "ends_at": f"{t.expires_at:%H:%M}"}
                           for t in self._timers.timers()],
                "reminders": [{"id": r.id, "message": r.message, "remaining": spoken_remaining(r.remaining(now)),
                               "at": f"{r.expires_at:%H:%M}"} for r in self._timers.reminders()]}

    def _routine_summary(self) -> dict:
        routines = self._routines.list() if self._routines is not None else []
        upcoming = sorted((r for r in routines if r["next_run"]), key=lambda r: r["next_run"])[:5]
        return {"total": len(routines), "enabled": sum(r["enabled"] for r in routines),
                "next": [{"id": r["id"], "name": r["name"], "next_run": r["next_run"]} for r in upcoming]}

    def _last_activity(self) -> dict | None:
        if self._activity is None:
            return None
        records = self._activity.read(1)
        return history_item(records[-1]) if records else None

    def snapshot(self) -> dict:
        results = self._run_checks()
        devices = self.devices(results)
        lights = [d for d in devices if d["kind"] == "light"]
        agents = [d for d in devices if d["kind"] == "pc"]
        return {
            "core": {"online": True, "uptime_s": round(time.time() - self._started), "time": self._clock().isoformat(
                timespec="seconds")},
            "llm": results.get("llm", {"online": False}),
            **({"home": self._home.snapshot()} if self._home is not None else {}),
            "search": results.get("search", {"online": False}),
            "agents": [{"id": d["id"], "name": d["name"], "online": d["online"]} for d in agents],
            "lights": {"online": sum(bool(d["online"]) for d in lights), "total": len(lights),
                       "items": [{"id": d["id"], "name": d["name"], "online": d["online"], **d.get("state", {})}
                                 for d in lights]},
            "schedule": self._schedule(),
            "routines": self._routine_summary(),
            "last_activity": self._last_activity(),
        }


def history_item(record: dict) -> dict:
    payload = record.get("payload") or {}
    try:
        when = datetime.fromtimestamp(float(record.get("timestamp", 0))).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        when = None
    success = payload.get("success")
    if record.get("type", "").endswith(".failed"):
        success = False
    return {"time": when, "type": record.get("type"), "source": record.get("source"),
            "subject": payload.get("subject") or payload.get("tool") or record.get("type"),
            "routine": payload.get("name") if record.get("source") == "routines" else None,
            "routine_id": payload.get("routine_id"), "action": payload.get("action") or payload.get("tool"),
            "success": success, "message": payload.get("message") or payload.get("error")}
