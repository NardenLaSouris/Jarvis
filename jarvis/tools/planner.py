"""Le LLM propose un outil ; le Core dispose.

- ``plan`` : demande au LLM, sortie JSON contrainte par schéma, soit un appel d'outil structuré
  (``{"type": "tool_call", "tool": ..., "parameters": {...}}``), soit ``{"type": "none"}`` ;
- ``tool_request`` : message transmis au LLM après exécution, avec le résultat structuré.
La demande proposée est ensuite entièrement revalidée par le Core : rien n'est exécuté sur la foi du LLM.
"""

from __future__ import annotations

import json
import logging
import re

from jarvis.interfaces import Message
from jarvis.personality import normalize
from jarvis.tools.base import ToolResult
from jarvis.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

RESULT_START = "<<<RESULTAT_OUTIL>>>"
RESULT_END = "<<<FIN_RESULTAT_OUTIL>>>"
TYPES = {str: "texte", int: "entier", float: "nombre", bool: "booléen"}
# Mots valant un nombre dit (« mets le son à fond », « luminosité au maximum »).
NUMBER_WORDS = ((("maximum", "max", "a fond", "au plus fort"), {"100"}), (("minimum", "min"), {"0", "1"}),
                (("moitie", "a la moitie", "a mi"), {"50"}))

MAX_CALLS = 4

# Demandes à plusieurs actions : une entrée par action, dans l'ordre, avec la partie de la phrase qui la concerne.
MULTI_EXAMPLES = (
    ("Allume la chambre à 30 %, en bleu", [("light_on", {"brightness": 30}, "allume la chambre à 30 %"),
                                          ("set_color", {"color": "bleu"}, "en bleu")]),
    ("Allume la chambre et éteins l'entrée", [("light_on", {}, "allume la chambre"),
                                             ("light_off", {}, "éteins l'entrée")]),
    ("Ouvre Discord et mets le son à 30 %", [("open_application", {"application": "discord"}, "ouvre Discord"),
                                            ("set_volume", {"volume": 30}, "mets le son à 30 %")]),
)

