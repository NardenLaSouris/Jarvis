"""Couleurs du visage : choix à la voix ou par l'API, thème diffusé aux pages, palette par teinte, arc-en-ciel.

Aucun navigateur ni son ; fichiers dans un dossier temporaire.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.face import FaceServer, VisualState  # noqa: E402
from jarvis.face.server import night_hours  # noqa: E402
from jarvis.face.themes import COLORS, THEMES, FaceThemeStore, face_theme_tool, said_theme  # noqa: E402
from jarvis.tools.quick import quick_plan  # noqa: E402
from test_quick import full_core  # noqa: E402
from test_tools import PlannerLLM, run_agent  # noqa: E402


def core_with_colors(tmp_path):
    store = FaceThemeStore(tmp_path / "face_theme.json")
    core = full_core()
    core.registry.register(face_theme_tool(store))
    return core, store


@pytest.mark.parametrize("text, theme", [
    ("Mets ton visage en vert", "vert"), ("Passe en arc-en-ciel", "arcenciel"), ("Change de couleur, violet", "violet"),
    ("Habille-toi en rose", "rose"), ("Mets ton thème en noir et blanc", "night"), ("Mets ton visage en bleu", "day"),
    ("Mets ton visage en bleu ciel", "azur"), ("Remets ta couleur normale", "auto"), ("Mets ton visage en doré", "or"),
])
def test_colours_said_by_voice(tmp_path, text, theme):
    core, store = core_with_colors(tmp_path)
    assert said_theme(text) == theme
    assert quick_plan(text, core.registry) == {"type": "tool_call", "tool": "set_face_theme",
                                               "parameters": {"theme": theme}}


def test_lights_keep_their_colours(tmp_path):
    core, store = core_with_colors(tmp_path)
    assert quick_plan("Mets l'entrée en vert", core.registry)["tool"] == "set_color"
    assert (quick_plan("Change de couleur dans la chambre, en vert", core.registry) or {}).get("tool") != "set_face_theme"


def test_choice_by_voice_reaches_every_face_page(tmp_path):
    core, store = core_with_colors(tmp_path)
    spoken, _ = run_agent(["Mets ton visage en émeraude."], PlannerLLM(), core, fast_path=True)
    assert spoken == ["Je passe en émeraude."] and store.get() == "emeraude"
    clock = lambda: datetime(2026, 10, 5, 23, 0)  # noqa: E731 - la nuit : la couleur choisie l'emporte
    server = FaceServer(VisualState(), night=night_hours("22:00", "07:00"), clock=clock, themes=store)
    assert server.payload()["theme"] == "emeraude" and server.payload()["hue"] == COLORS["emeraude"]
    spoken, _ = run_agent(["Remets ta couleur normale."], PlannerLLM(), core, fast_path=True)
    assert spoken == ["Je reprends mes couleurs habituelles."]
    assert server.payload()["theme"] == "night" and server.payload()["hue"] is None


def test_error_still_wins_and_store_is_robust(tmp_path):
    from jarvis.events import SYSTEM_ERROR, Event, EventBus
    from jarvis.face import FaceBridge

    store = FaceThemeStore(tmp_path / "face_theme.json")
    store.set("rose")
    visual, bus = VisualState(), EventBus()
    FaceBridge(visual).attach(bus)
    server = FaceServer(visual, themes=store)
    bus.publish(Event(SYSTEM_ERROR, "system", {"source": "llm"}))
    assert server.payload()["theme"] == "error" and server.payload()["base_theme"] == "rose"
    (tmp_path / "face_theme.json").write_text("{abîmé", encoding="utf-8")
    assert FaceThemeStore(tmp_path / "face_theme.json").get() == "auto"
    with pytest.raises(ValueError):
        store.set("plaid écossais")
    assert set(COLORS) < set(THEMES) and "arcenciel" in THEMES


def test_page_builds_any_colour_from_its_hue():
    js = (ROOT / "jarvis" / "face" / "static" / "face.js").read_text(encoding="utf-8")
    assert "function huePalette" in js and "arcenciel" in js and "data.hue" in js


def test_llm_cannot_pick_a_colour_that_was_not_said(tmp_path):
    core, store = core_with_colors(tmp_path)
    llm = PlannerLLM({"type": "tool_call", "tool": "set_face_theme", "parameters": {"theme": "rouge"}})
    run_agent(["Change ton thème"], llm, core)
    assert store.get() == "auto"


# --- Thèmes de fête --------------------------------------------------------------------------------

@pytest.mark.parametrize("day, season", [
    ((2026, 12, 31), "nouvelan"), ((2027, 1, 1), "nouvelan"), ((2026, 2, 14), "saintvalentin"),
    ((2026, 3, 17), "saintpatrick"), ((2026, 4, 4), "paques"), ((2026, 4, 6), "paques"), ((2027, 3, 28), "paques"),
    ((2026, 6, 21), "fetemusique"), ((2026, 7, 14), "quatorzejuillet"), ((2026, 10, 31), "halloween"),
    ((2026, 12, 24), "noel"), ((2026, 10, 5), None), ((2026, 4, 7), None),
])
def test_holidays_come_by_themselves(day, season):
    from datetime import date

    from jarvis.face.themes import season_of

    assert season_of(date(*day)) == season


def test_birthday_and_holiday_payload(tmp_path):
    from jarvis.face.themes import SEASONS, parse_birthday

    store = FaceThemeStore(tmp_path / "face_theme.json")
    birthday = parse_birthday("10-05")
    server = FaceServer(VisualState(), clock=lambda: datetime(2026, 10, 5, 23, 0), themes=store, birthday=birthday,
                        night=night_hours("22:00", "07:00"))
    payload = server.payload()
    assert payload["theme"] == "anniversaire" and payload["greeting"] == "Joyeux anniversaire !"
    assert payload["hues"] == "rainbow" and payload["effect"] == "confetti"
    store.set("vert")  # une couleur choisie passe avant la fête
    assert server.payload()["theme"] == "vert" and server.payload()["greeting"] == ""
    store.set("noel")  # essayer un thème de fête à la voix
    assert server.payload()["hues"] == SEASONS["noel"]["hues"] and server.payload()["effect"] == "snow"
    off = FaceServer(VisualState(), clock=lambda: datetime(2026, 12, 24, 12, 0), themes=FaceThemeStore(tmp_path / "x"),
                     seasons=False)
    assert off.payload()["theme"] == "day"
    with pytest.raises(ValueError):
        parse_birthday("13-40")
    assert parse_birthday("") is None


def test_holiday_themes_by_voice(tmp_path):
    core, store = core_with_colors(tmp_path)
    spoken, _ = run_agent(["Mets ton visage en mode Halloween."], PlannerLLM(), core, fast_path=True)
    assert spoken == ["Je passe en thème Halloween."] and store.get() == "halloween"
    js = (ROOT / "jarvis" / "face" / "static" / "face.js").read_text(encoding="utf-8")
    assert all(effect in js for effect in ("snow", "embers", "confetti", "sparkle", "hearts", "clovers"))
    # Cœurs : renaissent en bas, en continu (une seule salve auparavant).
    assert 'const rising = kind === "embers" || kind === "hearts";' in js


def test_national_day_shows_its_three_colours_at_once(tmp_path):
    store = FaceThemeStore(tmp_path / "face_theme.json")
    server = FaceServer(VisualState(), clock=lambda: datetime(2026, 7, 14, 12, 0), themes=store)
    payload = server.payload()
    assert payload["theme"] == "quatorzejuillet" and payload["palette"] == "tricolore"
    js = (ROOT / "jarvis" / "face" / "static" / "face.js").read_text(encoding="utf-8")
    assert "tricolore: {" in js and "data.palette" in js
    patrick = FaceServer(VisualState(), clock=lambda: datetime(2026, 3, 17, 12, 0), themes=store).payload()
    assert patrick["effect"] == "clovers"


# --- 1er avril, 1er mai, 4 mai, anniversaire de JARVIS ----------------------------------------------

@pytest.mark.parametrize("day, season", [((2027, 4, 1), "poissonavril"), ((2027, 5, 1), "muguet"),
                                         ((2027, 5, 4), "starwars"), ((2027, 10, 2), "naissancejarvis"),
                                         ((2026, 10, 2), None), ((2027, 6, 27), "anniversaire")])
def test_new_holidays(day, season):
    from datetime import date

    from jarvis.face.themes import parse_born, season_of

    assert season_of(date(*day), (6, 27), parse_born("2026-10-02")) == season


def test_jarvis_birthday_greeting_counts_the_years(tmp_path):
    from datetime import date

    server = FaceServer(VisualState(), clock=lambda: datetime(2028, 10, 2, 9, 0),
                        themes=FaceThemeStore(tmp_path / "t.json"), born=date(2026, 10, 2))
    assert server.payload()["greeting"] == "Joyeux anniversaire JARVIS : 2 ans"
    js = (ROOT / "jarvis" / "face" / "static" / "face.js").read_text(encoding="utf-8")
    assert all(effect in js for effect in ('"fish"', '"lily"', '"hyperspace"'))


# --- Échéance proche ---------------------------------------------------------------------------------

def test_upcoming_ring_for_events_timers_and_reminders():
    from datetime import timedelta

    from jarvis.agenda import CalendarEvent
    from jarvis.face.upcoming import Upcoming
    from jarvis.scheduling.manager import TimerManager

    now = datetime(2026, 10, 5, 14, 0)

    class Agenda:
        reads = 0

        def events(self, start, end):
            Agenda.reads += 1
            return [CalendarEvent("1", "Dentiste", now + timedelta(minutes=12), now + timedelta(minutes=42)),
                    CalendarEvent("2", "Congés", now, now + timedelta(days=1), all_day=True)]

    clock = {"t": 0.0}
    upcoming = Upcoming(Agenda(), clock=lambda: now, monotonic=lambda: clock["t"])
    assert upcoming.current() == {"label": "Dentiste", "kind": "event", "seconds": 720, "window": 900}
    for _ in range(50):
        upcoming.current()
    assert Agenda.reads == 1  # le calendrier n'est pas relu 30 fois par seconde
    timers = TimerManager(clock=lambda: now)
    timers.create_timer(300)
    both = Upcoming(Agenda(), timers, clock=lambda: now, monotonic=lambda: clock["t"]).current()
    assert both["kind"] == "timer" and both["seconds"] == 300 and both["window"] == 300
    assert Upcoming(None, TimerManager(clock=lambda: now), clock=lambda: now).current() is None


def test_upcoming_reaches_the_face_and_never_breaks_it():
    visual = VisualState()
    visual.upcoming = lambda: {"label": "Dentiste", "kind": "event", "seconds": 60, "window": 900}
    assert FaceServer(visual).payload()["upcoming"]["label"] == "Dentiste"
    visual.upcoming = lambda: 1 / 0
    assert FaceServer(visual).payload()["upcoming"] is None
    html = (ROOT / "jarvis" / "face" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="upcoming"' in html and 'id="upcoming-label"' in html
