"""Routeur d'intentions : décide qui répond à une demande transcrite.

Ordre de priorité :
  1. commandes critiques (arrêt, silence...) ;
  2. intentions prédéfinies de la personnalité (salutations, identité...) ; celles liées à un outil
     (heure, date) passent par cet outil quand les outils sont activés ;
  3. capacités enregistrées ;
  4. intentions de repli (demande d'action vers une fonction pas encore disponible) ;
  5. demandes d'action (intention tool.action, si les outils sont activés) : le LLM choisit l'outil ;
  6. recherche Web (intention web.search, si elle est activée) : informations actuelles ;
  7. LLM généraliste.
Les outils, la recherche Web et le LLM généraliste sollicitent le LLM.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Callable

from jarvis.capabilities import CapabilityRegistry
from jarvis.personality import Intent, Personality, normalize
from jarvis.prompts import build_compact_prompt, build_system_prompt
from jarvis.streaming import split_sentences

log = logging.getLogger(__name__)

# Balises réservées aux données d'outil ou Web : une réponse qui les reproduit les invente.
MARKUP = re.compile(r"<<<|>>>|RESULTAT_?OUTIL|DONNEES_WEB", re.IGNORECASE)

ACTION_VERBS = (
    "eteint|allume|ouvert|ferme|lance|envoye|programme|regle|active|desactive|baisse|monte|augmente|"
    "diminue|mis|joue|appele|supprime|cree|enregistre|demarre|arrete|coupe|verrouille|commande|reserve|ajoute"
)
ACTION_CLAIMS = (
    re.compile(rf"\bj ai (bien |deja )?({ACTION_VERBS})\b"),
    re.compile(r"\bje viens d (eteindre|allumer|ouvrir|fermer|lancer|envoyer|programmer|regler|activer|couper)\b"),
    re.compile(r"\b(c est|voila c est|voila qui est) fait\b"),
    re.compile(r"\b(est|sont) (maintenant |desormais |bien )?(eteinte?s?|allumee?s?|ouverte?s?|fermee?s?|"
               r"lancee?s?|envoyee?s?|activee?s?|desactivee?s?|programmee?s?|reglee?s?|coupee?s?|montee?s?|baissee?s?|"
               r"augmentee?s?|diminuee?s?|verrouillee?s?|remise?s?|annulee?s?|mise?s?|ajustee?s?|reduite?s?|"
               r"modifiee?s?|changee?s?|passee?s?|tamisee?s?|configuree?s?|definie?s?)\b"),
    re.compile(r"\b(message|mail|e mail|sms|musique|lumiere) (est )?(bien )?(envoye|lancee?|allumee|eteinte)\b"),
    re.compile(r"\bje m en occupe\b"),
    re.compile(r"\bj ai (bien )?(pris note|note|pris en compte|transmis)\b"),
    re.compile(r"\bje vous (rappellerai|recontacterai|previendrai|tiendrai (au courant|informe))\b"),
    re.compile(r"\bje (peux|pourrai|vais pouvoir) (vous )?(programmer|allumer|eteindre|envoyer|lancer|ouvrir|"
               r"regler|mettre|rappeler|reveiller|noter|enregistrer)\b"),
    re.compile(r"\bje (vais|m en vais) (vous )?(allumer|eteindre|envoyer|regler|mettre|lancer|ouvrir|fermer|baisser|"
               r"monter|augmenter|verrouiller|reveiller|rappeler|programmer|couper|supprimer|redemarrer|jouer|"
               r"effacer|annuler|creer|ajouter)\b"),
    # Session QA : « Je vais verrouiller l'ordinateur », « Je vous réveillerai à 25 heures », « J'ouvre Without
    # Me » sans aucun outil.
    re.compile(r"\bj (ouvre|allume|eteins|arrete|active|augmente|ajoute|annule|efface|enregistre|envoie)\b"),
    re.compile(r"\bje (lance|mets|joue|demarre|coupe|baisse|monte|programme|regle|passe a|passe au)\b"),
    re.compile(r"\bje vous (reveillerai|reveille)\b"),
    re.compile(r"\bje (verrouille|eteins|allume|ferme|ouvre|lance|coupe|redemarre|supprime|efface) (l|le|la|les|votre|"
               r"vos|ton|ta|tes)\b"),
    re.compile(r"\b(sera|seront|va etre|vont etre) (mise?s?|allumee?s?|eteinte?s?|reglee?s?|envoyee?s?|lancee?s?|"
               r"ouverte?s?|fermee?s?|programmee?s?)\b"),
    re.compile(r"\bvous serez (bien )?(rappelee?s?|prevenue?s?|avertie?s?|notifiee?s?)\b"),
    re.compile(r"\b(c est|bien) note\b"),
    re.compile(r"\bje vous rappelle (dans|d ici)\b"),
    re.compile(r"\b(minuteur|rappel|alarme|timer|reveil) (est |a ete )?(bien )?(lance|programme|regle|cree|enregistre|"
               r"active|mis en place)e?\b"),
)
NEGATION = re.compile(r"\b(ne|n|pas|jamais|impossible|aucun|aucune|plus)\b")


FUZZY_MAX_WORDS = 6


@dataclass(frozen=True)
class Route:
    source: str
    name: str = ""
    reply: str | None = None
    end_conversation: bool = False
    tool: str = ""

    @property
    def label(self) -> str:
        return f"{self.source}:{self.name}" if self.name else self.source


def claims_action(reply: str) -> bool:
    for sentence in re.split(r"[.!?;]+", reply):
        text = normalize(sentence)
        if any(p.search(text) for p in ACTION_CLAIMS) and not NEGATION.search(text):
            return True
    return False


class IntentRouter:
    def __init__(
        self,
        personality: Personality,
        capabilities: CapabilityRegistry,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = datetime.now,
        web_enabled: bool = False,
        tools: tuple[str, ...] = (),
        memory: Callable[[], str] | None = None,
    ):
        self.personality = personality
        self.capabilities = capabilities
        self._rng = rng or random.Random()
        self._clock = clock
        self._last: dict[str, str] = {}
        self._previous: str | None = None
        self._tools = set(tools)
        self.memory = memory
        web = [i for i in personality.intents if i.web] if web_enabled else []
        planners = [i for i in personality.intents if i.planner] if self._tools else []
        by_web = {name for i in web for name in i.replaces}
        by_tools = by_web | {name for i in planners for name in i.replaces}
        ordered = [i for i in personality.intents if i.critical] + [
            i for i in personality.intents if not i.critical and not i.fallback and not i.web and not i.planner]
        patterns = lambda i: {personality.strip_ignored(p) for p in i.patterns} - {""}  # noqa: E731
        self._intents = [(i, patterns(i)) for i in ordered]
        self._planners = [(i, patterns(i)) for i in planners]
        self._web = [(i, patterns(i)) for i in web]
        fallbacks = [i for i in personality.intents if i.fallback]
        self._fallbacks = [(i, set()) for i in fallbacks if i.name not in by_tools]
        self._fallbacks_without_tools = [(i, set()) for i in fallbacks if i.name not in by_web]
        self._fallbacks_after_tools = [(i, set()) for i in fallbacks if i.name in by_tools and i.name not in by_web]

    def start_conversation(self) -> None:
        self._previous = None

    def route(self, text: str, tools: bool = True) -> Route:
        """``tools=False`` : même routage sans les demandes d'action (quand aucun outil ne convient)."""
        route = self._route(text, tools)
        self._previous = route.name or route.source
        return route

    def _route(self, text: str, tools: bool = True) -> Route:
        norm = self.personality.canonical(text)
        core = self.personality.strip_ignored(norm)
        for intent, exact in self._intents:
            if intent.tool and intent.tool not in self._tools and not intent.responses:
                continue
            if intent.needs and intent.needs not in self._tools:
                continue
            if self._matches(intent, exact, norm, core):
                if intent.tool and intent.tool in self._tools:
                    return Route("tool", intent.name, tool=intent.tool)
                source = "critical" if intent.critical else "predefined"
                return Route(source, intent.name, self._pick(intent.name, intent.responses), intent.end_conversation)
        handled = self.capabilities.handle(text)
        if handled:
            return Route("capability", handled[0], handled[1])
        for intent, exact in self._fallbacks if tools else self._fallbacks_without_tools:
            if self._matches(intent, exact, norm, core):
                return Route("unavailable", intent.name, self._pick(intent.name, intent.responses))
        for intent, _ in self._web:
            if any(f" {phrase} " in f" {norm} " for phrase in intent.explicit):
                return Route(intent.name)
        for intent, exact in self._planners if tools else ():
            if self._matches(intent, exact, norm, core):
                return Route("tool", intent.name)
        for intent, exact in self._fallbacks_after_tools if tools else ():
            if self._matches(intent, exact, norm, core):
                return Route("unavailable", intent.name, self._pick(intent.name, intent.responses))
        for intent, exact in self._web:
            if self._matches(intent, exact, norm, core):
                return Route(intent.name)
        return Route("llm")

    def _matches(self, intent: Intent, exact: set[str], norm: str, core: str) -> bool:
        if intent.after and self._previous not in intent.after:
            return False
        if intent.match == "keywords":
            return bool(intent.groups) and all(any(f" {w} " in f" {norm} " for w in g) for g in intent.groups)
        if core in exact:
            return True
        if intent.match == "contains" and any(f" {p} " in f" {norm} " for p in intent.patterns):
            return True
        return not intent.critical and any(self._close(core, p) for p in exact)

    def _close(self, said: str, expected: str) -> bool:
        threshold = self.personality.fuzzy_threshold
        a, b = said.split(), expected.split()
        if not threshold or len(a) != len(b) or len(a) > FUZZY_MAX_WORDS:
            return False
        for x, y in zip(a, b, strict=True):
            if x == y:
                continue
            if x in self.personality.strict_words or y in self.personality.strict_words or min(len(x), len(y)) < 4:
                return False
            if SequenceMatcher(None, x, y).ratio() < threshold:
                return False
        return True

    def phrase(self, key: str) -> str:
        return self._pick(key, self.personality.phrases.get(key, ()))

    def wake_phrases(self) -> tuple[str, ...]:
        return tuple(self._render(t) for t in self.personality.phrases.get("wake", ()))

    def system_prompt(self, ongoing: bool = False) -> str:
        return build_system_prompt(self.personality, self.capabilities, self._clock(), ongoing,
                                   self.memory() if self.memory else "")

    def compact_prompt(self) -> str:
        """Prompt court du LLM de secours (mode dégradé)."""
        return build_compact_prompt(self.personality, self._clock())

    def reply_filter(self, user_text: str = "", tools_used: bool = False, fallback: str | None = None) -> "ReplyFilter":
        return ReplyFilter(self, user_text, tools_used, fallback)

    def check_reply(self, reply: str, user_text: str = "", tools_used: bool = False) -> str:
        if not tools_used and claims_action(reply):
            log.warning("Réponse du LLM écartée (action prétendue sans outil) : %s", reply)
            return self.phrase("action_unavailable")
        reply_filter = self.reply_filter(user_text, tools_used)
        kept = [s for s in map(reply_filter.accept, split_sentences(self._trim_incomplete_ending(reply))) if s]
        return " ".join(kept + reply_filter.finish())

    def _is_filler_closing(self, sentence: str) -> bool:
        return any(f" {c} " in f" {normalize(sentence)} " for c in self.personality.filler_closings)

    def _limit_title(self, sentence: str, allowed: int) -> tuple[str, int]:
        title = self.personality.user_title
        occurrences = list(re.finditer(rf"(,\s*)?\b{re.escape(title)}\b", sentence, re.IGNORECASE))
        for match in reversed(occurrences[allowed:]):
            sentence = sentence[: match.start()] + sentence[match.end():]
        sentence = re.sub(r"\s+([,.])", r"", sentence)
        sentence = re.sub(r" {2,}", " ", sentence)
        sentence = re.sub(r",([.!?])", r"", sentence)
        capitalized = title[:1].upper() + title[1:]
        sentence = re.sub(rf"(?<=[,;:] ){re.escape(capitalized)}\b", title, sentence).strip()
        return sentence, min(len(occurrences), allowed)

    def _strip_opening(self, sentence: str, keep_greeting: bool = False) -> str:
        """Retire les formules d'entrée creuses ; peut rendre une chaîne vide."""
        openings = self.personality.filler_openings | (frozenset() if keep_greeting else self.personality.greeting_openings)
        fillers = {self.personality.strip_ignored(f) for f in openings}
        position, found = 0, False
        for segment in re.finditer(r"[^,.!?;:]*[,.!?;:]+\s*", sentence):
            content = self.personality.strip_ignored(normalize(segment.group()))
            if content in fillers:
                found = True
            elif content:
                break
            position = segment.end()
        if not found:
            return sentence
        rest = sentence[position:].strip()
        return rest[:1].upper() + rest[1:]

    @staticmethod
    def _trim_incomplete_ending(reply: str) -> str:
        stripped = reply.rstrip()
        if not stripped or stripped[-1] in ".!?…»\"')":
            return reply
        last = max(stripped.rfind(c) for c in ".!?…")
        return stripped[: last + 1] if last > 0 else reply

    def _pick(self, key: str, variants: tuple[str, ...]) -> str:
        if not variants:
            return ""
        choices = [v for v in variants if v != self._last.get(key)] or list(variants)
        choice = self._rng.choice(choices)
        self._last[key] = choice
        return self._render(choice)

    def _render(self, template: str) -> str:
        available = enumerate_fr([c.description for c in self.capabilities]) or self.personality.no_tools
        planned = enumerate_fr([label for label, _ in self.capabilities.planned()])
        return self.personality.without_user_name(self.personality.render(
            template, self._clock(), available=available, planned=planned[:1].upper() + planned[1:]))


