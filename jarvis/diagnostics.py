"""Diagnostics non destructifs : ``python -m jarvis --health``, ``--diagnostics`` et ``--benchmark``.

- health : chaque composant vérifié en quelques secondes (en parallèle) : fichiers des modèles, worker LLM et LLM
  de secours, recherche Web, agents des PC, ampoules (lecture d'état seulement), mémoire, échéances, routines,
  notifications, disque, mémoire vive. Code de sortie 1 si un composant indispensable est en échec.
- diagnostics : health, plus la configuration utile, les outils et leur niveau de risque, les dernières erreurs du
  journal d'activité.
- benchmark : temps réels du wake word, du STT (extrait de tests/fixtures/scenario.wav), du LLM (réponse simple,
  choix d'outil, commandes sans LLM), d'un outil en lecture seule et de la synthèse vocale (jamais jouée).
Rien n'est allumé, réglé ni prononcé.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OK, WARN, FAIL, OFF = "OK", "AVERT", "ÉCHEC", "—"
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    critical: bool = False
    seconds: float = 0.0


def _timed(name: str, critical: bool, check: Callable[[], tuple[str, str]]) -> Check:
    started = time.perf_counter()
    try:
        status, detail = check()
    except Exception as exc:  # un diagnostic ne doit jamais planter
        status, detail = FAIL, f"{type(exc).__name__} : {str(exc)[:120]}"
    return Check(name, status, detail, critical, time.perf_counter() - started)


def _http_json(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def _tcp(url: str, timeout: float = 1.0) -> bool:
    from jarvis.llm.failover import tcp_reachable

    return tcp_reachable(url, timeout)


def _file(path: Path, label: str) -> tuple[str, str]:
    return (OK, str(path)) if Path(path).exists() else (FAIL, f"{label} introuvable : {path}")


def health_checks(cfg) -> list[tuple[str, bool, Callable[[], tuple[str, str]]]]:
    from jarvis.config import secret
    from jarvis.factory import ENV_FILE

    checks: list[tuple[str, bool, Callable[[], tuple[str, str]]]] = []

    def files() -> tuple[str, str]:
        missing = [str(p) for p in (cfg.wake_word.model, cfg.wake_word.melspectrogram_model,
                                    cfg.wake_word.embedding_model, cfg.tts.voice) if not Path(p).exists()]
        return (FAIL, "manquants : " + ", ".join(missing)) if missing else (OK, "wake word, voix Piper")

    checks.append(("Modèles (wake word, voix)", True, files))

    def stt() -> tuple[str, str]:
        root = Path(cfg.stt.download_root)
        found = root.exists() and any(cfg.stt.model in p.name for p in root.iterdir())
        return (OK, f"whisper {cfg.stt.model} ({cfg.stt.device})") if found else \
            (WARN, f"whisper {cfg.stt.model} pas encore téléchargé dans {root}")

    checks.append(("STT", True, stt))

    def llm(url: str, model: str) -> Callable[[], tuple[str, str]]:
        def check() -> tuple[str, str]:
            if not _tcp(url, cfg.llm.connect_timeout):
                return FAIL, f"injoignable ({url})"
            started = time.perf_counter()
            data = _http_json(f"{url.rstrip('/')}/api/ps", 3.0)
            loaded = [m.get("name") for m in data.get("models", [])]
            delay = time.perf_counter() - started
            state = OK if model in loaded else WARN
            note = "chargé" if model in loaded else f"pas chargé (chargés : {', '.join(loaded) or 'aucun'})"
            return state, f"{model} {note}, réponse {delay * 1000:.0f} ms ({url})"
        return check

    checks.append(("LLM principal (worker)", True, llm(cfg.llm.host, cfg.llm.model)))
    if cfg.llm.fallback_host:
        checks.append(("LLM de secours", False, llm(cfg.llm.fallback_host, cfg.llm.fallback_model or cfg.llm.model)))

    if cfg.web.enabled:
        def web() -> tuple[str, str]:
            with urllib.request.urlopen(f"{cfg.web.base_url.rstrip('/')}/healthz", timeout=2.0):
                return OK, cfg.web.base_url
        checks.append(("Recherche Web (SearXNG)", False, web))

    token = secret("JARVIS_AGENT_TOKEN", ENV_FILE)
    for key, spec in (cfg.tools.devices or {}).items():
        url = str(spec.get("url", "")).rstrip("/")
        if not url:
            checks.append((f"Appareil {key}", False, lambda: (OFF, "interdit (aucune action)")))
            continue

        def agent(url: str = url) -> tuple[str, str]:
            data = _http_json(f"{url}/health", 2.0)
            return (OK, url) if data.get("status") == "ok" else (FAIL, f"réponse inattendue de {url}")
        checks.append((f"Agent {key}", False, agent))
    if cfg.audio.remote:
        checks.append(("Terminal audio (micro distant)", True,
                       lambda: (OK, cfg.audio.remote) if _tcp(cfg.audio.remote) else (FAIL, f"injoignable ({cfg.audio.remote})")))
    if (cfg.tools.devices or cfg.audio.remote or cfg.api.enabled) and len(token) < 32:
        checks.append(("Jeton partagé", True, lambda: (FAIL, "JARVIS_AGENT_TOKEN absent ou trop court dans .env")))

    if cfg.call.enabled:
        checks.append(("Passerelle d'appel (Linphone)", False,
                       lambda: (OK, cfg.call.gateway) if _tcp(cfg.call.gateway) else (FAIL, "injoignable")))

    if cfg.lights.enabled and cfg.lights.rooms:
        from jarvis.factory import build_lights

        def lights() -> tuple[str, str]:
            rooms, driver = build_lights(cfg)
            states, failed = [], []
            for room in rooms.rooms():
                try:
                    state = driver.state(room)
                    states.append(f"{room.key} {'allumée' if state['on'] else 'éteinte'}")
                except Exception:
                    failed.append(room.key)
            detail = ", ".join(states) + (f" ; sans réponse : {', '.join(failed)}" if failed else "")
            return (FAIL if not states else WARN if failed else OK), detail
        checks.append(("Lumières Tuya (lecture)", False, lights))

    def json_file(path: Path, label: str) -> Callable[[], tuple[str, str]]:
        def check() -> tuple[str, str]:
            if not Path(path).exists():
                return OK, f"{label} : aucun fichier (vide)"
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return OK, f"{label} : {len(data)} élément(s)"
        return check

    if cfg.memory.enabled:
        checks.append(("Mémoire", False, json_file(cfg.memory.path, "faits retenus")))
    if cfg.timers.enabled:
        checks.append(("Minuteurs et rappels", False,
                       json_file(cfg.timers.path, "échéances") if cfg.timers.persist else lambda: (OK, "en mémoire")))
    if cfg.routines.enabled:
        checks.append(("Routines", False, json_file(cfg.routines.path, "routines")))
    calendar_path = getattr(getattr(cfg, "calendar", None), "path", None)
    if calendar_path is not None and getattr(cfg.calendar, "enabled", False):
        checks.append(("Calendrier", False, json_file(calendar_path, "événements")))
    checks.append(("Notifications", False,
                   lambda: (OK, "voix activée") if cfg.notifications.voice_enabled else (WARN, "voix désactivée")))
    if cfg.alarms.enabled:
        checks.append(("Sonnerie du réveil", False, lambda: (OK, str(cfg.alarms.sound)) if Path(cfg.alarms.sound).exists()
                       else (WARN, f"{cfg.alarms.sound} absent : sonnerie de secours")))

    def disk() -> tuple[str, str]:
        usage = shutil.disk_usage(ROOT)
        free = usage.free / 1e9
        return (OK if free > 5 else WARN if free > 1 else FAIL), f"{free:.0f} Go libres"
    checks.append(("Disque", False, disk))

    def ram() -> tuple[str, str]:
        try:
            info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
            available = int(info["MemAvailable"].split()[0]) / 1e6
            total = int(info["MemTotal"].split()[0]) / 1e6
            return (OK if available > 1.5 else WARN), f"{available:.1f} Go disponibles sur {total:.1f}"
        except OSError:
            return OK, "non mesurée sur ce système"
    checks.append(("Mémoire vive", False, ram))
    return checks


def run_health(cfg, skip: tuple[str, ...] = ()) -> list[Check]:
    checks = [c for c in health_checks(cfg) if c[0] not in skip]
    with ThreadPoolExecutor(max_workers=max(1, len(checks))) as pool:
        return list(pool.map(lambda c: _timed(*c), checks))


def print_checks(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.status:6s} {c.name:<{width}}  {c.detail}  ({c.seconds * 1000:.0f} ms)")
    failed = [c for c in checks if c.status == FAIL and c.critical]
    print(f"\n{len([c for c in checks if c.status == OK])}/{len(checks)} OK"
          + (f" — composant(s) indispensable(s) en échec : {', '.join(c.name for c in failed)}" if failed else ""))
    return 1 if failed else 0


def health(cfg) -> int:
    print(f"ORION — état ({platform.node()}, {time.strftime('%Y-%m-%d %H:%M')})\n")
    return print_checks(run_health(cfg))


def diagnostics(cfg) -> int:
    code = health(cfg)
    print("\nConfiguration")
    print(f"  LLM : {cfg.llm.model} sur {cfg.llm.host}"
          + (f", secours {cfg.llm.fallback_model} sur {cfg.llm.fallback_host}" if cfg.llm.fallback_host else ""))
    print(f"  STT : whisper {cfg.stt.model} ({cfg.stt.device}/{cfg.stt.compute_type}), TTS : {cfg.tts.engine}")
    print(f"  Wake word : seuil {cfg.wake_word.threshold}, {cfg.wake_word.patience} image(s)")
    print(f"  Audio : {cfg.audio.remote or 'local'} ; raccourci sans LLM : {'oui' if cfg.tools.fast_path else 'non'}")
    print(f"  Python {platform.python_version()} sur {platform.system()} {platform.release()}, {os.cpu_count()} cœurs")
    try:
        from jarvis.config import Config  # noqa: F401
        from jarvis.factory import build_devices, build_lights, build_memory, build_tools, light_tools_for
        from jarvis.personality import load_personality

        personality = load_personality(cfg.assistant.personality)
        extra = []
        memory = build_memory(cfg)
        if memory is not None:
            from jarvis.memory import memory_tools

            extra = memory_tools(memory)
        rooms, driver = build_lights(cfg)
        core = build_tools(cfg, personality, devices=build_devices(cfg), lights=light_tools_for(rooms, driver), extra=extra)
        if core is not None:
            print(f"\nOutils ({len(core.registry)}) — hors minuteurs, météo, réveils et routines, ajoutés au démarrage")
            for tool in core.registry.list():
                print(f"  {tool.name:28s} {tool.risk.value}")
    except Exception as exc:
        print(f"\nOutils : liste impossible ({exc})")
    if cfg.activity.enabled and Path(cfg.activity.path).exists():
        from jarvis.activity import JsonlActivityStore

        failures = [r for r in JsonlActivityStore(cfg.activity.path).read(500) if str(r.get("type", "")).endswith("failed")]
        print(f"\nDerniers échecs ({len(failures)} sur les 500 dernières activités)")
        for record in failures[-5:]:
            payload = record.get("payload", {})
            print(f"  {record.get('time')}  {payload.get('tool') or payload.get('name')}  {payload.get('error') or ''}")
    return code


def benchmark(cfg, runs: int = 3) -> int:
    """Mesures réelles, sans rien jouer ni actionner. Chaque mesure : médiane de ``runs`` essais."""
    import statistics

    import numpy as np

    from jarvis.audio.files import read_wav
    from jarvis.audio.resample import resample
    from jarvis.factory import build_llm, build_stt, build_tts, build_wake_word
    from jarvis.interfaces import Message

    def median(action: Callable[[], object]) -> float:
        values = []
        for _ in range(runs):
            started = time.perf_counter()
            action()
            values.append(time.perf_counter() - started)
        return statistics.median(values)

    results: list[tuple[str, str]] = []
    sample = ROOT / "tests" / "fixtures" / "scenario.wav"
    audio, rate = read_wav(sample) if sample.exists() else (np.zeros(16000 * 4, np.int16), 16000)
    audio = resample(audio, rate, 16000) if rate != 16000 else audio

    wake = build_wake_word(cfg)
    frames = [audio[i:i + cfg.audio.frame_samples] for i in range(0, 16000 * 4, cfg.audio.frame_samples)]
    frames = [f for f in frames if len(f) == cfg.audio.frame_samples]
    per_frame = median(lambda: [wake.process(f) for f in frames]) / max(1, len(frames))
    results.append(("Wake word (par image de 80 ms)", f"{per_frame * 1000:.1f} ms ({per_frame / 0.08 * 100:.0f} % d'un cœur)"))

    stt = build_stt(cfg)
    segment = audio[2 * 16000:6 * 16000]
    results.append(("STT (4 s de parole)", f"{median(lambda: stt.transcribe(segment, 16000)):.2f} s"))

    llm = build_llm(cfg)
    from jarvis.capabilities import CapabilityRegistry
    from jarvis.factory import build_devices, build_lights, build_tools, light_tools_for
    from jarvis.personality import load_personality
    from jarvis.router import IntentRouter
    from jarvis.tools import ToolsCapability, plan
    from jarvis.tools.quick import quick_plan

    personality = load_personality(cfg.assistant.personality)
    rooms, driver = build_lights(cfg)
    core = build_tools(cfg, personality, devices=build_devices(cfg), lights=light_tools_for(rooms, driver))
    capabilities = CapabilityRegistry()
    if core is not None:
        capabilities.register(ToolsCapability(core.registry))
    router = IntentRouter(personality, capabilities, tools=tuple(t.name for t in core.registry.list()) if core else ())
    messages = [Message("system", router.system_prompt()), Message("user", "Quelle est la capitale du Japon ?")]

    def first_token() -> float:
        started = time.perf_counter()
        for _ in llm.stream(messages):
            return time.perf_counter() - started
        return time.perf_counter() - started

    try:
        tokens = [first_token() for _ in range(runs)]
        results.append(("LLM premier mot (question simple)", f"{statistics.median(tokens):.2f} s"))
        if core is not None:
            text = "Mets le son à 40 %"
            results.append(("LLM choix d'outil", f"{median(lambda: plan(llm, text, core.registry)):.2f} s"))
            results.append(("Choix d'outil sans LLM (raccourci)",
                            f"{median(lambda: quick_plan(text, core.registry)) * 1000:.1f} ms"))
    except Exception as exc:
        results.append(("LLM", f"échec : {exc}"))
    if core is not None:
        call = {"type": "tool_call", "tool": "get_time", "parameters": {}}
        results.append(("Outil en lecture seule (get_time)", f"{median(lambda: core.submit(call)) * 1000:.1f} ms"))
        if core.registry.exists("light_status"):
            status = {"type": "tool_call", "tool": "light_status", "parameters": {"room": "all"}}
            results.append(("Lecture des ampoules (toutes)", f"{median(lambda: core.submit(status)):.2f} s"))

    tts = build_tts(cfg)
    sentence = "La lumière de la chambre est allumée à trente pour cent."
    seconds = median(lambda: tts.synthesize(sentence))
    wav, sr = tts.synthesize(sentence)
    results.append(("TTS (phrase de 4 s, non jouée)", f"{seconds:.2f} s (facteur temps réel {seconds / (len(wav) / sr):.2f})"))

    hostname = socket.gethostname()
    print(f"ORION — mesures ({hostname}, médiane de {runs} essais, rien n'est joué ni actionné)\n")
    width = max(len(name) for name, _ in results)
    for name, value in results:
        print(f"  {name:<{width}}  {value}")
    return 0


# --- Diagnostic à la demande (« lance un diagnostic de l'environnement ») ---------------------------------------

SPOKEN_NAMES = {"LLM principal (worker)": "le module de réflexion du Katana", "LLM de secours": "le module de secours",
                "Recherche Web (SearXNG)": "la recherche web", "Terminal audio (micro distant)": "le micro du PC",
                "Lumières Tuya (lecture)": "les lumières", "Passerelle d'appel (Linphone)": "la passerelle d'appel",
                "Modèles (wake word, voix)": "les modèles de voix", "Mémoire vive": "la mémoire vive",
                "Agent pc": "l'agent de votre PC", "Agent portable": "l'agent de votre PC portable"}


def diagnostic_tool(cfg, devices=None, client=None, presence=None, rooms=None, driver=None):
    """Outil en lecture seule : appareils en ligne ou non (PC, PC portable, mini-PC, iPhone sur le Wi-Fi, lumières),
    applications connues ouvertes sur les PC allumés, puis les contrôles de ``python -m jarvis --health``."""
    from jarvis.tools.base import Risk, Tool

    def apps_on(device) -> list[str] | None:
        """Applications du catalogue d'ORION ouvertes sur ce PC (jamais les titres des fenêtres) ; None si le PC n'a
        pas répondu à temps (le PC fixe met plusieurs secondes à parcourir ses processus)."""
        if client is None:
            return []
        try:
            result = client.call(device, "list_running_applications", {})
        except Exception:  # noqa: BLE001 - PC trop lent ou qui vient de s'éteindre
            return None
        return [str(a.get("label") or a.get("application"))[:30] for a in result.get("applications", [])
                if isinstance(a, dict) and a.get("running")][:8]

    def run() -> dict:
        checks = run_health(cfg, skip=("Lumières Tuya (lecture)",) if driver is not None else ())
        by_name = {c.name: c for c in checks}
        online, offline = [], []
        for key in (devices.keys() if devices is not None else ()):
            device = devices[key]
            if not device.url:  # le mini-PC du Core : c'est ORION elle-même, nommé en premier
                online.insert(0, {"name": device.name})
                continue
            check = by_name.get(f"Agent {key}")
            if check is not None and check.status == OK:
                online.append({"name": device.name, "device": device})
            else:
                offline.append(device.name)
        with ThreadPoolExecutor(max_workers=4) as pool:  # les PC en parallèle
            listed = list(pool.map(lambda d: apps_on(d["device"]) if "device" in d else [], online))
        for entry, apps in zip(online, listed):
            entry.pop("device", None)
            entry["apps"] = apps
        phone = None
        if presence is not None:
            users = presence.engine.snapshot()["users"]
            phone = next((u.get("phone") for u in users.values() if u.get("phone")), None)
        lights_on, lights_off, lights_down = [], [], []
        for room in (rooms.rooms() if rooms is not None and driver is not None else ()):
            try:
                (lights_on if driver.state(room)["on"] else lights_off).append(room.name)
            except Exception:  # noqa: BLE001 - ampoule hors tension ou hors réseau
                lights_down.append(room.name)
        return {"total": len(checks), "ok": sum(c.status in (OK, OFF) for c in checks),
                "warnings": [(c.name, c.detail[:80]) for c in checks if c.status == WARN],
                "failures": [(c.name, c.detail[:80], c.critical) for c in checks
                             if c.status == FAIL and not c.name.startswith("Agent ")],
                "online": online, "offline": offline, "phone": phone,
                "lights": {"on": lights_on, "off": lights_off, "down": lights_down}}

    def said(r: dict) -> str:
        name = lambda n: SPOKEN_NAMES.get(n, n.lower())  # noqa: E731
        parts = ["Diagnostic terminé, monsieur."]
        if r["online"]:
            described = []
            for d in r["online"]:
                apps = d["apps"]
                if apps is None:
                    described.append(f"{d['name']}, dont je n'ai pas pu lire les applications à temps")
                    continue
                described.append(d["name"] + (f", où sont ouverts {_join(apps)}" if len(apps) > 1 else
                                              f", où {apps[0]} est ouvert" if apps else ""))
            with_apps = any(d["apps"] or d["apps"] is None for d in r["online"])
            parts.append("En ligne : " + (" ; ".join(described) if with_apps else _join(described)) + ".")
        if r["offline"]:
            parts.append("Hors ligne : " + _join(r["offline"]) + ".")
        if r["phone"] == "home":
            parts.append("Votre iPhone est connecté au Wi-Fi de la maison.")
        elif r["phone"] == "away":
            parts.append("Votre iPhone n'est pas sur le Wi-Fi de la maison.")
        lights = r["lights"]
        if lights["on"] or lights["off"]:
            said_lights = [f"{_of(n)} allumée" for n in lights["on"]] + [f"{_of(n)} éteinte" for n in lights["off"]]
            parts.append("Lumières : " + _join(said_lights) + ".")
        if lights["down"]:
            parts.append("Lumières sans réponse : " + _join([_of(n) for n in lights["down"]]) + ".")
        if not r["warnings"] and not r["failures"]:
            parts.append(f"Côté système, les {r['total']} contrôles sont bons.")
        else:
            parts.append(f"Côté système, {r['ok']} contrôles sur {r['total']} sont bons.")
            if r["failures"]:
                parts.append("En panne : " + _join([name(n) for n, _, _ in r["failures"][:4]]) + ".")
            if r["warnings"]:
                parts.append("À surveiller : " + _join([f"{name(n)} ({d})" for n, d in r["warnings"][:3]]) + ".")
        return " ".join(parts)

    return Tool("run_diagnostic", "Diagnostic de l'environnement d'ORION : appareils en ligne ou non, applications "
                "ouvertes sur les PC allumés, lumières, iPhone, modules et ressources ; lecture seule.", {},
                {"online": "appareils en ligne", "offline": "hors ligne", "failures": "en panne"}, Risk.SAFE, run,
                say=said)


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1] if items else ""


def _of(room: str) -> str:
    """« l'entrée », « la chambre » : déjà avec leur article dans la configuration ; sinon tel quel."""
    return room
