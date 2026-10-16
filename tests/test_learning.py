"""Ce qu'ORION apprend (orion/learning.py) : vocabulaire réécrit et donné à Whisper, mot de réveil durci après un
faux réveil signalé, jamais de « oui » ou de « non » détourné."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orion.learning import (  # noqa: E402
    SILENT_LIMIT, WAKE_DECAY, WAKE_MAX_RAISE, WAKE_STEP, Learning, learning_tools,
)
from orion.tools import PermissionManager, ToolCore, ToolError, ToolRegistry  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def make(tmp_path, clock=None):
    return Learning(tmp_path / "learned.json", protected=("oui", "ouais", "non", "non merci", "vas y"),
                    names=("ORION", "Jarvis"), clock=clock or Clock())


def test_learned_expression_is_rewritten_whatever_the_accents(tmp_path):
    learning = make(tmp_path)
    learning.learn("la lampe du fond", "la chambre")
    assert learning.rewrite("Allume La Lampe du Fond en bleu") == "Allume la chambre en bleu"
    assert learning.rewrite("Éteins la lampe du fondue") == "Éteins la lampe du fondue"  # mots entiers seulement
    learning.learn("télé", "Freebox")
    assert learning.rewrite("Allume la tele") == "Allume la Freebox"


def test_words_reach_whisper_and_survive_a_restart(tmp_path):
    learning = make(tmp_path)
    changes = []
    learning.on_change = lambda: changes.append(learning.hotwords())
    learning.learn("Katana")
    learning.learn("la lampe du fond", "la chambre")
    assert changes[-1] == "Katana, la chambre"
    again = make(tmp_path)
    assert again.hotwords() == "Katana, la chambre" and again.rewrite("la lampe du fond") == "la chambre"


def test_yes_no_and_names_can_never_be_learned(tmp_path):
    learning = make(tmp_path)
    for heard, meaning in (("non", "ok"), ("bof", "oui"), ("ouais", "plus tard"), ("Orion", "la radio"),
                           ("éteins", "non merci"), ("ok", "")):
        with pytest.raises(ToolError):
            learning.learn(heard, meaning)
    assert learning.vocabulary() == {"rewrites": {}, "words": []}


def test_forget_by_expression_or_by_its_meaning(tmp_path):
    learning = make(tmp_path)
    learning.learn("la lampe du fond", "la chambre")
    learning.forget("la chambre")  # « oublie la lampe du fond », déjà réécrit
    assert learning.rewrite("la lampe du fond") == "la lampe du fond" and learning.hotwords() == ""
    with pytest.raises(ToolError):
        learning.forget("inconnu")


def test_false_wake_hardens_the_wake_word(tmp_path):
    clock = Clock()
    learning = make(tmp_path, clock)
    assert learning.wake_threshold(0.6) == 0.6
    first = learning.false_wake({"heard": "On prie. J'arrête.", "sure": True})
    assert first == {"raise": WAKE_STEP, "rule": "phrase"}
    assert learning.wake_threshold(0.6) == round(0.6 + WAKE_STEP, 3)
    assert learning.blocked("on prie, j'arrête") and not learning.blocked("Orion")
    assert learning.always_verify  # le réveil franc accepté sans vérification la passera désormais
    for _ in range(10):
        learning.false_wake({"heard": "Orion"})  # un seul mot : jamais refusé (ce serait aussi le vrai nom)
    assert learning.wake_threshold(0.6) == round(0.6 + WAKE_MAX_RAISE, 3) and not learning.blocked("Orion")
    clock.now += 3 * 86400  # trois jours sans faux réveil : le seuil redescend
    assert learning.wake_threshold(0.6) == round(0.6 + WAKE_MAX_RAISE - 3 * WAKE_DECAY, 3)


def test_repeated_silent_wakes_raise_the_threshold_slightly(tmp_path):
    learning = make(tmp_path)
    for _ in range(SILENT_LIMIT - 1):
        learning.silent_wake()
    assert learning.wake_threshold(0.6) == 0.6
    learning.silent_wake()
    assert learning.wake_threshold(0.6) > 0.6


def test_tools_teach_and_list(tmp_path):
    learning = make(tmp_path)
    core = ToolCore(ToolRegistry(), PermissionManager())
    for tool in learning_tools(learning):
        core.registry.register(tool)
    taught = core.submit({"type": "tool_call", "tool": "learn_word",
                          "parameters": {"heard": "la lampe du fond", "meaning": "la chambre"}})
    assert taught.result.success and "la chambre" in core.registry.get("learn_word").say(taught.result.result)
    listed = core.submit({"type": "tool_call", "tool": "list_learned", "parameters": {}})
    assert "« la lampe du fond » veut dire « la chambre »" in core.registry.get("list_learned").say(listed.result.result)


def test_false_wake_phrases_are_recognized():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_personality import router

    r = router()
    for text in ("C'était pas pour toi", "Je t'ai pas appelé", "Fausse alerte", "Orion, ce n'était pas pour toi."):
        assert r.route(text).name == "false_wake", text


def test_agent_rewrites_learned_words_and_hardens_after_a_false_wake(tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_tools import SLEEP, PlannerLLM, run_agent

    learning = make(tmp_path)
    learning.learn("la lampe du fond", "le salon")
    spoken, events = run_agent(["Raconte une blague sur la lampe du fond", SLEEP, "C'était pas pour toi"],
                               PlannerLLM(reply="Voici une blague."), learning=learning)
    assert any(k == "correction" and "le salon" in t and "vocabulaire appris" in t for k, t in events)
    assert any(k == "learning" and "Faux réveil" in t for k, t in events)
    assert learning.wake_threshold(0.5) == round(0.5 + WAKE_STEP, 3)
