"""Confirmation d'une action par l'utilisateur, gérée par le Core.

La question est posée par ORION (formulée par l'outil, pas par le LLM) ; la réponse est comparée à
des listes fixes de « oui » et de « non ». Un « oui » suivi de la redite de la même demande (« oui, éteins mon PC
portable ») vaut oui ; un seul mot inconnu (« Yep ») fait reposer la question, une fois ; toute autre phrase annule la
demande en attente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from orion.personality import normalize
from orion.tools.base import ToolRequest

YES, NO, OTHER, NONE, UNCLEAR = "yes", "no", "other", "none", "unclear"
# « Oui, éteins mon PC portable » : un oui suivi de la redite de la demande. Ces mots signalent autre chose qu'une
# redite (une réserve, une seconde demande) : la confirmation n'est alors jamais déduite.
NOT_A_RESTATEMENT = {"et", "puis", "mais", "aussi", "plutot", "non", "pas", "ne", "sauf", "attends", "apres", "ensuite",
                     "si", "quand", "demain", "plus", "tard", "annule"}
SAME = {"pc": "ordinateur", "ordi": "ordinateur", "machine": "ordinateur"}  # mots de la demande -> mots de la question
FILLER = {"le", "la", "les", "l", "mon", "ma", "mes", "ton", "ta", "tes", "votre", "vos", "un", "une", "des", "du",
          "de", "d", "s", "il", "te", "plait", "vas", "y", "stp", "svp", "merci", "monsieur", "orion", "c", "est", "ca",
          "bon", "ok", "d", "accord", "que", "tu", "peux", "moi"}


@dataclass(frozen=True)
class Pending:
    request: ToolRequest
    question: str
    asked_at: float
    user: str = ""  # qui a demandé : seul lui peut confirmer
    origin: str = "user"  # origine de la demande, réévaluée à la confirmation
    asked_again: bool = False  # question déjà reposée après une réponse incomprise


class ConfirmationManager:
    def __init__(self, yes: Iterable[str], no: Iterable[str], ignored: Iterable[str] = (), ttl: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self._yes = {normalize(w) for w in yes}
        self._no = {normalize(w) for w in no}
        self._ignored = {normalize(w) for w in ignored}
        self._ttl = ttl
        self._clock = clock
        # Une confirmation en attente par canal (« voice », « message »...) : un « oui » ne valide que ce qui a été
        # demandé sur son propre canal (un « oui » dit à la maison ne confirme jamais une action demandée par message).
        self._pendings: dict[str, Pending] = {}
        self.channel = "voice"

    @property
    def pending(self) -> Pending | None:
        pending = self._pendings.get(self.channel)
        if pending is not None and self._clock() - pending.asked_at > self._ttl:
            del self._pendings[self.channel]
            return None
        return pending

    def ask(self, request: ToolRequest, question: str, user: str = "", origin: str = "user") -> str:
        self._pendings[self.channel] = Pending(request, question, self._clock(), user, origin)
        return question

    def answer(self, text: str) -> tuple[str, Pending | None]:
        pending = self.pending
        if pending is None:
            return NONE, None
        self._pendings.pop(self.channel, None)
        words = [w for w in normalize(text).split() if w not in self._ignored]
        said = " ".join(words)
        if said in self._yes or self._restated(words, pending):
            return YES, pending
        if said in self._no:
            return NO, pending
        if len(words) == 1 and not pending.asked_again:  # « Yep », « Carrément » : ni oui ni non reconnu
            self._pendings[self.channel] = replace(pending, asked_at=self._clock(), asked_again=True)
            return UNCLEAR, pending
        return OTHER, pending

    def _restated(self, words: list[str], pending: Pending) -> bool:
        """« Oui » (ou une autre réponse positive) puis la demande redite : chaque mot restant renvoie à la question
        posée ou à ses paramètres (« éteins mon PC portable » pour « … éteigne l'ordinateur sur votre PC portable »).
        Une autre action (« oui, ferme Chrome »), une réserve ou une seconde demande ne confirment jamais."""
        lead = next((n for n in range(len(words) - 1, 0, -1) if " ".join(words[:n]) in self._yes), 0)
        rest = [SAME.get(w, w) for w in words[lead:] if w not in FILLER]
        if not lead or not rest or any(w in NOT_A_RESTATEMENT or w in self._no for w in rest):
            return False
        reference = normalize(" ".join([pending.question, pending.request.tool.replace("_", " "),
                                        *(str(v) for v in pending.request.parameters.values())])).split()
        return all(any(w[:4] == r[:4] for r in reference if len(r) > 1) for w in rest)

    def clear(self) -> None:
        """Abandonne la confirmation en attente du canal en cours (pas celles des autres canaux)."""
        self._pendings.pop(self.channel, None)
