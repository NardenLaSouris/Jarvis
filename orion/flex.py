"""Mode Flex : ORION se présente et montre ce qu'elle sait faire, sur les enceintes ou au téléphone.

La présentation est écrite par le Core à partir d'un inventaire des fonctions réellement branchées (``Facts``,
rempli par orion/factory.py au démarrage), jamais par le LLM : rien n'y est affirmé qui ne soit vrai. L'inventaire
distingue :
- opérationnel : outil enregistré et service en marche ;
- sous conditions : présent, mais il faut une condition extérieure (réveil réseau du BIOS, iPhone joignable...) ;
- indisponible ou en développement : jamais présenté comme une capacité.
Chaque séquence a plusieurs tournures (tirées au hasard) : deux présentations ne sont pas identiques. La version
téléphone est plus courte. Aucun renseignement privé (prénom, contenu des mails, rendez-vous) n'y figure.

Le déclenchement, le choix enceintes ou téléphone selon la présence, le délai d'une minute et l'appel passent par
la règle d'appel intégrée « flex » (orion/call/rules.py).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from orion.personality import normalize
from orion.tools.base import Param, Risk, Tool

FLEX_RULE = "flex"
PHONE_OPENING = "Bonjour, monsieur. Permettez-moi de me présenter : je suis ORION, votre assistante personnelle."
OPERATIONAL, CONDITIONAL, UNAVAILABLE = "opérationnel", "sous conditions", "indisponible"


@dataclass
class Facts:
    """Ce qui est réellement branché (rempli au démarrage) : la seule source de la présentation."""
    tools: frozenset[str] = frozenset()
    core_host: str = "votre mini-PC"
    llm_host: str = ""  # machine qui fait tourner le modèle de langage (« le Katana »), "" : sur le Core
    computers: tuple[str, ...] = ()  # PC pilotés par un agent (« votre PC », « votre PC portable »)
    speaker: str = ""  # sortie audio de la maison (« les haut-parleurs de votre PC »)
    lights: int = 0
    presence_sensors: int = 0
    calls: bool = False  # appels et messages Linphone vers votre iPhone
    mail_watch: bool = False  # surveillance des mails (annonces)
    mail_follow_up: bool = False  # analyse des échéances, rappels J-1 / H-1 h 30
    call_rules: int = 0
    wake_mac: bool = False  # adresse matérielle connue pour allumer un PC
    face: bool = False  # visage animé à l'écran
    learning: bool = False
    extra: dict = field(default_factory=dict)


def inventory(f: Facts) -> list[tuple[str, str, str]]:
    """(fonction, état, précision) : l'inventaire que la présentation a le droit d'utiliser."""
    t = f.tools
    items: list[tuple[str, str, str]] = []

    def add(name: str, ok: bool, detail: str = "", condition: str = "") -> None:
        if ok and condition:
            items.append((name, CONDITIONAL, condition))
        elif ok:
            items.append((name, OPERATIONAL, detail))
        else:
            items.append((name, UNAVAILABLE, detail))

    add("rappels", "create_reminder" in t)
    add("minuteurs", "create_timer" in t)
    add("réveils", "create_alarm" in t)
    add("agenda", "list_events" in t)
    add("météo", "get_weather" in t)
    add("recherche d'informations", f.extra.get("web", False))
    add("mails", "read_mail" in t, "lecture, tri, réponses après confirmation")
    add("échéances des mails", f.mail_follow_up)
    add("appels et messages", f.calls, condition="par Linphone, sur votre iPhone")
    add("règles d'appel", f.calls and "create_call_rule" in t)
    add("lumières", f.lights > 0, f"{f.lights} lumière(s)")
    add("présence", f.presence_sensors > 0, f"{f.presence_sensors} capteur(s)")
    add("musique", "spotify_play" in t)
    add("ordinateurs", bool(f.computers) and "open_application" in t, ", ".join(f.computers))
    add("fichiers", "find_files" in t)
    add("allumer un PC", "wake_pc" in t and f.wake_mac, condition="si le réveil réseau est activé sur le PC")
    add("éteindre un PC", "shutdown_pc" in t, "toujours après votre accord")
    add("apprentissage", f.learning, "vocabulaire, consignes, réveil")
    add("plusieurs enceintes", False, "une seule sortie audio à la maison")
    return items


def _available(f: Facts) -> set[str]:
    return {name for name, state, _ in inventory(f) if state in (OPERATIONAL, CONDITIONAL)}


def _pick(rng: random.Random, *options: str) -> str:
    return rng.choice(options)


def _join(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " et " + parts[-1] if parts else ""


def presentation(f: Facts, phone: bool = False, rng: random.Random | None = None) -> list[str]:
    """Séquences courtes de la présentation, aux enceintes ou au téléphone (même contenu, toujours adressé à
    monsieur ; au téléphone, l'ouverture fixe ``PHONE_OPENING`` sert de salut au décroché)."""
    rng = rng or random.Random()
    have = _available(f)
    # Toujours à monsieur (aux enceintes comme au téléphone) ; au téléphone, elle ouvre l'appel par ce salut.
    opening = PHONE_OPENING if phone else \
        _pick(rng, "Bonjour, monsieur. Permettez-moi de me présenter : je suis ORION, votre assistante personnelle.",
              "Bonsoir, monsieur. Je suis ORION, votre assistante personnelle, et je suis ravie de me présenter.",
              "Je m'appelle ORION, monsieur. Je suis votre assistante personnelle.")
    role = _pick(rng, "Je coordonne vos outils numériques et votre maison connectée, pour que vous gardiez le contrôle "
                      "sans avoir à vous soucier de chaque détail.",
                 "Mon rôle : coordonner votre environnement numérique et votre maison, et vous épargner les détails.")
    body = [f"Mon cœur réside sur {f.core_host}"]
    if f.llm_host:
        body.append(f"je réfléchis grâce à un modèle de langage qui tourne sur {f.llm_host}")
    architecture = ", ".join(body)
    architecture += (f", et mes agents me donnent la main sur {_join(list(f.computers))}." if f.computers else ".")
    abilities = []
    agenda = [w for w, key in (("vos rappels", "rappels"), ("vos minuteurs", "minuteurs"), ("vos réveils", "réveils"))
              if key in have]
    if agenda:
        abilities.append("je gère " + _join(agenda))
    if "échéances des mails" in have:
        abilities.append("je repère les échéances dans vos mails")
    elif "mails" in have:
        abilities.append("je trie et je lis vos mails")
    if "lumières" in have:
        abilities.append("je pilote l'éclairage de la maison")
    if "musique" in have:
        abilities.append("je lance votre musique")
    if "ordinateurs" in have:
        abilities.append("j'ouvre vos applications et vos fichiers")
    if "recherche d'informations" in have or "météo" in have:
        abilities.append("je trouve l'information qu'il vous faut" + (", météo comprise" if "météo" in have else ""))
    head = _pick(rng, "Concrètement, ", "Au quotidien, ", "Dans les faits, ")
    if len(abilities) > 3:  # deux séquences courtes plutôt qu'une énumération
        skills = [f"{head}{_join(abilities[:3])}.",
                  f"{_pick(rng, 'Et puis, ', 'Par ailleurs, ', 'Enfin, ')}{_join(abilities[3:])}."]
    else:
        skills = [head + _join(abilities) + "."] if abilities else []
    calls = ""
    if "appels et messages" in have:
        calls = _pick(rng, "Et lorsque vous n'êtes pas là, je ne reste pas muette : je peux vous appeler, ou vous "
                           "écrire, dès que les conditions que vous avez choisies sont réunies.",
                      "Quand vous vous absentez, je vous joins par téléphone ou par message, selon les règles que "
                      "vous m'avez données.")
    intelligence = _pick(rng, "Je comprends le langage naturel, je garde le fil de la conversation, et une seule "
                              "phrase me suffit pour enchaîner plusieurs actions.",
                         "Vous me parlez naturellement : je suis le fil de la conversation et j'enchaîne plusieurs "
                         "actions dans une même demande.")
    watch = []
    if "présence" in have:
        watch.append("je sais quand vous partez et quand vous rentrez")
    if "règles d'appel" in have or f.call_rules:
        watch.append("j'applique les règles que vous m'avez confiées")
    watch.append("je vous préviens de ce qui compte, au bon moment")
    autonomy = f"Et je veille même sans qu'on me sollicite : {_join(watch)}. Pour tout ce qui est sensible, " \
               "j'attends toujours votre accord."
    conditional = "Bientôt, je pourrai aussi allumer votre ordinateur à distance : il ne manque qu'un réglage." \
        if "allumer un PC" in have else ""
    closing = _pick(rng, "Et ceci n'est qu'un aperçu de mes capacités actuelles : j'évolue avec vous, une fonction "
                         "après l'autre.",
                    "Ce n'est qu'un aperçu, monsieur. De nouvelles fonctions me rejoindront au fil du temps.",
                    "Voilà pour aujourd'hui. Demain, j'en saurai davantage.")
    return [s for s in (f"{opening} {role}", architecture, *skills, calls, intelligence, autonomy, conditional,
                        closing) if s]


FLEX_WORDS = re.compile(r"\b(?:mode flex|flex|flexe|fais (?:ta|une|nous ta|nous une) (?:presentation|demonstration|demo)|"
                        r"montre(?: leur| nous| moi)? (?:tout )?ce que tu sais faire|impressionne(?: les| nous| moi)?)\b")
FLEX_SWITCH = re.compile(r"\b(?:active|reactive|desactive|coupe|suspends|arrete)\w*\b.*\bflex")


def is_flex(text: str) -> bool:
    """« Flex », « mode flex », « fais ta présentation », « montre-leur ce que tu sais faire »."""
    norm = normalize(text)
    return bool(FLEX_WORDS.search(norm)) and not FLEX_SWITCH.search(norm)


def flex_tools(rules) -> list[Tool]:
    from orion.call.rule_tools import _enabled

    def run() -> dict:
        return {"result": rules.trigger_flex()}

    def said(r: dict) -> str:
        return {"home": "Avec plaisir, monsieur.",
                "waiting": "Vous n'êtes pas à la maison, monsieur : je vous appelle dans une minute pour me présenter, "
                           "à moins que vous ne rentriez d'ici là.",
                "already": "Je suis déjà en train de me présenter, monsieur.",
                "disabled": "Le mode Flex est désactivé, monsieur.",
                "unavailable": "Le mode Flex n'est pas disponible.",
                "call": "Vos enceintes ne répondent pas, monsieur : je vous appelle pour me présenter."}.get(r["result"], "Très bien.")

    def switch(enabled: str) -> dict:
        return {"enabled": rules.set_flex_enabled(enabled == "oui")}

    return [
        Tool("flex", "Mode Flex : ORION se présente et montre ce qu'elle sait faire (enceintes, ou téléphone si "
             "l'utilisateur est absent).", {}, {"result": "home, waiting, already, disabled"}, Risk.SAFE, run, say=said),
        Tool("set_flex_enabled", "Active ou désactive le mode Flex.",
             {"enabled": Param(str, "activer ou désactiver", choices=("oui", "non"), hidden=True, resolve=_enabled)},
             {"enabled": "actif ?"}, Risk.SAFE, switch,
             say=lambda r: f"Le mode Flex est {'activé' if r['enabled'] else 'désactivé'}, monsieur."),
    ]
