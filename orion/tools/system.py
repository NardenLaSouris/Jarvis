"""Outils système : heure, date, informations non sensibles sur la machine, verrouillage de la session.

Uniquement des API du système (registre en lecture, kernel32/user32 sous Windows, /proc sous Linux) et, pour la
charge de la carte graphique NVIDIA, son outil officiel ``nvidia-smi`` lancé avec des arguments fixes (sans shell) :
aucune variable d'environnement renvoyée, aucun fichier personnel lu.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from orion.personality import MONTHS, WEEKDAYS, normalize, spoken_time
from orion.tools.base import EXECUTION_FAILED, Param, Risk, Tool, ToolError

GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
IGNORED_GPUS = ("microsoft basic", "microsoft remote", "virtual", "parsec", "meta virtual")


def time_tool(clock: Callable[[], datetime]) -> Tool:
    def run() -> dict:
        now = clock()
        return {"time": now.strftime("%H:%M"), "spoken": spoken_time(now)}

    return Tool("get_time", "Donne l'heure locale.", {}, {"time": "HH:MM", "spoken": "heure en toutes lettres"},
                Risk.SAFE, run, say=lambda r: f"Il est {r['spoken']}.")


DAY_OFFSETS = {"yesterday": -1, "today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
DAY_SAID = {"yesterday": ("Hier, nous étions", "hier"), "tomorrow": ("Demain, nous serons", "demain"),
            "day_after_tomorrow": ("Après-demain, nous serons", "apres demain")}


def said_day(text: str) -> str | None:
    """Jour nommé dans la demande (« demain », « après-demain », « hier ») ; None pour aujourd'hui."""
    norm = f" {normalize(text)} "
    for key in ("day_after_tomorrow", "yesterday", "tomorrow"):
        if f" {DAY_SAID[key][1]} " in norm:
            return key
    return None


def date_tool(clock: Callable[[], datetime]) -> Tool:
    def run(day: str | None = None) -> dict:
        when = clock() + timedelta(days=DAY_OFFSETS.get(day or "today", 0))
        number = "1er" if when.day == 1 else str(when.day)
        result = {"date": when.strftime("%Y-%m-%d"), "weekday": WEEKDAYS[when.weekday()],
                  "spoken": f"{WEEKDAYS[when.weekday()]} {number} {MONTHS[when.month - 1]} {when.year}"}
        return {**result, "day": day} if day and day != "today" else result

    def said(r: dict) -> str:
        lead = DAY_SAID.get(r.get("day", ""), ("Nous sommes",))[0]
        return f"{lead} le {r['spoken']}."

    return Tool("get_date", "Donne la date du jour (ou d'hier, de demain, d'après-demain).",
                {"day": Param(str, "jour", required=False, choices=tuple(DAY_OFFSETS), hidden=True, resolve=said_day)},
                {"date": "AAAA-MM-JJ", "weekday": "jour", "spoken": "date en toutes lettres"}, Risk.SAFE, run,
                say=said)


# --- Informations machine -------------------------------------------------------------------------

def _registry_value(root, path: str, name: str):
    try:
        import winreg

        with winreg.OpenKey(root, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _memory_gb() -> tuple[float | None, float | None]:
    try:
        if sys.platform == "win32":
            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.total / 2**30, 1), round(status.available / 2**30, 1)
        elif Path("/proc/meminfo").exists():
            info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
            kb = lambda key: int(info[key].split()[0])  # noqa: E731
            return round(kb("MemTotal") / 2**20, 1), round(kb("MemAvailable") / 2**20, 1)
    except Exception:
        pass
    return None, None


def _cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        name = _registry_value(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                               "ProcessorNameString")
        if name:
            return str(name).strip()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _gpus() -> list[str]:
    names: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GPU_CLASS) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    sub = winreg.EnumKey(key, i)
                    if sub.isdigit():
                        desc = _registry_value(winreg.HKEY_LOCAL_MACHINE, f"{GPU_CLASS}\\{sub}", "DriverDesc")
                        if desc:
                            names.append(str(desc).strip())
        except OSError:
            pass
    else:
        for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
            for line in info.read_text(errors="ignore").splitlines():
                if line.startswith("Model:"):
                    names.append(line.split(":", 1)[1].strip())
    return [n for n in dict.fromkeys(names) if not any(word in n.lower() for word in IGNORED_GPUS)]


_CPU_SAMPLE: list = []  # dernier relevé (occupé, total) : la charge est l'écart entre deux appels


