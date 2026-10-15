"""Sources du fond d'écran, tirées des services déjà créés par ORION (rien de nouveau n'est interrogé, sauf une
connexion TCP courte vers chaque appareil et la liste des modèles chargés d'Ollama).

Chaque fonction rend des données prêtes pour la page (JSON) ; aucune n'agit, aucune ne vide une file.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from jarvis.wallpaper.collector import CoreMachine, Source

START = time.time()


def _clock(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def orion_source(cfg, llm, stt) -> Callable[[], dict]:
    def read() -> dict:
        llm_state = getattr(llm, "state", None)
        remote = getattr(stt, "url", None)
        down = getattr(stt, "_down_since", None)
        return {"uptime": round(time.time() - START), "wake_word": cfg.wake_word.phrase,
                "llm": {"state": llm_state or "local", "model": cfg.llm.model},
                "stt": {"model": cfg.stt.model, "remote": bool(remote), "remote_up": bool(remote) and down is None,
                        "last": getattr(stt, "last_source", "") or ""},
                "voice": Path(str(cfg.tts.voice)).stem}
    return read


def devices_source(devices, presence, reachable: Callable[[str], bool]) -> Callable[[], list]:
    def read() -> list:
        items = []
        for key in (devices.keys() if devices is not None else ()):
            device = devices[key]
            online = True if not device.url else reachable(device.url)  # sans adresse : le mini-PC, ORION elle-même
            items.append({"name": device.name, "online": bool(online), "core": not device.url})
        if presence is not None:
            users = presence.engine.snapshot()["users"]
            phone = next((u.get("phone") for u in users.values() if u.get("phone") is not None), None)
            if phone is not None:
                items.append({"name": "iPhone", "online": bool(phone), "core": False})
        return items
    return read


def presence_source(presence) -> Callable[[], dict]:
    def read() -> dict:
        snap = presence.engine.snapshot()
        doors = snap.get("doors") or {}
        return {"house": snap.get("house"),
                "users": {u: v.get("state") for u, v in (snap.get("users") or {}).items()},
                "doors_open": sum(1 for d in doors.values() if d.get("open"))}
    return read


def katana_source(llm_url: str, timeout: float = 2.0) -> Callable[[], dict]:
    """Modèles chargés sur le Katana (Ollama /api/ps) : nom et mémoire graphique occupée."""
    def read() -> dict:
        with urllib.request.urlopen(f"{llm_url.rstrip('/')}/api/ps", timeout=timeout) as response:
            data = json.loads(response.read())
        models = [{"name": m.get("name", ""), "vram": m.get("size_vram", 0)} for m in data.get("models", [])]
        return {"models": models}
    return read


def weather_source(weather) -> Callable[[], dict]:
    def read() -> dict:
        place, now = weather.current()
        days = []
        for day, label in (("today", "Aujourd'hui"), ("tomorrow", "Demain"), ("day_after_tomorrow", "Après-demain")):
            try:
                _, f, _ = weather.forecast(day, "day")
            except Exception:  # noqa: BLE001 - prévision manquante : le jour est simplement omis
                continue
            if f is not None:
                days.append({"label": label, "min": round(f.temperature_min), "max": round(f.temperature_max),
                             "rain": f.precipitation_probability, "slug": f.condition.slug,
                             "condition": f.condition.label})
        return {"place": place.name, "temperature": round(now.temperature),
                "feels": round(now.feels_like) if now.feels_like is not None else None,
                "humidity": now.humidity, "wind": now.wind_speed, "slug": now.condition.slug,
                "condition": now.condition.label, "days": days}
    return read


def agenda_source(calendar, days: int = 7) -> Callable[[], list]:
    def read() -> list:
        now = calendar.now()
        events = sorted(calendar.events(now, now + timedelta(days=days)), key=lambda e: e.start)[:5]
        return [{"title": e.title[:60], "day": e.start.strftime("%d/%m"), "time": "" if e.all_day else _clock(e.start),
                 "today": e.start.date() == now.date()} for e in events]
    return read


def schedule_source(timers) -> Callable[[], list]:
    def read() -> list:
        now = timers.now()
        items = []
        for t in timers.timers():
            items.append({"kind": "minuteur", "label": "Minuteur", "seconds": max(0, round((t.expires_at - now)
                                                                                       .total_seconds()))})
        for r in timers.reminders():
            items.append({"kind": "rappel", "label": (r.message or "Rappel")[:60],
                          "seconds": max(0, round((r.expires_at - now).total_seconds())), "at": _clock(r.expires_at)})
        return sorted(items, key=lambda i: i["seconds"])[:6]
    return read


def lights_source(rooms, driver) -> Callable[[], list]:
    def read() -> list:
        items = []
        for room in rooms.rooms():
            try:
                state = driver.state(room)
                items.append({"name": room.name, "on": bool(state.get("on")), "brightness": state.get("brightness"),
                              "online": True})
            except Exception:  # noqa: BLE001 - ampoule hors tension
                items.append({"name": room.name, "on": False, "brightness": None, "online": False})
        return items
    return read


def music_source(spotify) -> Callable[[], dict]:
    def read() -> dict:
        state = spotify.call("GET", "/me/player") or {}
        item = state.get("item") or {}
        artists = ", ".join(a.get("name", "") for a in (item.get("artists") or [])[:2] if isinstance(a, dict))
        progress, duration = state.get("progress_ms"), item.get("duration_ms")
        return {"playing": bool(state.get("is_playing")), "track": str(item.get("name") or "")[:60],
                "artist": artists[:60], "progress": progress, "duration": duration}
    return read


def mail_source(watcher) -> Callable[[], dict]:
    def read() -> dict:
        return {"important": len(getattr(watcher, "pending", []) or [])}  # compte seulement, la file reste intacte
    return read


def build_sources(cfg, *, llm=None, stt=None, devices=None, presence=None, weather=None, calendar=None, timers=None,
                  rooms=None, driver=None, spotify=None, watcher=None, reachable=None) -> list[Source]:
    from jarvis.llm.failover import tcp_reachable

    reachable = reachable or (lambda url: tcp_reachable(url, 0.6))
    machine = CoreMachine()
    sources = [Source("core", 1.0, machine.read), Source("orion", 2.0, orion_source(cfg, llm, stt))]
    optional: list[tuple[Any, str, float, Callable[[], Callable]]] = [
        (devices, "devices", 15.0, lambda: devices_source(devices, presence, reachable)),
        (presence, "presence", 5.0, lambda: presence_source(presence)),
        (weather, "weather", 900.0, lambda: weather_source(weather)),
        (calendar, "agenda", 60.0, lambda: agenda_source(calendar)),
        (timers, "schedule", 5.0, lambda: schedule_source(timers)),
        (rooms if driver is not None else None, "lights", 30.0, lambda: lights_source(rooms, driver)),
        (spotify, "music", 10.0, lambda: music_source(spotify)),
        (watcher, "mail", 30.0, lambda: mail_source(watcher)),
    ]
    for service, name, every, make in optional:
        if service is not None:
            sources.append(Source(name, every, make()))
    url = cfg.llm.host or ""
    if url.startswith("http"):
        sources.append(Source("katana", 20.0, katana_source(url)))
    return sources
