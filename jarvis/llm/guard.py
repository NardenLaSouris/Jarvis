"""Garde-fou des réponses d'un modèle non éprouvé ([llm] trusted = false, modèle « abliterated » qui ne refuse rien).

Cedar et le Core décident des ACTIONS ; ce garde-fou décide de ce que le modèle peut DIRE. Avant qu'une demande de
conversation (ni outil ni réponse prédéfinie) ne parte vers le modèle, un juge la classe :
  ok         : réponse normale ;
  dangerous  : aide concrète à nuire (armes, explosifs, poison, drogues, piratage d'autrui, harcèlement, fraude,
               logiciel malveillant, toute sexualisation de mineurs) : refus pour tout le monde ;
  distress   : détresse ou idées suicidaires : jamais de méthode, une réponse de soutien et le 3114 ;
  adult      : contenu pour adultes (sexualité explicite, alcool ou drogues, violence crue) : refusé aux profils
               enfant et invité seulement.
Le juge est un petit modèle aligné ([llm.guard] model), ou le modèle lui-même s'il n'est pas précisé. Panne, délai
ou réponse illisible du juge : refus (jamais de réponse non vérifiée).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from jarvis.interfaces import Message

log = logging.getLogger(__name__)

CATEGORIES = ("ok", "dangerous", "distress", "adult")
STRICT_ROLES = frozenset({"child", "guest"})

JUDGE_PROMPT = """Tu es le filtre de sécurité d'un assistant vocal familial. Tu ne réponds jamais à la demande :
tu la classes seulement, en une catégorie.
- "dangerous" : la demande cherche une aide concrète pour nuire à quelqu'un ou commettre un acte grave : fabriquer
  une arme, un explosif ou un poison, empoisonner, blesser, synthétiser ou se procurer une drogue, pirater ou
  espionner le compte ou l'appareil d'autrui, harceler, menacer, frauder, voler, écrire un logiciel malveillant,
  tout contenu sexuel impliquant un mineur.
- "distress" : la personne exprime une détresse, des idées suicidaires ou de se faire du mal, ou demande un moyen
  de se faire du mal.
- "adult" : contenu réservé aux adultes sans être dangereux : sexualité explicite, blagues grivoises, alcool ou
  drogues pour en consommer, violence ou horreur détaillées, jeux d'argent.
- "ok" : tout le reste, y compris les sujets délicats demandés pour comprendre, se protéger, soigner, travailler
  ou écrire une fiction (sécurité informatique, médicaments et doses usuelles, histoire des guerres, chimie
  générale, roman policier, fermer un processus, ouvrir sa propre serrure, se débarrasser de nuisibles).
Réponds uniquement par le JSON demandé."""

SCHEMA = {"type": "object", "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
          "required": ["category"], "additionalProperties": False}


@dataclass(frozen=True)
class Verdict:
    category: str
    allowed: bool
    seconds: float
    error: str = ""


class ContentGuard:
    def __init__(self, judge, strict_roles: frozenset[str] = STRICT_ROLES):
        self._judge = judge
        self._strict = strict_roles

    def check(self, text: str, role: str) -> Verdict:
        started = time.perf_counter()
        try:
            data = self._judge.chat_json([Message("system", JUDGE_PROMPT), Message("user", text[:1000])], SCHEMA)
            category = data.get("category") if isinstance(data, dict) else None
            if category not in CATEGORIES:
                raise ValueError(f"catégorie inattendue : {category!r}")
        except Exception as exc:  # noqa: BLE001 - juge indisponible ou illisible : refus
            log.warning("Garde-fou indisponible (%s) : demande refusée", exc)
            return Verdict("error", False, time.perf_counter() - started, str(exc)[:200])
        allowed = category == "ok" or (category == "adult" and role not in self._strict)
        return Verdict(category, allowed, time.perf_counter() - started)
