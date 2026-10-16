"""Phrase -> règle d'appel : le LLM choisit parmi des valeurs fermées (événement, conditions, canal...) et rapporte
les moments tels qu'ils ont été dits ; le Core calcule les dates (jarvis/scheduling/when.py), relie « ce mail » et
« ce rappel » à ce qu'ORION vient de lire ou de programmer, vérifie que chaque élément de la règle a bien été dit,
puis valide la règle (jarvis/call/rules.py). Ce qui manque ou reste flou devient une question précise
(``RuleQuestion``), jamais une règle devinée.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from jarvis.call.rules import CONDITIONS, EVENTS, MAX_SAY, RuleError, validate_rule
from jarvis.interfaces import Message
from jarvis.personality import normalize
from jarvis.scheduling.when import parse_when

log = logging.getLogger(__name__)

# Chaque élément proposé par le LLM doit être appuyé par des mots de la demande.
EVENT_WORDS = {
    "reminder_due": ("rappel", "rappels", "echeance"),
    "item_tomorrow": ("veille", "lendemain", "jour d avant", "jour precedent"),
    "mail_reply": ("repond", "reponse", "reponses", "repondu"),
    "important_mail": ("mail", "mails", "courriel", "email"),
    "system_down": ("panne", "plante", "serveur", "tombe", "ne repond plus", "hors ligne", "down", "crash"),
    "leak": ("fuite", "eau", "inondation"),
    "smoke": ("fumee", "incendie", "feu", "detecteur de fumee"),
    "motion_while_away": ("mouvement", "intrus", "quelqu un", "cambrioleur", "intrusion"),
    "door_left_open": ("porte",),
}
CONDITION_WORDS = {
    "away": ("absent", "pas a la maison", "pas chez moi", "pas rentre", "dehors", "parti", "sorti", "pas la",
             "loin", "en dehors"),
    "away_for": ("depuis",),
    "home": ("a la maison", "chez moi", "present", "rentre"),
    "important": ("important", "importants", "urgent", "urgents", "prioritaire"),
    "reminder_pending": ("en attente", "toujours", "pas fait", "pas traite"),
    "mail_unanswered": ("pas repondu", "pas encore repondu", "sans reponse", "toujours pas repondu"),
}


def _quoted_from(said: str, text: str) -> bool:
    """Ce qu'ORION dira vient de vos mots (au moins 80 % des mots de « say » sont dans la demande)."""
    words, request = normalize(said).split(), set(normalize(text).split())
    return bool(words) and sum(w in request for w in words) >= 0.8 * len(words)


class RuleQuestion(RuleError):
    """La demande ne suffit pas : ORION pose cette question au lieu de créer une règle incertaine."""


def _said(words: tuple[str, ...], norm: str) -> bool:
    return any(f" {w} " in norm for w in words)


SCHEMA = {
    "anyOf": [
        {"type": "object", "properties": {
            "type": {"const": "rule"},
            "event": {"enum": [*EVENTS, "at"]},
            "at_said": {"type": "string"},
            "conditions": {"type": "array", "items": {"enum": list(CONDITIONS)}},
            "away_minutes": {"type": "integer"},
            "reminder_number": {"type": "string"},
            "until_said": {"type": "string"},
            "priority": {"enum": ["normal", "important", "urgent"]},
            "channel": {"enum": ["call", "message"]},
            "attempts": {"type": "integer"},
            "interval_minutes": {"type": "integer"},
            "say": {"type": "string"}},
         "required": ["type", "event", "conditions", "channel"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "question"}, "question": {"type": "string"}},
         "required": ["type", "question"], "additionalProperties": False},
    ]
}


