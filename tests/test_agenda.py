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


# --- Fuseaux et répétitions (session red team) ------------------------------------------------------

RECURRING = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ny
SUMMARY:Appel New York
DTSTART;TZID=America/New_York:20261005T090000
DTEND;TZID=America/New_York:20261005T100000
END:VEVENT
BEGIN:VEVENT
UID:sport
SUMMARY:Sport
DTSTART;TZID=Europe/Paris:20260907T190000
DTEND;TZID=Europe/Paris:20260907T200000
RRULE:FREQ=WEEKLY;BYDAY=MO,TH;UNTIL=20261231T235959Z
EXDATE;TZID=Europe/Paris:20261008T190000
END:VEVENT
BEGIN:VEVENT
UID:loyer
SUMMARY:Loyer
DTSTART;VALUE=DATE:20260131
RRULE:FREQ=MONTHLY;COUNT=12
END:VEVENT
BEGIN:VEVENT
UID:casse
SUMMARY:Règle cassée
DTSTART:20261005T120000
RRULE:FREQ=HOURLY;INTERVAL=abc
END:VEVENT
BEGIN:VEVENT
SUMMARY:Sans date
DTSTART:pas une date
END:VEVENT
END:VCALENDAR
"""


def test_named_time_zones_are_converted_to_local_time():
    from datetime import timezone
    from zoneinfo import ZoneInfo

    ny = next(e for e in parse_ics(RECURRING) if e.title == "Appel New York")
    expected = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("America/New_York")).astimezone().replace(tzinfo=None)
    assert ny.start == expected and ny.end - ny.start == datetime(1, 1, 1, 1) - datetime(1, 1, 1, 0)
    assert timezone  # import utilisé pour la lisibilité du calcul attendu


def test_recurrences_are_expanded_with_exceptions_and_limits(tmp_path):
    from jarvis.agenda import occurrences

    events = {e.id: e for e in parse_ics(RECURRING)}
    week = occurrences(events["sport"], datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert [e.start for e in week] == [datetime(2026, 10, 5, 19, 0)]  # jeudi 8 exclu (EXDATE)
    assert not occurrences(events["sport"], datetime(2027, 1, 1), datetime(2027, 2, 1))  # après UNTIL
    rent = occurrences(events["loyer"], datetime(2026, 1, 1), datetime(2027, 6, 1))
    assert len(rent) <= 12 and datetime(2026, 2, 28) not in [e.start for e in rent]  # pas de 31 février
    broken = occurrences(events["casse"], datetime(2026, 10, 5), datetime(2026, 10, 6))
    assert len(broken) == 1  # règle illisible : l'événement d'origine seul
    assert "Sans date" not in [e.title for e in events.values()]


def test_recurring_event_appears_on_the_right_day_through_the_tools(tmp_path):
    ics = tmp_path / "r.ics"
    ics.write_text(RECURRING, encoding="utf-8")
    calendar = Calendar([IcsCalendar(str(ics))], clock=lambda: NOW)
    registry = ToolRegistry()
    for tool in calendar_tools(calendar):
        registry.register(tool)
    core = ToolCore(registry, PermissionManager())
    assert "Sport" in run(core, "list_events").message  # lundi 5 octobre : occurrence de la répétition hebdomadaire