def _cpu_times() -> tuple[int, int] | None:
    """(temps occupé, temps total) du processeur depuis le démarrage."""
    try:
        if sys.platform == "win32":
            idle, kernel, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            total = kernel.value + user.value  # le temps noyau comprend le temps d'inactivité
            return total - idle.value, total
        values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        total = sum(values)
        return total - values[3] - (values[4] if len(values) > 4 else 0), total
    except (OSError, ValueError, AttributeError, IndexError):
        return None


def _cpu_percent() -> float | None:
    """Charge du processeur depuis l'appel précédent (None au premier appel)."""
    sample = _cpu_times()
    previous = _CPU_SAMPLE[0] if _CPU_SAMPLE else None
    _CPU_SAMPLE[:] = [sample] if sample else []
    if sample is None or previous is None or sample[1] <= previous[1]:
        return None
    return round(100.0 * (sample[0] - previous[0]) / (sample[1] - previous[1]), 1)


def _nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        default = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "nvidia-smi.exe"
        return str(default) if default.is_file() else None
    return None


def _gpu_stats() -> list[dict]:
    """Utilisation, température et mémoire des cartes NVIDIA (vide sans carte NVIDIA ou sans pilote)."""
    program = _nvidia_smi()
    if program is None:
        return []
    try:
        out = subprocess.run([program, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                             shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    stats = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 5:
            continue
        number = lambda v: float(v) if v.replace(".", "", 1).isdigit() else None  # noqa: E731 - « [N/A] »
        stats.append({"name": parts[0][:60], "usage": number(parts[1]), "temperature": number(parts[2]),
                      "memory_used_mb": number(parts[3]), "memory_total_mb": number(parts[4])})
    return stats


def _disk_gb() -> tuple[float | None, float | None]:
    root = (os.environ.get("SystemDrive", "C:") + "\\") if sys.platform == "win32" else "/"
    try:
        usage = shutil.disk_usage(root)
        return round(usage.total / 2**30, 1), round(usage.free / 2**30, 1)
    except OSError:
        return None, None


def _uptime_hours() -> float | None:
    try:
        if sys.platform == "win32":
            ticks = ctypes.windll.kernel32.GetTickCount64
            ticks.restype = ctypes.c_ulonglong
            return round(ticks() / 3_600_000, 1)
        return round(float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1)
    except (OSError, ValueError, AttributeError):
        return None


def system_info_tool() -> Tool:
    def run() -> dict:
        total, available = _memory_gb()
        disk_total, disk_free = _disk_gb()
        return {
            "os": platform.system(),
            "os_version": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpu": _cpu_name(),
            "cpu_cores": os.cpu_count(),
            "memory_total_gb": total,
            "memory_available_gb": available,
            "disk_total_gb": disk_total,
            "disk_free_gb": disk_free,
            "gpu": _gpus(),
            "uptime_hours": _uptime_hours(),
            "cpu_percent": _cpu_percent(),
            "gpu_stats": _gpu_stats(),
        }

    return Tool("system_info", "Donne des informations non sensibles sur la machine : système, processeur et sa "
                "charge, mémoire, stockage, carte graphique et sa charge, durée depuis le démarrage.", {},
                {"os": "système", "os_version": "version", "architecture": "architecture", "python": "version de Python",
                 "cpu": "processeur", "cpu_cores": "cœurs logiques", "memory_total_gb": "mémoire totale (Go)",
                 "memory_available_gb": "mémoire disponible (Go)", "disk_total_gb": "disque système (Go)",
                 "disk_free_gb": "espace libre (Go)", "gpu": "cartes graphiques", "uptime_hours": "allumé depuis (h)",
                 "cpu_percent": "charge du processeur depuis la dernière lecture (%)",
                 "gpu_stats": "utilisation, température et mémoire des cartes NVIDIA"},
                Risk.SAFE, run)


# --- Verrouillage ---------------------------------------------------------------------------------

def lock_session() -> bool:
    """Verrouille la session : LockWorkStation (Windows) ou loginctl (Linux). Rend True si le système a accepté."""
    if sys.platform == "win32":
        return bool(ctypes.windll.user32.LockWorkStation())
    try:
        return subprocess.run(["loginctl", "lock-session"], capture_output=True, timeout=10, shell=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def lock_tool(locker: Callable[[], bool] = lock_session) -> Tool:
    def run() -> dict:
        if not locker():
            raise ToolError(EXECUTION_FAILED, "Je n'ai pas pu verrouiller la session.")
        return {"locked": True}

    return Tool("lock_pc", "Verrouille la session de l'ordinateur (l'écran de connexion s'affiche).", {},
                {"locked": "true"}, Risk.CONFIRMATION_REQUIRED, run,
                question=lambda p: "Voulez-vous que je verrouille l'ordinateur ?",
                say=lambda r: "L'ordinateur est verrouillé.")
