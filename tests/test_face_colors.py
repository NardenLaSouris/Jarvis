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
