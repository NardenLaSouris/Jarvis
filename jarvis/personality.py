"""Personnalité de l'assistant : identité, ton, phrases et intentions prédéfinies (personality.toml)."""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre")


def spoken_date(now: datetime) -> str:
    day = "1er" if now.day == 1 else str(now.day)
    return f"{WEEKDAYS[now.weekday()]} {day} {MONTHS[now.month - 1]}"


def spoken_time(now: datetime) -> str:
    hours = {0: "minuit", 12: "midi"}.get(now.hour, f"{now.hour} heure{'s' if now.hour > 1 else ''}")
    return f"{hours} {now.minute}" if now.minute else hours


@dataclass(frozen=True)
class Intent:
    name: str
    patterns: tuple[str, ...]
    responses: tuple[str, ...]
    match: str = "contains"
    critical: bool = False
    fallback: bool = False
    end_conversation: bool = False
    groups: tuple[tuple[str, ...], ...] = ()
    after: tuple[str, ...] = ()
    web: bool = False
    replaces: tuple[str, ...] = ()
    tool: str = ""
    planner: bool = False


@dataclass(frozen=True)
class Personality:
    assistant_name: str = "JARVIS"
    user_name: str = ""
    user_title: str = "monsieur"
    language: str = "fr"
    tone: str = ""
    humor: bool = True
    evening_starts_at: int = 18
    style_examples: tuple[str, ...] = ()
    phrases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_tools: str = ""
    filler_openings: frozenset[str] = frozenset()
    greeting_openings: frozenset[str] = frozenset()
    filler_closings: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    rewrites: tuple[tuple[str, str], ...] = ()
    fuzzy_threshold: float = 0.0
    strict_words: frozenset[str] = frozenset()
    intents: tuple[Intent, ...] = ()
    max_sentences: int = 0
    detail_words: tuple[str, ...] = ()
    confirm_yes: tuple[str, ...] = ()
    confirm_no: tuple[str, ...] = ()

    def without_user_name(self, text: str) -> str:
        """Retire le prénom de l'utilisateur d'une réponse : JARVIS ne le prononce jamais."""
        if not self.user_name:
            return text
        text = re.sub(rf"[,\s]*\b{re.escape(self.user_name)}\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^[\s,]+", "", text)
        if not any(c.isalnum() for c in text):
            return ""
        return text[:1].upper() + text[1:]

    def wants_details(self, user_text: str) -> bool:
        words = f" {normalize(user_text)} "
        return any(f" {w} " in words for w in self.detail_words)

    def canonical(self, text: str) -> str:
        words = f" {normalize(text)} "
        for source, target in self.rewrites:
            words = words.replace(f" {source} ", f" {target} ")
        return " ".join(words.split())

    def render(self, template: str, now: datetime | None = None, **extra: str) -> str:
        now = now or datetime.now()
        hour = now.hour
        values = {
            "assistant_name": self.assistant_name,
            "title": self.user_title,
            "Title": self.user_title[:1].upper() + self.user_title[1:],
            "user_name": self.user_name,
            "salutation": "Bonsoir" if hour >= self.evening_starts_at or hour < 5 else "Bonjour",
            "date": spoken_date(now),
            "time": spoken_time(now),
            "available": self.no_tools,
            "planned": "",
            **extra,
        }
        return template.format(**values)

    def strip_ignored(self, normalized: str) -> str:
        words = f" {normalized} "
        for phrase in sorted(self.ignored, key=len, reverse=True):
            words = words.replace(f" {phrase} ", " ")
        return " ".join(words.split())


def load_personality(path: str | Path) -> Personality:
    with Path(path).open("rb") as fh:
        raw = tomllib.load(fh)
    p = raw.get("personality", {})
    phrases = raw.get("phrases", {})
    matching = raw.get("matching", {})
    rewrites = tuple(sorted(((normalize(a), normalize(b)) for a, b in matching.get("rewrites", {}).items()),
                            key=lambda pair: -len(pair[0])))

    def canonical(text: str) -> str:
        words = f" {normalize(text)} "
        for source, target in rewrites:
            words = words.replace(f" {source} ", f" {target} ")
        return " ".join(words.split())

    names = {"user_name": p.get("user_name", ""), "assistant_name": p.get("assistant_name", "JARVIS")}
    ignored = tuple(normalize(w) for w in matching.get("ignored", []))
    ignored += (normalize(p.get("assistant_name", "JARVIS")), normalize(p.get("user_title", "monsieur")))
    intents = tuple(
        Intent(
            name=name,
            patterns=tuple(canonical(x.format(**names)) for x in spec.get("patterns", [])),
            responses=tuple(spec.get("responses", [])),
            match=spec.get("match", "contains"),
            critical=spec.get("critical", False),
            fallback=spec.get("fallback", False),
            groups=tuple(tuple(normalize(w) for w in group) for group in spec.get("groups", [])),
            end_conversation=spec.get("end_conversation", False),
            after=tuple(spec.get("after", [])),
            web=spec.get("web", False),
            replaces=tuple(spec.get("replaces", [])),
            tool=spec.get("tool", ""),
            planner=spec.get("planner", False),
        )
        for name, spec in raw.get("intents", {}).items()
    )
    return Personality(
        assistant_name=p.get("assistant_name", "JARVIS"),
        user_name=p.get("user_name", ""),
        user_title=p.get("user_title", "monsieur"),
        language=p.get("language", "fr"),
        tone=p.get("tone", ""),
        humor=p.get("humor", True),
        evening_starts_at=int(p.get("evening_starts_at", 18)),
        style_examples=tuple(p.get("style_examples", [])),
        phrases={k: tuple(v) for k, v in phrases.items() if isinstance(v, list)},
        no_tools=phrases.get("no_tools", ""),
        filler_openings=frozenset(normalize(f) for f in phrases.get("filler_openings", [])),
        greeting_openings=frozenset(normalize(f) for f in phrases.get("greeting_openings", [])),
        filler_closings=tuple(normalize(f) for f in phrases.get("filler_closings", [])),
        ignored=ignored,
        rewrites=rewrites,
        fuzzy_threshold=float(matching.get("fuzzy_threshold", 0.0)),
        strict_words=frozenset(normalize(w) for w in matching.get("strict_words", [])),
        intents=intents,
        max_sentences=int(p.get("max_sentences", 0)),
        detail_words=tuple(normalize(w) for w in p.get("detail_words", [])),
        confirm_yes=tuple(raw.get("confirmation", {}).get("yes", [])),
        confirm_no=tuple(raw.get("confirmation", {}).get("no", [])),
    )