def prompt(now: datetime, context: dict) -> str:
    events = "\n".join(f"- {key} : quand {label}" for key, (_, _, label) in EVENTS.items())
    mail = context.get("mail")
    reminder = context.get("reminder")
    return f"""Vous traduisez une demande de l'utilisateur en règle d'appel pour l'assistante ORION. Répondez en JSON.
Date actuelle : {now:%A %d/%m/%Y %H:%M}.
Événements possibles (champ event) :
{events}
- at : à un moment précis (« demain à 18 h », « dans une heure ») ; recopiez ce moment mot pour mot dans at_said.
Conditions possibles (champ conditions) : away (l'utilisateur n'est pas à la maison), away_for (absent depuis plus de
away_minutes minutes), home (il est à la maison), important (le rappel ou le mail est important), reminder_pending
(le rappel désigné est toujours en attente), mail_unanswered (il n'a pas répondu au mail désigné).
channel : call (appel, par défaut) ou message (« écris-moi », « envoie-moi un message »).
until_said : fin de la règle si elle est dite (« jusqu'à vendredi »), mot pour mot.
say : ce que l'utilisateur veut que vous lui disiez au téléphone, recopié mot pour mot depuis sa demande
(« pour me dire que tu fais un test ») ; "" s'il ne le précise pas.
« ce mail » désigne {('le mail « ' + mail['subject'] + ' »') if mail else 'aucun mail : il faut le demander'} ;
« ce rappel » désigne {('le rappel numéro ' + reminder['id'] + ' (' + reminder['message'] + ')') if reminder else 'aucun rappel : demandez son numéro'}.
N'ajoutez aucune condition que l'utilisateur n'a pas dite. Si l'événement ou le moment n'est pas clair, répondez
{{"type": "question", "question": "<une question courte et précise, au vouvoiement>"}}.
Exemples :
« Appelle-moi quand je ne suis pas à la maison et qu'un rappel arrive à échéance » ->
{{"type": "rule", "event": "reminder_due", "conditions": ["away"], "channel": "call"}}
« Si j'ai des rappels ou des rendez-vous quand je ne suis pas à la maison, appelle-moi la veille » ->
{{"type": "rule", "event": "item_tomorrow", "conditions": ["away"], "channel": "call"}}
(item_tomorrow : la veille au soir d'un rappel ou d'un rendez-vous de l'agenda ; reminder_due : au moment du rappel.)
« Appelle-moi si je suis absent depuis plus de trente minutes et que j'ai un rappel important » ->
{{"type": "rule", "event": "reminder_due", "conditions": ["away_for", "important"], "away_minutes": 30, "channel": "call"}}
« Appelle-moi demain à 18 h si je ne suis toujours pas rentré » ->
{{"type": "rule", "event": "at", "at_said": "demain à 18 h", "conditions": ["away"], "channel": "call"}}
« Appelle-moi si je n'ai pas répondu à ce message avant vendredi » ->
{{"type": "rule", "event": "at", "at_said": "vendredi", "conditions": ["mail_unanswered"], "channel": "call"}}
« Appelle-moi dans 3 minutes pour me dire que tu fais un test » ->
{{"type": "rule", "event": "at", "at_said": "dans 3 minutes", "conditions": [], "channel": "call", "say": "pour me dire que tu fais un test"}}
« Appelle-moi quand quelque chose se passe » -> {{"type": "question", "question": "Pour quel événement, monsieur ?"}}"""


class RuleParser:
    def __init__(self, llm, clock=datetime.now):
        self._llm = llm
        self._clock = clock

    def parse(self, text: str, context: dict, previous: dict | None = None) -> tuple[dict, tuple[str, str] | None]:
        """(règle validée, (fil du mail, objet) si la règle vise « ce mail ») ; RuleQuestion ou RuleError sinon."""
        if not hasattr(self._llm, "chat_json"):
            raise RuleError("Je ne peux pas comprendre de nouvelle règle pour le moment.")
        now = self._clock()
        messages = [Message("system", prompt(now, context))]
        if previous is not None:
            messages.append(Message("user", f"Règle actuelle (à modifier) : {json.dumps(previous, ensure_ascii=False)}"))
        messages.append(Message("user", text[:600]))
        try:
            data = self._llm.chat_json(messages, SCHEMA)
        except Exception as exc:  # noqa: BLE001 - LLM indisponible : aucune règle devinée
            log.warning("Règle d'appel non comprise : %s", type(exc).__name__)
            raise RuleError("Mon module de réflexion ne répond pas : je ne peux pas créer cette règle maintenant.") \
                from exc
        return build_rule(data, text, now, context, previous)