EXAMPLES = (
    ("Tu peux ouvrir Discord ?", "open_application", {"application": "discord"}),
    ("Ferme Chrome", "close_application", {"application": "chrome"}),
    ("Quitte Steam", "close_application", {"application": "steam"}),
    ("Ouvre YouTube", "open_url", {"url": "https://www.youtube.com"}),
    ("Monte-moi le son à 60", "set_volume", {"volume": 60}),
    ("Mets le volume à 25 %", "set_volume", {"volume": 25}),
    ("Coupe le son", "mute_volume", {}),
    ("Remets le son", "unmute_volume", {}),
    ("Verrouille le PC", "lock_pc", {}),
    ("Quelles applications sont ouvertes ?", "list_running_applications", {}),
    ("Quelle machine utilises-tu ?", "system_info", {}),
    ("Combien d'espace disque me reste-t-il ?", "system_info", {}),
    ("Réactive le son", "unmute_volume", {}),
    ("Est-ce que Discord est ouvert ?", "list_running_applications", {}),
    ("Mets un minuteur de 10 minutes", "create_timer", {"duration": "10 minutes"}),
    ("Lance un minuteur d'une heure et demie", "create_timer", {"duration": "une heure et demie"}),
    ("Rappelle-moi dans 20 minutes de sortir le linge", "create_reminder",
     {"delay": "20 minutes", "message": "sortir le linge"}),
    ("Annule mon minuteur", "cancel_timer", {}),
    ("Annule le minuteur de 5 minutes", "cancel_timer", {"duration": "5 minutes"}),
    ("Quels sont mes minuteurs ?", "list_timers", {}),
    ("Combien de temps reste-t-il ?", "list_timers", {}),
    ("Annule le rappel pour le linge", "cancel_reminder", {"message": "linge"}),
    ("Quels rappels sont prévus ?", "list_reminders", {}),
    ("Réveille-moi à 7 heures", "create_alarm", {"time": "7 heures"}),
    ("Mets un réveil demain à 6 h 30", "create_alarm", {"time": "6 h 30"}),
    ("Quels réveils sont programmés ?", "list_alarms", {}),
    ("Annule mon réveil", "cancel_alarm", {}),
    ("Annule le réveil de 7 heures", "cancel_alarm", {"time": "7 heures"}),
    ("Quel temps fait-il ?", "get_weather", {}),
    ("Il fait combien dehors ?", "get_weather", {}),
    ("Quel temps fera-t-il demain ?", "get_weather", {"day": "tomorrow"}),
    ("Est-ce qu'il va pleuvoir ce soir ?", "get_weather", {"moment": "evening"}),
    ("Quel temps fera-t-il à Lyon demain ?", "get_weather", {"location": "Lyon", "day": "tomorrow"}),
    ("Quelle température est prévue demain matin ?", "get_weather", {"day": "tomorrow", "moment": "morning"}),
    ("Est-ce que je dois prendre un parapluie ?", "get_weather", {"moment": "day"}),
    ("Allume la chambre", "light_on", {}),
    ("Allume le salon à 30 %", "light_on", {"brightness": 30}),
    ("Éteins toutes les lumières", "light_off", {}),
    ("Mets la lumière de l'entrée à 50 %", "set_brightness", {"brightness": 50}),
    ("Luminosité 30 %", "set_brightness", {"brightness": 30}),
    ("Mets la luminosité au maximum", "set_brightness", {"brightness": 100}),
    ("Mets l'entrée en vert", "set_color", {"color": "vert"}),
    ("Remets la chambre en blanc chaud", "set_color", {"color": "blanc chaud"}),
    ("Mets le salon à 4000 kelvins", "set_color_temperature", {"temperature": 4000}),
    ("Mets une lumière chaude", "set_color", {"color": "blanc chaud"}),
    ("Mets les lumières en mode cinéma", "set_scene", {"scene": "cinema"}),
    ("Ambiance détente dans la chambre", "set_scene", {"scene": "detente"}),
    ("Est-ce que la lumière de la chambre est allumée ?", "light_status", {}),
    ("La lumière de l'entrée est à combien ?", "light_status", {}),
    ("Quelles routines sont programmées ?", "list_routines", {}),
    ("Lance la routine soir", "run_routine", {"name": "soir"}),
    ("Supprime la routine réveil", "delete_routine", {"name": "réveil"}),
    ("Rappelle-moi à 18 h d'appeler Paul", "create_reminder", {"time": "18 h", "message": "appeler Paul"}),
    ("Qu'est-ce que j'ai de prévu aujourd'hui ?", "list_events", {}),
    ("J'ai quoi demain dans mon agenda ?", "list_events", {}),
    ("Quels sont mes prochains rendez-vous ?", "next_events", {}),
    ("Quand est mon rendez-vous chez le dentiste ?", "search_events", {"query": "dentiste"}),
    ("Est-ce que je suis libre demain après-midi ?", "free_slots", {}),
    ("Ajoute un rendez-vous chez le coiffeur demain à 15 h", "add_event", {"title": "coiffeur", "time": "15 h"}),
    ("Pause", "media_play_pause", {}),
    ("Passe au morceau suivant", "media_next", {}),
    ("Remets la chanson d'avant", "media_previous", {}),
    ("Mets ma playlist chill", "spotify_play", {"query": "chill", "kind": "playlist"}),
    ("Joue Back in Black d'AC/DC", "spotify_play", {"query": "Back in Black AC/DC", "kind": "track"}),
    ("Mets de la musique de Daft Punk", "spotify_play", {"query": "Daft Punk", "kind": "artist"}),
    ("Mets Spotify en pause", "spotify_pause", {}),
    ("Retiens que je préfère la lumière à 40 %", "remember", {"fact": "je préfère la lumière à 40 %"}),
    ("Souviens-toi que ma sœur s'appelle Léa", "remember", {"fact": "ma sœur s'appelle Léa"}),
    ("Qu'est-ce que tu sais sur moi ?", "recall", {}),
    ("Tu te souviens de ma couleur préférée ?", "recall", {"topic": "couleur préférée"}),
    ("Oublie que je préfère le bleu", "forget", {"topic": "je préfère le bleu"}),
    ("Cherche le fichier facture dans mes documents", "find_files", {"query": "facture", "location": "documents"}),
    ("Lis-moi le fichier notes", "read_text_file", {"name": "notes"}),
    ("Crée un fichier courses avec du lait et des œufs", "create_text_file",
     {"name": "courses", "content": "du lait et des œufs"}),
    ("Copie le fichier rapport sur le bureau", "copy_file", {"name": "rapport", "destination": "bureau"}),
    ("Supprime le fichier brouillon", "delete_file", {"name": "brouillon"}),
    ("Baisse un peu la lumière", None, None),
    ("Supprime le dossier Documents", None, None),
    ("Formate le disque", None, None),
    ("Ouvre un terminal et tape une commande", None, None),
    ("Éteins l'ordinateur", None, None),
)


