"""Assemblage des outils intégrés de JARVIS, selon la section ``[tools]`` de config.toml.

- système (``system.py``) : get_time, get_date, system_info, lock_pc ;
- applications (``applications.py``) : open_application, close_application, list_running_applications, open_url ;
- audio (``audio.py``) : set_volume, mute_volume, unmute_volume.

Chaque outil a un niveau de risque fixé dans son code. La configuration peut seulement le rendre
plus strict (``confirm = true`` sur un outil SAFE), jamais l'assouplir.
"""

from __future__ import annotations

import logging
import webbrowser
from dataclasses import replace
from datetime import datetime
from typing import Callable

from jarvis.tools.applications import (
    CATALOG, Application, Processes, _launch, close_app_tool, list_apps_tool, load_applications, open_app_tool,
    url_tool, validate_url,
)
from jarvis.tools.audio import VolumeControl, mute_tool, set_volume_tool
from jarvis.tools.base import Risk, Tool
from jarvis.tools.system import date_tool, lock_session, lock_tool, system_info_tool, time_tool

log = logging.getLogger(__name__)

__all__ = ["CATALOG", "Application", "Processes", "builtin_tools", "load_applications", "validate_url"]


def builtin_tools(settings: dict | None = None, clock: Callable[[], datetime] = datetime.now,
                  opener: Callable[[str], bool] | None = None, launcher: Callable[[list[str]], None] = _launch,
                  processes: Processes | None = None, volume: VolumeControl | None = None,
                  locker: Callable[[], bool] = lock_session, open_wait: float = 4.0,
                  close_wait: float = 6.0) -> list[Tool]:
    """Outils activés selon ``settings`` ({nom: {"enabled": bool, "confirm": bool}, "applications": {...}}).

    Tous les outils sont activés par défaut ; les applications autorisées viennent de ``applications``.
    """
    settings = settings or {}
    enabled = lambda name: settings.get(name, {}).get("enabled", True)  # noqa: E731
    apps = load_applications(settings.get("applications"))
    processes = processes or Processes()
    volume = volume or VolumeControl()
    candidates = [
        ("get_time", lambda: time_tool(clock)),
        ("get_date", lambda: date_tool(clock)),
        ("system_info", system_info_tool),
        ("open_url", lambda: url_tool(opener or (lambda url: webbrowser.open(url, new=2)))),
        ("open_application", lambda: open_app_tool(apps, launcher, processes, open_wait)),
        ("close_application", lambda: close_app_tool(apps, processes, launcher, close_wait)),
        ("list_running_applications", lambda: list_apps_tool(apps, processes)),
        ("set_volume", lambda: set_volume_tool(volume)),
        ("mute_volume", lambda: mute_tool(volume, True)),
        ("unmute_volume", lambda: mute_tool(volume, False)),
        ("lock_pc", lambda: lock_tool(locker)),
    ]
    tools = []
    for name, build in candidates:
        if not enabled(name):
            continue
        tool = build()
        confirm = settings.get(name, {}).get("confirm")
        if confirm is True and tool.risk is Risk.SAFE:
            tool = replace(tool, risk=Risk.CONFIRMATION_REQUIRED)
        elif confirm is False and tool.risk is not Risk.SAFE:
            log.warning("[tools.%s] confirm = false ignoré : cet outil exige toujours une confirmation.", name)
        tools.append(tool)
    return tools