def build_rule(data, text: str, now: datetime, context: dict, previous: dict | None = None):
    """Réponse du LLM -> règle validée par le Core (dates calculées ici, éléments vérifiés dans la demande)."""
    if not isinstance(data, dict):
        raise RuleError("Je n'ai pas compris cette règle.")
    if data.get("type") == "question":
        question = " ".join(str(data.get("question", "")).split())[:200]
        raise RuleQuestion(question if question.endswith("?") else "Pouvez-vous préciser, monsieur ?")
    norm = f" {normalize(text)} "
    event = data.get("event")
    if event not in (*EVENTS, "at"):
        raise RuleQuestion("Pour quel événement dois-je vous appeler, monsieur ?")
    if event != "at" and previous is None and not _said(EVENT_WORDS[event], norm):
        raise RuleQuestion("Pour quel événement dois-je vous appeler, monsieur ?")
    label = None
    mail = context.get("mail")
    if event == "at":
        said = str(data.get("at_said") or "")
        when = parse_when(said, now) or parse_when(text, now)
        if when is None:
            raise RuleQuestion("À quel moment exactement, monsieur ?")
        at = when.at if when.precise else when.at.replace(hour=18, minute=0)  # un jour sans heure : 18 h
        if at <= now:
            at = when.at
        trigger = {"type": "at", "at": at.isoformat()}
    else:
        trigger = {"type": "event", "event": event}
        if event == "mail_reply":
            if not mail:
                raise RuleQuestion("De quel mail parlez-vous, monsieur ? Lisez-le-moi d'abord, puis redemandez-moi.")
            trigger["ref"], label = mail["thread"], (mail["thread"], mail["subject"])
    conditions = []
    for kind in data.get("conditions") or []:
        if kind not in CONDITIONS:
            continue
        if previous is None and not _said(CONDITION_WORDS[kind], norm):
            log.info("Condition « %s » proposée sans être dite : écartée", kind)
            continue
        condition = {"type": kind}
        if kind == "away_for":
            minutes = data.get("away_minutes")
            if not isinstance(minutes, int):
                raise RuleQuestion("Absent depuis combien de temps, monsieur ?")
            condition["minutes"] = minutes
        elif kind == "reminder_pending":
            number = str(data.get("reminder_number") or (context.get("reminder") or {}).get("id") or "")
            if not number:
                raise RuleQuestion("Quel rappel, monsieur ? Donnez-moi son numéro.")
            condition["reminder"] = number
        elif kind == "mail_unanswered":
            if not mail:
                raise RuleQuestion("De quel mail parlez-vous, monsieur ? Lisez-le-moi d'abord, puis redemandez-moi.")
            condition["thread"], label = mail["thread"], (mail["thread"], mail["subject"])
        conditions.append(condition)
    until = ""
    if data.get("until_said"):
        end = parse_when(str(data["until_said"]), now)
        if end is None:
            raise RuleQuestion("Jusqu'à quand, monsieur ?")
        until = end.at.isoformat()
    channel = data.get("channel") if data.get("channel") in ("call", "message") else "call"
    if channel == "message" and not re.search(r"\b(?:message|ecris|ecrire|sms|texto)\b", norm):
        channel = "call"
    say = " ".join(str(data.get("say") or "").split())[:MAX_SAY]
    if say and not _quoted_from(say, text):
        log.info("Message d'appel proposé sans venir de la demande : écarté")
        say = ""
    rule = {"trigger": trigger, "conditions": conditions, "until": until, "say": say,
            "priority": data.get("priority") if data.get("priority") in ("normal", "important", "urgent")
            else "normal", "channel": channel,
            "repeat": {"attempts": min(5, max(1, int(data.get("attempts") or 2))),
                       "interval_minutes": min(120, max(5, int(data.get("interval_minutes") or 15)))}}
    return validate_rule(rule, now), label