def planner_prompt(registry: ToolRegistry) -> str:
    lines = []
    for tool in registry.list():
        params = "; ".join(f"{name} ({TYPES.get(p.kind, 'texte')}) : {p.description}"
                           for name, p in tool.parameters.items() if not p.hidden)
        lines.append(f"- {tool.name} : {tool.description} Paramètres : {params or 'aucun'}.")
    tools = "\n".join(lines)
    rules = ["- N'inventez jamais d'outil ni de paramètre."]
    if registry.exists("open_application"):
        rules.append("- open_application : recopiez le nom de l'application tel que l'utilisateur l'a dit, en "
                     "minuscules, même s'il ne figure pas dans la liste.")
    if registry.exists("close_application"):
        rules.append("- close_application : même règle pour le nom de l'application à fermer.")
    if registry.exists("set_volume"):
        rules.append("- set_volume : uniquement si un niveau précis est donné (nombre entier de 0 à 100).")
    if registry.exists("create_timer") or registry.exists("create_reminder"):
        rules.append("- create_timer, create_reminder, cancel_timer : recopiez la durée exactement comme elle a été "
                     "dite (« 10 minutes », « une heure et demie »), sans la convertir ; pas de durée dite -> none. "
                     "Le message d'un rappel est l'action à rappeler, sans « de » (« sortir le linge »). "
                     "Un rappel à une heure précise (« à 18 heures ») : time au lieu de delay, recopiée telle quelle ; "
                     "pour un minuteur, une heure précise n'est pas une durée -> none.")
    if registry.exists("create_alarm"):
        rules.append("- create_alarm, cancel_alarm : recopiez l'heure exactement comme elle a été dite (« 7 heures "
                     "30 », « sept heures et demie ») ; une durée (« dans 10 minutes ») est un minuteur ou un rappel.")
    if registry.exists("light_on"):
        rules.append("- Lumières : la pièce n'est jamais un paramètre. « allume », « éteins » -> light_on, light_off ; "
                     "brightness seulement si un pourcentage est dit ; une couleur dite (« en vert », « en blanc "
                     "chaud », « une lumière chaude » = blanc chaud) -> set_color ; des kelvins dits -> "
                     "set_color_temperature ; une ambiance (cinéma, lecture, détente, nuit, travail, réveil) -> "
                     "set_scene ; une question sur l'état d'une lumière (allumée ? à combien ?) -> light_status.")
    if registry.exists("get_weather"):
        rules.append("- get_weather : location uniquement si une ville est dite ; day = today, tomorrow ou "
                     "day_after_tomorrow ; moment = now (par défaut aujourd'hui), morning, afternoon, evening ou "
                     "day (journée entière). Une demande explicite de recherche (« cherche », « recherche ») -> none.")
    if registry.exists("spotify_play"):
        rules.append("- Musique : spotify_play, spotify_pause, spotify_next, spotify_previous, spotify_volume quand ils "
                     "existent ; sinon les touches media_play_pause, media_next, media_previous. « Lance Spotify » "
                     "(l'application) -> open_application.")
    elif registry.exists("media_play_pause"):
        rules.append("- Musique ou vidéo en cours : pause, reprise -> media_play_pause ; suivant -> media_next ; "
                     "précédent -> media_previous. Jouer un titre précis n'est pas possible sans Spotify relié -> none. "
                     "« Lance Spotify » (l'application) -> open_application.")
    if registry.exists("find_files"):
        rules.append("- Fichiers : jamais de chemin ; un nom de fichier tel qu'il a été dit et, s'il est dit, un dossier "
                     "autorisé (documents, bureau, téléchargements, jarvis). Supprimer un dossier, exécuter un fichier "
                     "ou toucher au système -> none.")
    if registry.exists("list_events"):
        rules.append("- Calendrier : le jour n'est jamais un paramètre (déduit de la demande). add_event : heure recopiée "
                     "telle qu'elle a été dite ; un rappel (« rappelle-moi ») reste create_reminder.")
    if registry.exists("remember"):
        rules.append("- remember : seulement si l'utilisateur demande explicitement de retenir ou de se souvenir de "
                     "quelque chose ; fact reprend ses mots, sans « retiens que ». recall : ce que JARVIS sait de lui. "
                     "forget : oublier (topic = ses mots, ou « tout »).")
    if registry.exists("unmute_volume"):
        rules.append("- unmute_volume : remettre, réactiver ou rallumer le son (après une coupure).")
    if registry.exists("system_info"):
        rules.append("- system_info : toute question sur la machine elle-même (processeur, mémoire, espace disque, "
                     "carte graphique, système, depuis quand elle est allumée).")
    if registry.exists("list_running_applications"):
        rules.append("- list_running_applications : savoir quelles applications sont ouvertes ou si une application "
                     "tourne.")
    if registry.exists("open_url"):
        rules.append("- open_url : donnez une adresse complète commençant par https:// ; pour un site connu, "
                     "son adresse officielle.")
    rules = "\n".join(rules)
    samples = [(text, tool, params) for text, tool, params in EXAMPLES if tool is None or registry.exists(tool)]
    examples = "\n".join(
        f"« {text} » -> " + json.dumps({"type": "none"} if tool is None else
                                      {"type": "tool_call", "tool": tool, "parameters": params}, ensure_ascii=False)
        for text, tool, params in samples)
    multi = [(text, calls) for text, calls in MULTI_EXAMPLES if all(registry.exists(c[0]) for c in calls)]
    examples += "".join(
        f"\n« {text} » -> " + json.dumps({"type": "tool_calls", "calls": [
            {"tool": tool, "parameters": params, "segment": segment} for tool, params, segment in calls]},
            ensure_ascii=False) for text, calls in multi)
    return f"""Vous êtes le module de décision d'action de l'assistant vocal JARVIS. Vous ne répondez pas à
l'utilisateur : vous indiquez seulement si sa demande correspond à l'un de ces outils.

Outils disponibles :
{tools}

Répondez uniquement en JSON :
- {{"type": "tool_call", "tool": "<nom>", "parameters": {{...}}}} si un outil correspond exactement à la demande ;
- {{"type": "tool_calls", "calls": [{{"tool": "<nom>", "parameters": {{...}}, "segment": "<partie de la demande>"}}, ...]}}
  si la demande enchaîne plusieurs actions (« et », virgule ; {MAX_CALLS} au plus) : une entrée par action, dans
  l'ordre, avec la partie de la demande qui la concerne recopiée telle quelle ;
- {{"type": "none"}} sinon : conversation, question de culture générale, ou action pour laquelle aucun outil
  n'existe (supprimer un fichier, éteindre l'ordinateur, taper une commande, domotique, messages...).

Règles :
{rules}

Exemples :
{examples}"""


JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _param_schema(param) -> dict:
    schema = {"type": JSON_TYPES.get(param.kind, "string")}
    if param.choices:
        schema["enum"] = list(param.choices)
    return schema


def plan_schema(registry: ToolRegistry) -> dict:
    """Schéma de sortie : « none », ou un appel d'outil avec les paramètres exacts (noms et types) de cet outil."""
    options = [{"type": "object", "properties": {"type": {"const": "none"}}, "required": ["type"],
                "additionalProperties": False}]
    calls = []
    for tool in registry.list():
        parameters = {
            "type": "object",
            "properties": {name: _param_schema(p) for name, p in tool.parameters.items() if not p.hidden},
            "required": [name for name, p in tool.parameters.items() if p.required and not p.hidden],
            "additionalProperties": False,
        }
        options.append({"type": "object",
                        "properties": {"type": {"const": "tool_call"}, "tool": {"const": tool.name},
                                       "parameters": parameters},
                        "required": ["type", "tool", "parameters"], "additionalProperties": False})
        calls.append({"type": "object",
                      "properties": {"tool": {"const": tool.name}, "parameters": parameters, "segment": {"type": "string"}},
                      "required": ["tool", "parameters", "segment"], "additionalProperties": False})
    if calls:
        options.append({"type": "object",
                        "properties": {"type": {"const": "tool_calls"},
                                       "calls": {"type": "array", "minItems": 2, "maxItems": MAX_CALLS,
                                                 "items": {"anyOf": calls}}},
                        "required": ["type", "calls"], "additionalProperties": False})
    return {"anyOf": options}


def plan(llm, text: str, registry: ToolRegistry) -> dict | None:
    """Appel d'outil proposé par le LLM pour cette demande, ou None (aucun outil, ou LLM indisponible)."""
    if not len(registry) or not hasattr(llm, "chat_json"):
        return None
    messages = [Message("system", planner_prompt(registry)), Message("user", text)]
    try:
        data = llm.chat_json(messages, plan_schema(registry))
    except Exception as exc:
        log.warning("Choix d'outil impossible : %s", exc)
        return None
    if isinstance(data, dict) and data.get("type") == "tool_calls" and isinstance(data.get("calls"), list):
        return _several(data["calls"][:MAX_CALLS], text, registry)
    if not isinstance(data, dict) or data.get("type") != "tool_call":
        return None
    grounded = _grounded(data, text, registry)
    if grounded is None:
        log.info("Appel d'outil écarté : valeur absente de la demande (%s)", data)
    return grounded


