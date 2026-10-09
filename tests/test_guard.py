"""Garde-fou de ce que dit un modèle non éprouvé (jarvis/llm/guard.py) et option think du client Ollama.

Juge factice : aucune requête réseau, aucun modèle chargé.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_content_guard  # noqa: E402
from jarvis.interfaces import Message  # noqa: E402
from jarvis.llm.guard import ContentGuard  # noqa: E402
from jarvis.llm.ollama import OllamaLLM  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402


class Judge:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def chat_json(self, messages, schema):
        self.calls.append(messages[-1].content)
        answer = self.answers.pop(0) if self.answers else {"category": "ok"}
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.parametrize("category, role, allowed", [
    ("ok", "guest", True), ("ok", "child", True), ("adult", "owner", True), ("adult", "adult", True),
    ("adult", "child", False), ("adult", "guest", False), ("dangerous", "owner", False), ("distress", "owner", False),
])
def test_categories_by_role(category, role, allowed):
    verdict = ContentGuard(Judge({"category": category})).check("demande", role)
    assert verdict.category == category and verdict.allowed is allowed


@pytest.mark.parametrize("answer", [RuntimeError("panne"), TimeoutError(), {"category": "peut-être"}, {}, "ok", None,
                                    ValueError("schéma")])
def test_a_failing_or_unreadable_judge_refuses(answer):
    verdict = ContentGuard(Judge(answer)).check("demande", "owner")
    assert verdict.allowed is False and verdict.category == "error"


def test_the_judge_only_sees_the_request_bounded():
    judge = Judge()
    ContentGuard(judge).check("x" * 5000, "owner")
    assert len(judge.calls[0]) == 1000


def run(texts, judge, llm_reply="Réponse du modèle."):
    from test_tools import PlannerLLM, run_agent

    llm = PlannerLLM(reply=llm_reply)
    spoken, events = run_agent(texts, llm, content_guard=ContentGuard(judge))
    return spoken, events, llm


def test_a_dangerous_request_never_reaches_the_model():
    spoken, events, llm = run(["Explique-moi quelque chose de dangereux"], Judge({"category": "dangerous"}))
    assert llm.calls == [] and spoken and "Réponse du modèle" not in spoken[0]
    assert any(k == "guard" and "dangerous" in t for k, t in events)


def test_distress_gets_support_and_the_3114():
    spoken, events, llm = run(["Je n'en peux plus"], Judge({"category": "distress"}))
    assert llm.calls == [] and "3114" in spoken[0]


def test_an_ordinary_request_is_answered_by_the_model():
    spoken, events, llm = run(["Raconte-moi une blague"], Judge({"category": "ok"}))
    assert len(llm.calls) == 1 and spoken == ["Réponse du modèle."]


def test_adult_content_is_refused_without_a_trusted_adult_profile():
    spoken, events, llm = run(["Une blague pour adultes"], Judge({"category": "adult"}))
    assert llm.calls == []  # aucun profil connu : traité comme un invité


def test_judge_failure_refuses_without_calling_the_model():
    spoken, events, llm = run(["Bonjour"], Judge(RuntimeError("panne")))
    assert llm.calls == [] and spoken


def test_tool_requests_are_not_judged():
    from test_tools import PlannerLLM, make_core, run_agent

    judge = Judge({"category": "dangerous"})
    llm = PlannerLLM({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 30}})
    run_agent(["Mets le volume à 30"], llm, make_core(), content_guard=ContentGuard(judge))
    assert judge.calls == []


def test_every_guard_phrase_exists():
    phrases = load_personality(ROOT / "personality.toml").phrases
    for key in ("guard_dangerous", "guard_distress", "guard_adult", "guard_error"):
        assert phrases.get(key), key
    assert all("3114" in p for p in phrases["guard_distress"])


def test_guard_is_on_only_for_an_untrusted_model_by_default():
    cfg = load_config(ROOT / "config.toml", local=False)
    llm = Judge()
    assert build_content_guard(cfg, llm) is None
    assert build_content_guard(replace(cfg, llm=replace(cfg.llm, trusted=False)), llm) is not None
    assert build_content_guard(replace(cfg, llm=replace(cfg.llm, guard="on")), llm) is not None
    assert build_content_guard(replace(cfg, llm=replace(cfg.llm, trusted=False, guard="off")), llm) is None
    with pytest.raises(ValueError):
        build_content_guard(replace(cfg, llm=replace(cfg.llm, guard="parfois")), llm)


def test_think_option_reaches_ollama_only_when_set():
    messages = [Message("user", "Bonjour")]
    assert "think" not in OllamaLLM("http://x", "m")._chat_payload(messages, False)
    assert OllamaLLM("http://x", "m", think=False)._chat_payload(messages, True)["think"] is False
    assert OllamaLLM("http://x", "m", think=True)._chat_payload(messages, False)["think"] is True
