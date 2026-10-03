"""Annonces des routines : heure, date, météo, ou résumé de la journée, construits par les outils de JARVIS."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from jarvis.tools import DONE

WHAT = ("time", "date", "weather", "day")


class Announcer:
    def __init__(self, run_tool: Callable[[dict], object], today_events: Callable[[], list[str]],
                 clock: Callable[[], datetime] = datetime.now):
        self._run_tool = run_tool
        self._today_events = today_events
        self._clock = clock

    def _said(self, tool: str) -> str:
        outcome = self._run_tool({"type": "tool_call", "tool": tool, "parameters": {}})
        result = getattr(outcome, "result", None)
        ok = getattr(outcome, "status", None) == DONE and result is not None and result.success
        return result.message if ok and result.message else ""

    def text(self, what: str) -> str:
        if what == "time":
            return self._said("get_time")
        if what == "date":
            return self._said("get_date")
        if what == "weather":
            return self._said("get_weather") or "La météo n'est pas disponible pour le moment."
        events = self._today_events()
        agenda = ("Au programme aujourd'hui : " + " ; ".join(events) + ".") if events else "Rien de prévu aujourd'hui."
        parts = [self._said("get_date"), self._said("get_weather"), agenda]
        return " ".join(p for p in parts if p)