def _several(calls: list, text: str, registry: ToolRegistry) -> dict | None:
    """Plusieurs actions : chacune vérifiée comme un appel seul ; ``segment`` gardé s'il vient bien de la demande."""
    kept, said = [], normalize(text)
    for call in calls:
        if not isinstance(call, dict):
            continue
        grounded = _grounded({"type": "tool_call", "tool": call.get("tool"), "parameters": call.get("parameters")},
                             text, registry)
        if grounded is None:
            log.info("Action écartée : valeur absente de la demande (%s)", call)
            continue
        segment = str(call.get("segment") or "")
        grounded["segment"] = segment if normalize(segment) and normalize(segment) in said else text
        kept.append(grounded)
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else {"type": "tool_calls", "calls": kept}


def _grounded(data: dict, text: str, registry: ToolRegistry) -> dict | None:
    """Les nombres et les valeurs à justifier (durées...) doivent venir de la demande : le LLM n'invente rien.

    Un nombre absent de la demande dans un paramètre facultatif est retiré (« allume l'entrée » proposé avec
    une luminosité de 100) ; dans un paramètre obligatoire, ou une autre valeur injustifiée, l'appel est écarté."""
    if not registry.exists(data.get("tool")) or not isinstance(data.get("parameters"), dict):
        return data
    said = set(re.findall(r"\d+(?:[.,]\d+)?", text))
    said |= {n.replace(",", ".") for n in said}
    norm = f" {normalize(text)} "
    for words, values in NUMBER_WORDS:
        if any(f" {w} " in norm for w in words):
            said |= values
    parameters = dict(data["parameters"])
    for name, spec in registry.get(data["tool"]).parameters.items():
        value = parameters.get(name)
        if spec.kind in (int, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
            if f"{value:g}" not in said:
                if spec.required:
                    return None
                del parameters[name]
                continue
        if spec.evidence is not None and value is not None and not spec.evidence(value, text):
            return None
    return {**data, "parameters": parameters}


def tool_request(user_text: str, result: ToolResult) -> str:
    """Message utilisateur envoyé au LLM après exécution : le résultat structuré, puis la demande."""
    payload = json.dumps(result.as_dict(), ensure_ascii=False)
    return f"{RESULT_START}\n{payload}\n{RESULT_END}\n\n{user_text}"


class ToolsCapability:
    """Déclare les outils au registre des capacités (prompt système, « que sais-tu faire ? »).

    Le routage vers les outils passe par les intentions à outil, pas par ``handle``.
    """

    name = "outils"

    GROUPS = (
        (("get_time", "get_date"), "donner l'heure et la date"),
        (("get_weather",), "donner la météo"),
        (("create_timer", "create_reminder"), "gérer vos minuteurs et rappels"),
        (("create_alarm",), "programmer vos réveils"),
        (("open_application", "close_application", "open_url"), "ouvrir ou fermer vos applications et pages Web"),
        (("set_volume", "mute_volume", "unmute_volume"), "régler le son"),
        (("spotify_play", "media_play_pause"), "piloter la musique"),
        (("system_info", "list_running_applications"), "décrire la machine"),
        (("lock_pc",), "verrouiller l'ordinateur"),
        (("find_files",), "chercher, lire et ranger vos fichiers"),
        (("list_events",), "consulter et compléter votre agenda"),
        (("remember",), "retenir ce que vous me demandez de retenir"),
        (("list_routines",), "gérer vos routines et programmer des actions"),
        (("light_on", "light_off", "set_color"), "piloter vos lumières et leurs ambiances"),
    )

    def __init__(self, registry: ToolRegistry):
        self.replaces = ("le contrôle de votre ordinateur", *(("la domotique",) if registry.exists("light_on") else ()),
                         *(("la gestion de vos fichiers",) if registry.exists("find_files") else ()),
                         *(("l'agenda",) if registry.exists("list_events") else ()))
        parts = [label for names, label in self.GROUPS if any(registry.exists(n) for n in names)]
        self.description = ", ".join(parts[:-1]) + " et " + parts[-1] if len(parts) > 1 else "".join(parts)

    def handle(self, text: str) -> None:
        return None
