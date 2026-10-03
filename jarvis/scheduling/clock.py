"""Heure dite à voix haute -> (heure, minute) : « 7 heures », « 7h30 », « sept heures et demie »,
« six heures moins le quart », « midi », « 10 heures du soir ». Déterministe, sans LLM."""

from __future__ import annotations

from jarvis.scheduling.durations import _number, tokens

HOUR_WORDS = {"heure", "heures", "h"}


def _minutes_after(words: list[str], i: int) -> tuple[int, int]:
    """(minutes, mots consommés) après « N heures »."""
    if i < len(words) and words[i] == "et" and i + 1 < len(words):
        if words[i + 1] in ("demie", "demi"):
            return 30, 2
        if words[i + 1] == "quart":
            return 15, 2
    if i < len(words) and words[i] == "moins":
        if words[i + 1:i + 3] == ["le", "quart"]:
            return -15, 3
        parsed = _number(words, i + 1) if i + 1 < len(words) else None
        if parsed:
            return -parsed[0], 1 + parsed[1]
    parsed = _number(words, i) if i < len(words) else None
    if parsed and 0 <= parsed[0] <= 59:
        return parsed[0], parsed[1]
    return 0, 0


def parse_clock(text: str) -> tuple[int, int] | None:
    """Première heure dite dans ``text`` ; None s'il n'y en a pas."""
    words = tokens(text)
    for i, word in enumerate(words):
        if word in ("midi", "minuit"):
            hour = 12 if word == "midi" else 0
            minutes, _ = _minutes_after(words, i + 1)
            return (hour + (minutes // 60 if minutes < 0 else 0)) % 24, minutes % 60
        parsed = _number(words, i)
        if parsed is None or i + parsed[1] >= len(words) or words[i + parsed[1]] not in HOUR_WORDS:
            continue
        hour = parsed[0]
        if not 0 <= hour <= 24:
            continue
        rest = i + parsed[1] + 1
        minutes, used = _minutes_after(words, rest)
        after = words[rest + used:rest + used + 3]
        if hour < 12 and ("soir" in after or "apres" in after):
            hour += 12
        if minutes < 0:
            hour, minutes = hour - 1, 60 + minutes
        return hour % 24, minutes
    return None


def spoken_clock(hour: int, minute: int) -> str:
    return f"{hour} h {minute:02d}" if minute else f"{hour} heures" if hour != 1 else "1 heure"
