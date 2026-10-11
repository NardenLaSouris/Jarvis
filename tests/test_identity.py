"""ORION est une femme : présentation, prompts du LLM et phrases toutes faites accordés au féminin ; elle vouvoie
l'utilisateur et l'appelle « monsieur », sans jamais prononcer son prénom."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.prompts import build_compact_prompt, build_system_prompt  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")
MASCULINE_SELF = re.compile(r"\b(?:je suis|je me suis|j'étais|je serai|je serais)\s+(?:vraiment\s+|très\s+)?"
                            r"(prêt|désolé|navré|ravi|heureux|attentif|réveillé|arrêté|sûr|certain|content|"
                            r"développé|occupé|connecté|un assistant)\b", re.IGNORECASE)


def test_identity_answers_are_feminine():
    for response in next(i for i in PERSONALITY.intents if i.name == "identity").responses:
        assert "assistante" in response and "assistant " not in response.replace("assistante", "")


def test_prompts_tell_the_llm_she_is_a_woman_and_keep_monsieur():
    full = build_system_prompt(PERSONALITY, CapabilityRegistry())
    compact = build_compact_prompt(PERSONALITY)
    for prompt in (full, compact):
        assert "assistante" in prompt and "féminin" in prompt and "« monsieur »" in prompt
    assert "Jules" not in full.split("Identité")[1][:400].replace("son prénom", "")


def test_no_masculine_first_person_in_canned_phrases():
    text = (ROOT / "personality.toml").read_text(encoding="utf-8")
    phrases = [line for line in text.splitlines() if not line.strip().startswith("#")
               and "filler" not in line and "heureux de vous" not in line and "ravi de vous" not in line]
    offending = [line.strip() for line in phrases if MASCULINE_SELF.search(line)]
    assert offending == []
