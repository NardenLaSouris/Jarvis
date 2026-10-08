"""Annonces des routines : heure, date, météo, ou résumé de la journée, construits par les outils d'ORION."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from jarvis.tools import DONE

WHAT = ("time", "date", "weather", "day", "welcome")


class Announcer:
    def __init__(self, run_tool: Callable[[dict], object], today_events: Callable[[], list[str]],
                 clock: Callable[[], datetime] = datetime.now, welcome: Callable[[dict], str] | None = None,
                 mail_summary: Callable[[], str] | None = None):
        self._run_tool = run_tool
        self._today_events = today_events
        self._clock = clock
        self._welcome = welcome
        self._mail_summary = mail_summary  # mails importants arrivés pendant la nuit (point du jour)

    def _said(self, tool: str) -> str:
        outcome = self._run_tool({"type": "tool_call", "tool": tool, "parameters": {}})
        result = getattr(outcome, "result", None)
        ok = getattr(outcome, "status", None) == DONE and result is not None and result.success
        return result.message if ok and result.message else ""

    def text(self, what: str, context: dict | None = None) -> str:
        if what == "welcome":  # retour confirmé : « Bon retour, monsieur. » et le résumé de l'absence
            return self._welcome(context or {}) if self._welcome is not None else ""
        if what == "time":
            return self._said("get_time")
        if what == "date":
            return self._said("get_date")
        if what == "weather":
            return self._said("get_weather") or "La météo n'est pas disponible pour le moment."
        events = self._today_events()
        agenda = ("Au programme aujourd'hui : " + " ; ".join(events) + ".") if events else "Rien de prévu aujourd'hui."
        mails = ""
        if self._mail_summary is not None:
            try:
                mails = self._mail_summary()
            except Exception:  # le point du jour ne dépend jamais de la boîte mail
                mails = ""
        parts = [self._said("get_date"), self._said("get_weather"), agenda, mails]
        return " ".join(p for p in parts if p)
