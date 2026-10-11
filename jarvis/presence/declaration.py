"""« ORION, je sors. » : déclaration explicite de départ, reconnue sans LLM et appliquée par le Core
(outil ``declare_departure``, jarvis/presence/tools.py ; état du moteur de présence, jarvis/presence/engine.py).

Seule une déclaration à la première personne, au présent, compte :
- départ maintenant : « je sors », « je pars de la maison », « je m'en vais », « je quitte la maison pour un
  moment », « je pars faire des courses » ;
- départ plus tard : « je sors dans une heure », « je pars à 18 h » (l'heure dite, jamais tout de suite) ;
- sortie courte, sans changement : « je sors deux minutes », « je sors les poubelles », « je descends le courrier » ;
- retour sur l'annonce : « finalement je reste », « je ne sors plus ».
Jamais : une question (« je sors ce soir ? »), une condition ou une demande (« appelle-moi quand je sors », « si je
pars »), un autre sens (« je sors de la douche », « je pars du principe »), le passé ou une autre personne.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from jarvis.personality import normalize
from jarvis.scheduling.durations import _number, tokens
from jarvis.scheduling.when import parse_when

NOW, LATER, SHORT, STAY = "now", "later", "short", "stay"
SHORT_MAX_MINUTES = 15  # « je sors 10 minutes » : pas une absence

LEAVE = re.compile(r"\b(?:je sors|je vais sortir|je pars|je vais partir|je m en vais|je vais y aller|j y vais|"
                   r"je quitte (?:la maison|l appart\w*|chez moi|le domicile)|je m absente|je file|je decolle|"
                   r"je me sauve|je vais faire (?:des|les|mes) courses|je pars faire|je sors faire)\b")
STAY_END = re.compile(r"\bje reste(?: a la maison| ici| chez moi)?\s*$")  # « (finalement) je reste »
STAY_WORDS = re.compile(r"\b(?:finalement je reste|je ne sors (?:pas|plus)|je sors (?:pas|plus)|je ne pars (?:pas|plus)|"
                        r"je pars (?:pas|plus)|j annule ma sortie|je ne m en vais (?:pas|plus)|"
                        r"je m en vais (?:pas|plus))\b")
CONDITION = re.compile(r"\b(?:si|quand|lorsque|lorsqu|des que|avant que|au cas ou|appelle|appelez|rappelle|"
                       r"rappelez|previens|previent|preveniez|dis moi|est ce que|faut il|dois je|devrais je|"
                       r"pourquoi|comment)\b")
# « je sors de la douche », « je pars du principe » : un autre sens.
OTHER_SENSE = re.compile(r"\b(?:je sors (?:de|du|des|d) (?!la maison|chez moi|l appart)|je pars (?:du|d un) principe|"
                         r"je pars (?:de|d) (?!la maison|chez moi|l appart)\w+ (?:pour|que)|je sors avec|je sors ensemble|"
                         r"je m en vais (?:te|vous|le|la|les|lui) )")
SHORT_WORDS = re.compile(r"\b(?:quelques minutes|deux secondes|une seconde|un instant|une minute|deux minutes|"
                         r"cinq minutes|vite fait|en vitesse|juste (?:deux|cinq|une|un)|les poubelles|la poubelle|"
                         r"le courrier|la boite aux lettres|un colis|le colis|fumer|une clope|une cigarette|"
                         r"sur le balcon|dans le jardin|sur la terrasse|je reviens tout de suite|je reviens vite|"
                         r"je reviens dans (?:deux|cinq|une|1|2|5|10) (?:minutes?|secondes?))\b")
REPORTED = {"que", "qu", "dit", "dis", "crois", "pense", "pensais", "sais", "savais", "croit", "pensait"}
OBJECT = re.compile(r"\bje sors (?:le|la|les|un|une) (?!maison)\w+")  # « je sors le linge » : rien à voir


@dataclass(frozen=True)
class Declaration:
    kind: str  # NOW, LATER, SHORT, STAY
    at: datetime | None = None  # LATER : heure du départ annoncé
    said: str = ""


def _short_duration(norm: str) -> bool:
    """« je sors pour 10 minutes », « je sors 5 minutes » : pas plus de SHORT_MAX_MINUTES."""
    words = tokens(norm)
    for i, word in enumerate(words):
        if word not in ("minute", "minutes", "min", "secondes", "seconde") or i == 0:
            continue
        for start in (i - 1, i - 2):
            parsed = _number(words, start) if start >= 0 else None
            if parsed is not None and start + parsed[1] == i:
                minutes = parsed[0] if word.startswith("min") else parsed[0] / 60
                return minutes <= SHORT_MAX_MINUTES
    return False


def parse_declaration(text: str, now: datetime) -> Declaration | None:
    """Déclaration de départ dans ``text``, ou None si la phrase n'en est pas une."""
    norm = f" {normalize(text)} "
    if text.strip().endswith("?") or CONDITION.search(norm):
        return None
    if STAY_WORDS.search(norm) or STAY_END.search(norm):
        return Declaration(STAY, said="reste")
    match = LEAVE.search(norm)
    if match is None or OTHER_SENSE.search(norm):
        return None
    if set(norm[:match.start()].split()) & REPORTED:
        return None  # « il dit que je sors », « tu crois que je pars » : pas une déclaration
    if SHORT_WORDS.search(norm) or OBJECT.search(norm) or _short_duration(norm):
        return Declaration(SHORT, said="courte")
    when = parse_when(norm, now)
    if when is not None and (when.at - now).total_seconds() > 120:
        return Declaration(LATER, when.at, when.said)
    return Declaration(NOW, said="maintenant")
