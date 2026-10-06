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
    from zoneinfo import ZoneInfo

    from jarvis.agenda import occurrences

    def paris(*args):  # heure de Paris vue depuis le fuseau de la machine qui lance les tests
        return datetime(*args, tzinfo=ZoneInfo("Europe/Paris")).astimezone().replace(tzinfo=None)

    events = {e.id: e for e in parse_ics(RECURRING)}
    week = occurrences(events["sport"], datetime(2026, 10, 5), datetime(2026, 10, 12))
    assert [e.start for e in week] == [paris(2026, 10, 5, 19, 0)]  # jeudi 8 exclu (EXDATE)
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


# --- Jours de la semaine, dates, moments (session QA : « jeudi » donnait aujourd'hui) -------------------------

import pytest  # noqa: E402
from datetime import date  # noqa: E402

from jarvis.agenda import said_date, said_moment  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("Qu'est-ce que j'ai de prévu jeudi ?", date(2026, 10, 8)),
    ("Suis-je libre lundi ?", date(2026, 10, 5)),
    ("Et lundi prochain ?", date(2026, 10, 12)),
    ("Mon agenda de demain", date(2026, 10, 6)),
    ("Après-demain", date(2026, 10, 7)),
    ("Ajoute un rendez-vous le 12 à 15 heures", date(2026, 10, 12)),
    ("Ajoute un rendez-vous le 3 à 15 heures", date(2026, 11, 3)),
    ("Qu'est-ce que j'ai le 2 janvier ?", date(2027, 1, 2)),
    ("Le 31 février", None),
    ("Ajoute le dentiste à 15 heures", None),
    ("Dans 30 minutes", None),
])
def test_said_dates(text, expected):
    assert said_date(text, NOW.date()) == expected


def test_a_weekday_reaches_the_tools_through_the_hidden_day(tmp_path):
    calendar, local, core = setup(tmp_path)
    day = core.registry.get("list_events").parameters["day"].resolve("Qu'est-ce que j'ai de prévu jeudi ?")
    assert day == "2026-10-08"
    assert run(core, "list_events", day=day).message == "Rien de prévu jeudi 8 octobre."
    added = run(core, "add_event", title="Dentiste", time="15 heures", day=day)
    assert added.message == "C'est noté dans votre calendrier : jeudi 8 octobre à 15 heures, Dentiste."
    assert run(core, "list_events", day="tomorrow").success  # forme enregistrée dans les routines : toujours valide
    assert run(core, "list_events", day="mardi").error == "invalid_parameters"


def test_free_slots_of_an_afternoon(tmp_path):
    calendar, local, core = setup(tmp_path)
    assert said_moment("Suis-je libre jeudi après-midi ?") == "afternoon"
    result = run(core, "free_slots", moment="afternoon")
    assert result.result["slots"] == ["de 12 heures à 14 h 30", "de 15 heures à 18 heures"]
    assert result.message.startswith("Aujourd'hui après-midi, vous êtes libre")


def test_an_invented_duration_is_dropped_not_the_whole_event(tmp_path):
    # Session QA : le LLM ajoutait « duration: 1 heure » jamais dit : rendez-vous refusé (« pas encore disponible »).
    from jarvis.tools.planner import _grounded

    calendar, local, core = setup(tmp_path)
    call = {"type": "tool_call", "tool": "add_event",
            "parameters": {"title": "dentiste", "time": "15 heures", "duration": "1 heure"}}
    grounded = _grounded(call, "Ajoute un rendez-vous chez le dentiste jeudi à 15 heures", core.registry)
    assert grounded["parameters"] == {"title": "dentiste", "time": "15 heures"}
    said = _grounded(call, "Ajoute le dentiste jeudi à 15 heures pendant 1 heure", core.registry)
    assert said["parameters"]["duration"] == "1 heure"
    wrong_time = {**call, "parameters": {"title": "dentiste", "time": "16 heures"}}
    assert _grounded(wrong_time, "Ajoute le dentiste jeudi à 15 heures", core.registry) is None  # obligatoire : écarté