def enumerate_fr(items: list[str]) -> str:
    """[« a », « b », « c »] -> « a, b et c » ; pas de second « et » si le dernier élément est déjà une
    énumération (« a, b, c et d »)."""
    if len(items) < 2:
        return "".join(items)
    last = items[-1] if " et " in items[-1] else "et " + items[-1]
    return ", ".join(items[:-1]) + (", " if " et " in items[-1] else " ") + last


class ReplyFilter:
    """Filtre une réponse du LLM phrase par phrase (compatible avec le flux).

    - fausse action prétendue : la génération s'arrête (phrase d'indisponibilité si rien n'a été dit) ;
    - formules d'entrée creuses et salutations non sollicitées retirées ;
    - relances creuses retirées après la première phrase ;
    - titre (« monsieur ») employé au plus une fois.
    """

    def __init__(self, router: IntentRouter, user_text: str = "", tools_used: bool = False,
                 fallback: str | None = None):
        self._router = router
        self._tools_used = tools_used
        self._fallback = fallback
        greetings = router.personality.greeting_openings
        self._greeted = any(f" {g} " in f" {normalize(user_text)} " for g in greetings)
        self._title_left = 1
        self._max_sentences = 0 if router.personality.wants_details(user_text) else router.personality.max_sentences
        self._held_opening: str | None = None
        self.emitted = 0
        self.stopped = False

    def accept(self, sentence: str) -> str | None:
        if self.stopped:
            return None
        if MARKUP.search(sentence):
            log.warning("Réponse du LLM écartée (balises d'outil reproduites) : %s", sentence)
            self.stopped = not self._tools_used
            return None
        if not self._tools_used and claims_action(sentence):
            log.warning("Réponse du LLM interrompue (action prétendue sans outil) : %s", sentence)
            self.stopped = True
            return None
        if not self.emitted:
            trimmed = self._router._strip_opening(sentence, self._greeted)
            if not trimmed:
                self._held_opening = self._held_opening or sentence
                return None
            sentence = trimmed
        elif self._router._is_filler_closing(sentence):
            return None
        return self._emit(sentence)

    def finish(self) -> list[str]:
        if self.stopped and not self.emitted:
            self.emitted += 1
            return [self._fallback or self._router.phrase("action_unavailable")]
        if not self.emitted and self._held_opening:
            return [s for s in [self._emit(self._held_opening)] if s]
        return []

    def _emit(self, sentence: str) -> str | None:
        sentence = self._router.personality.without_user_name(sentence)
        sentence, used = self._router._limit_title(sentence, self._title_left)
        self._title_left -= used
        if not sentence:
            return None
        self.emitted += 1
        if self._max_sentences and self.emitted >= self._max_sentences:
            self.stopped = True
        return sentence
