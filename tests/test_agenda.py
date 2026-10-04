"""Calendrier : jour, demain, prochains, recherche, créneaux libres, ajout et suppression ; lecture iCal.

Calendriers temporaires et texte iCal en dur : aucun accès réseau, aucune voix.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.agenda import Calendar, IcsCalendar, LocalCalendar, calendar_tools, parse_ics  # noqa: E402
from jarvis.tools import PermissionManager, ToolCore, ToolRegistry  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402

NOW = datetime(2026, 10, 5, 9, 30)  # lundi
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:a1
SUMMARY:Dentiste
LOCATION:Nantes
DTSTART:20261005T143000
DTEND:20261005T150000
END:VEVENT
BEGIN:VEVENT
UID:a2
SUMMARY:Anniversaire de L
 éa
DTSTART;VALUE=DATE:20261006
DTEND;VALUE=DATE:20261007
END:VEVENT
END:VCALENDAR
"""


def setup(tmp_path):
    ics = tmp_path / "agenda.ics"
    ics.write_text(ICS, encoding="utf-8")
    local = LocalCalendar(tmp_path / "calendar.json")
    calendar = Calendar([local, IcsCalendar(str(ics))], clock=lambda: NOW)
    registry = ToolRegistry()
    for tool in calendar_tools(calendar):
        registry.register(tool)
    return calendar, local, ToolCore(registry, PermissionManager())


def run(core, tool, **parameters):
    return core.submit({"tool": tool, "parameters": parameters}).result


def test_ics_parsing_unfolds_lines_and_reads_all_day_events():
    events = parse_ics(ICS)
    assert [e.title for e in events] == ["Dentiste", "Anniversaire de Léa"]
    assert events[0].start == datetime(2026, 10, 5, 14, 30) and events[0].location == "Nantes"
    assert events[1].all_day and events[1].start == datetime(2026, 10, 6)


def test_today_tomorrow_next_and_search(tmp_path):
    calendar, local, core = setup(tmp_path)
    assert run(core, "list_events").message == \
        "Aujourd'hui, vous avez 1 événement : à 14 h 30, Dentiste (Nantes)."
    assert run(core, "list_events", day="tomorrow").message == \
        "Demain, vous avez 1 événement : toute la journée, Anniversaire de Léa."
    assert run(core, "list_events", day="day_after_tomorrow").message.startswith("Rien de prévu")
    assert run(core, "next_events").message.startswith("À venir : aujourd'hui à 14 h 30, Dentiste")
    assert "Dentiste" in run(core, "search_events", query="dentiste").message


def test_free_slots_skip_busy_times(tmp_path):
    calendar, local, core = setup(tmp_path)
    assert run(core, "free_slots").result["slots"] == ["de 9 h 30 à 14 h 30", "de 15 heures à 20 heures"]


def test_add_then_delete_with_confirmation(tmp_path):
    calendar, local, core = setup(tmp_path)
    added = run(core, "add_event", title="Coiffeur", time="17 heures", day="tomorrow")
    assert added.message == "C'est noté dans votre calendrier : demain à 17 heures, Coiffeur."
    assert [e.title for e in local.events(NOW, NOW.replace(day=7))] == ["Coiffeur"]
    outcome = core.submit({"tool": "delete_event", "parameters": {"title": "coiffeur"}})
    assert outcome.status == "confirm"
    assert core.answer("oui").result.success and local.events(NOW, NOW.replace(day=7)) == []


def test_read_only_ics_events_cannot_be_deleted(tmp_path):
    calendar, local, core = setup(tmp_path)
    core.submit({"tool": "delete_event", "parameters": {"title": "dentiste"}})
    assert not core.answer("oui").result.success


def test_unreachable_calendar_is_reported(tmp_path):
    calendar = Calendar([IcsCalendar(str(tmp_path / "absent.ics"))], clock=lambda: NOW)
    registry = ToolRegistry()
    for tool in calendar_tools(calendar):
        registry.register(tool)
    result = ToolCore(registry, PermissionManager()).submit({"tool": "list_events"}).result
    assert not result.success and "calendrier" in result.message


def test_briefing_lines_and_quick_commands(tmp_path):
    calendar, local, core = setup(tmp_path)
    assert calendar.today_lines() == ["« Dentiste » à 14 h 30"]
    assert quick_plan("Qu'est-ce que j'ai de prévu demain ?", core.registry)["tool"] == "list_events"
    assert quick_plan("Suis-je libre cet après-midi ?", core.registry)["tool"] == "free_slots"
    assert quick_plan("Quels sont mes prochains rendez-vous ?", core.registry)["tool"] == "next_events"
