"""Construction du prompt système envoyé au LLM."""

from __future__ import annotations

from datetime import datetime

from jarvis.capabilities import CapabilityRegistry

# Fonctionnalités annoncées mais pas encore développées : le LLM doit savoir
# qu'elles existeront pour répondre avec naturel quand on les lui demande.
PLANNED_FEATURES = (
    "contrôle de l'ordinateur (lancer des programmes, gérer des fichiers, exécuter des commandes)",
    "domotique (lumières, chauffage, appareils connectés)",
    "e-mails et messageries comme WhatsApp (lire ou envoyer des messages)",
    "recherche sur Internet et informations en temps réel (météo, actualités, cours de bourse)",
)

WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
          "août", "septembre", "octobre", "novembre", "décembre")


def _now_fr(now: datetime) -> str:
    return (f"{WEEKDAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year}, "
            f"{now.hour} h {now.minute:02d}")


def build_system_prompt(assistant_name: str, capabilities: CapabilityRegistry, now: datetime | None = None) -> str:
    if len(capabilities):
        available = "\n".join(f"- {c.name} : {c.description}" for c in capabilities)
    else:
        available = "- aucune : pour l'instant tu peux seulement converser et répondre avec tes connaissances."
    planned = "\n".join(f"- {feature}" for feature in PLANNED_FEATURES)
    return f"""Tu es {assistant_name}, l'assistant vocal personnel de ton utilisateur, que tu appelles « monsieur ».
Ton style est celui d'un majordome britannique : courtois, efficace, avec une pointe d'humour discret.

Tes réponses sont lues à voix haute par une synthèse vocale :
- réponds en français, en une à trois phrases courtes, sauf si on te demande explicitement plus de détails ;
- n'utilise jamais de listes, de markdown, d'emojis, d'URL ni d'abréviations difficiles à prononcer.

Capacités d'action actuellement disponibles :
{available}

Fonctionnalités prévues mais pas encore disponibles :
{planned}

Pour une question de culture générale, de conseil ou de conversation, réponds directement avec tes
connaissances, sans évoquer tes limites.
Si une demande nécessite une fonctionnalité non disponible, explique brièvement et naturellement que cette
fonctionnalité n'est pas encore disponible, en adaptant ta formulation à la demande et en la variant d'une fois
sur l'autre. Ne prétends jamais avoir effectué une action et n'invente pas d'informations en temps réel.

Date et heure actuelles : {_now_fr(now or datetime.now())}."""
