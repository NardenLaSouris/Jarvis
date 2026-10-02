"""Durées en français : « 10 minutes », « 1 heure 30 », « une demi-heure », « 1h30 » -> secondes.

L'analyse est déterministe et séparée de l'exécution : le LLM rapporte la durée telle qu'elle a
été dite, ce module la convertit (ou la refuse).
"""

from __future__ import annotations

import re

from jarvis.personality import normalize

UNITS = {
    "seconde": 1, "secondes": 1, "sec": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mn": 60,
    "heure": 3600, "heures": 3600, "h": 3600,
    "second": 1, "seconds": 1, "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
}
SMALL = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
}
TENS = {"vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60}
FILLER = {"dans", "de", "d", "pendant", "pour", "et", "environ", "minuteur", "timer", "un", "une"}
SPECIAL = (
    ("trois quarts d heure", 2700), ("trois quart d heure", 2700), ("quart d heure", 900),
    ("demi heure", 1800), ("demie heure", 1800), ("demi minute", 30),
)


class DurationError(ValueError):
    pass


def tokens(text: str) -> list[str]:
    """Mots normalisés, chiffres séparés des lettres (« 1h30 » -> 1 h 30)."""
    text = re.sub(r"(\d)([a-z])", r"\1 \2", normalize(text))
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    return text.split()


def _number(words: list[str], i: int) -> tuple[int, int] | None:
    """Nombre écrit en chiffres ou en lettres à la position ``i`` : (valeur, mots consommés)."""
    word = words[i]
    if word.isdigit():
        return int(word), 1
    if word in TENS:
        value, used = TENS[word], 1
        if i + 1 < len(words) and words[i + 1] == "et" and i + 2 < len(words) and words[i + 2] in ("un", "une"):
            return value + 1, 3
        if i + 1 < len(words) and words[i + 1] in SMALL and 1 <= SMALL[words[i + 1]] <= 9:
            return value + SMALL[words[i + 1]], 2
        return value, used
    if word == "dix" and i + 1 < len(words) and words[i + 1] in ("sept", "huit", "neuf"):
        return 10 + SMALL[words[i + 1]], 2
    if word in SMALL:
        return SMALL[word], 1
    return None


def parse_duration(text: str) -> int:
    """Durée dite -> secondes (> 0). Lève DurationError si la phrase n'est pas une durée claire."""
    if re.search(r"-\s*\d", text):
        raise DurationError(f"durée négative : « {text} »")
    joined = " ".join(tokens(text))
    total = 0
    for phrase, seconds in SPECIAL:
        match = re.search(rf"(?:\b(\w+) )?\b{phrase}\b", joined)
        if not match:
            continue
        prefix = match.group(1) or ""
        number = _number([prefix], 0) if prefix and prefix not in ("un", "une") else None
        count = number[0] if number else 1
        keep = "" if number else prefix
        joined = f"{joined[:match.start()]} {keep} {joined[match.end():]}"
        total += count * seconds
    words = joined.split()
    i, last_unit, found = 0, None, total > 0
    while i < len(words):
        parsed = _number(words, i)
        if parsed is None:
            if words[i] in ("demi", "demie", "quart") and last_unit:
                total += last_unit // (2 if words[i] != "quart" else 4)
                i += 1
                continue
            if words[i] in FILLER:
                i += 1
                continue
            raise DurationError(f"durée incomprise : « {text} »")
        value, used = parsed
        i += used
        if i < len(words) and words[i] in UNITS:
            last_unit = UNITS[words[i]]
            total += value * last_unit
            i += 1
            found = True
        elif last_unit in (3600, 60) and i >= len(words):
            total += value * (last_unit // 60)
            found = True
        elif words[i - used] in ("un", "une") and used == 1:
            continue
        else:
            raise DurationError(f"durée incomprise : « {text} »")
    if not found or total <= 0:
        raise DurationError(f"durée incomprise : « {text} »")
    return total


def duration_in_text(value: str, text: str) -> bool:
    """La durée ``value`` (proposée par le LLM) figure-t-elle, sous une forme ou une autre, dans ``text`` ?"""
    try:
        target = parse_duration(value)
    except DurationError:
        return False
    words = tokens(text)
    for start in range(len(words)):
        for end in range(start + 1, min(len(words), start + 8) + 1):
            try:
                if parse_duration(" ".join(words[start:end])) == target:
                    return True
            except DurationError:
                continue
    return False


def spoken_remaining(seconds: float) -> str:
    """Temps restant dit naturellement : à la minute près au-delà de deux minutes (« 59 minutes 58 secondes »
    devient « 1 heure »)."""
    if seconds >= 120:
        seconds = round(seconds / 60) * 60
    return spoken_duration(seconds)


def spoken_duration(seconds: int) -> str:
    """Secondes -> durée lisible à voix haute (« 1 heure 30 minutes », « 45 secondes »)."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} heure{'s' if hours > 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} seconde{'s' if secs > 1 else ''}")
    return " ".join(parts)
