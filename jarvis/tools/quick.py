"""Commandes simples reconnues sans LLM (déterministe) : lumières, son, applications, minuteurs, rappels, réveils,
météo, verrouillage.

Deux usages :
- mode dégradé : le worker LLM (Katana) est hors ligne, JARVIS exécute quand même les commandes simples ;
- raccourci (``[tools] fast_path``) : une commande sans ambiguïté évite le choix d'outil par le LLM (~0,5 à 1 s).

``quick_plan`` rend exactement ce que rendrait le planificateur du LLM (un ``tool_call``, ou ``tool_calls`` pour
une demande enchaînée), ou None dès que la phrase sort des formes connues : le LLM (ou un message du mode dégradé)
prend alors le relais. Les paramètres masqués (pièce, appareil, jour) sont déduits ensuite par l'agent, comme pour
le LLM, et tout passe par le Core (validation, permissions, confirmation) : ce module ne fait que proposer.
"""

from __future__ import annotations

import re

from jarvis.personality import normalize
from jarvis.scheduling.actions import CLOCK_WORDS
from jarvis.scheduling.clock import parse_clock
from jarvis.scheduling.durations import DurationError, parse_duration, tokens
from jarvis.tools.base import ToolError
from jarvis.tools.registry import ToolRegistry

# Demandes conditionnelles, récurrentes ou négatives : jamais traitées ici (le sens dépasse une commande simple).
DISQUALIFIERS = re.compile(r"\b(quand|lorsque|si|sauf|chaque|tous les|toutes les|ne|n|pas|jamais|pourquoi|comment)\b")
# « et demie », « et quart », « vingt et un » ne séparent pas deux commandes.
SPLIT = re.compile(r"\s*(?:,|;|\bet puis\b|\bet\b(?!\s+(?:demie?|quart|une?)\b)|\bpuis\b|\bensuite\b)\s*",
                   re.IGNORECASE)

LIGHT_WORDS = ("lumiere", "lumieres", "lampe", "lampes", "ampoule", "ampoules", "eclairage", "luminosite")
ON_VERBS = ("allume", "allumer", "rallume", "rallumer", "allumes")
OFF_VERBS = ("eteins", "eteindre", "eteint", "eteignez", "coupe la lumiere", "coupe les lumieres")
OPEN_VERBS = ("ouvre", "ouvrir", "lance", "lancer", "demarre", "demarrer", "ouvre moi", "lance moi")
CLOSE_VERBS = ("ferme", "fermer", "quitte", "quitter", "ferme moi")
SOUND_WORDS = ("son", "volume")
MUTE = ("coupe le son", "coupe le volume", "mets en sourdine", "sourdine", "mute", "coupe moi le son")
UNMUTE = ("remets le son", "remet le son", "reactive le son", "rallume le son", "enleve la sourdine",
          "retire la sourdine", "remets moi le son")
TIMER_WORDS = ("minuteur", "timer", "minuterie", "compte a rebours")
REMINDER_WORDS = ("rappelle moi", "rappelez moi", "fais moi penser", "previens moi", "rappelle nous")
ALARM_WORDS = ("reveille moi", "reveillez moi", "mets un reveil", "mets moi un reveil", "programme un reveil",
               "regle un reveil", "un reveil a", "une alarme a")
WEATHER_WORDS = ("meteo", "quel temps", "temps fait il", "temps fera t il", "il fait combien", "pleuvoir", "pleut",
                 "va t il neiger", "parapluie")
LOCK_WORDS = ("verrouille", "verrouiller", "verrouillage")
REMEMBER = re.compile(r"^(?:jarvis\s+)?(?:retiens|retenez|souviens toi|souvenez vous|rappelle toi|note|memorise|"
                      r"n oublie pas|n oubliez pas)(?: bien)? (?:que|qu)\s+(.+)$")
RECALL = ("que sais tu de moi", "que sais tu sur moi", "qu est ce que tu sais de moi", "qu est ce que tu sais sur moi",
          "tu sais quoi sur moi", "qu as tu retenu", "qu est ce que tu as retenu", "que t ai je demande de retenir")
FORGET = re.compile(r"^(?:jarvis\s+)?(?:oublie|oubliez|efface)\s+(?:que |qu |ce que )?(.+)$")
STATUS_WORDS = ("est allumee", "est eteinte", "sont allumees", "sont eteintes", "est elle allumee",
                "est elle eteinte", "est a combien", "etat de la lumiere", "etat des lumieres")
SCENE_LEADS = ("mode", "ambiance", "scene", "en mode", "en ambiance")
COLOR_SYNONYMS = {"lumiere chaude": "blanc chaud", "chaude": "blanc chaud", "chaud": "blanc chaud",
                  "lumiere froide": "blanc froid", "froide": "blanc froid", "froid": "blanc froid",
                  "blanche": "blanc", "bleue": "bleu", "verte": "vert", "rouge": "rouge", "jaune": "jaune",
                  "violette": "violet"}
LEVEL_WORDS = ((("au maximum", "au max", "a fond", "a 100"), 100), (("au minimum", "au min"), 1),
               (("a moitie", "a la moitie", "a mi puissance"), 50))
PERCENT = re.compile(r"\b(\d{1,3})\s*(?:%|pour ?cent|pourcent|pourcents)?(?:\s|$)")
KELVIN = re.compile(r"\b(\d{4})\s*k(?:elvins?)?\b")
EDGE_FILLERS = {"un", "une", "de", "d", "pour", "dans", "minuteur", "timer", "et", "pendant", "environ"}
DAYS = (("apres demain", "day_after_tomorrow"), ("demain", "tomorrow"))
MOMENTS = (("ce matin", "morning"), ("demain matin", "morning"), ("matin", "morning"),
           ("cet apres midi", "afternoon"), ("apres midi", "afternoon"), ("ce soir", "evening"),
           ("soir", "evening"), ("soiree", "evening"), ("toute la journee", "day"), ("la journee", "day"))


def _has(norm: str, phrases) -> bool:
    padded = f" {norm} "
    return any(f" {p} " in padded for p in phrases)


def _percent(norm: str) -> int | None:
    for words, value in LEVEL_WORDS:
        if _has(norm, words):
            return value
    match = re.search(r"\b(\d{1,3})\s*(?:pour ?cent|pourcents?)\b", norm) or re.search(r"\ba (\d{1,3})\b", norm) \
        or re.search(r"\b(\d{1,3})$", norm)
    if match and 0 <= int(match.group(1)) <= 100:
        return int(match.group(1))
    return None


def _percent_raw(text: str) -> int | None:
    """« 30 % » écrit avec le signe (que normalize retire) ou dit (« 30 pour cent »)."""
    match = re.search(r"(\d{1,3})\s*%", text)
    if match and 0 <= int(match.group(1)) <= 100:
        return int(match.group(1))
    return _percent(normalize(text))


def _colour(norm: str, colours: tuple[str, ...]) -> str | None:
    """Couleur dite, en préférant la plus longue (« blanc chaud » avant « blanc »)."""
    padded = f" {norm} "
    for name in sorted(colours, key=len, reverse=True):
        if f" en {normalize(name)} " in padded or f" {normalize(name)} " in padded and normalize(name).count(" "):
            return name
    for said, name in sorted(COLOR_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if name in colours and (f" en {said} " in padded or f" une {said} " in padded or f" {said} " in padded
                                and said.startswith("lumiere")):
            return name
    return None


def _duration_phrase(text: str, after: tuple[str, ...] = ()) -> tuple[str, int, int] | None:
    """Plus longue suite de mots formant une durée (après l'un des mots ``after`` s'il est donné) :
    (durée telle qu'elle est dite, indice du premier mot, indice après le dernier) dans ``tokens(text)``."""
    words = tokens(text)
    best = None
    for start in range(len(words)):
        if after and (start == 0 or words[start - 1] not in after):
            continue
        for end in range(min(len(words), start + 8), start, -1):
            try:
                seconds = parse_duration(" ".join(words[start:end]))
            except DurationError:
                continue
            if best is None or end - start > best[1] - best[0]:
                best = (start, end, seconds)
            break
    if best is None:
        return None
    start, end, seconds = best

    def same(a: int, b: int) -> bool:
        try:
            return parse_duration(" ".join(words[a:b])) == seconds
        except DurationError:
            return False

    while end - start > 1 and words[start] in EDGE_FILLERS and same(start + 1, end):  # « un minuteur de 10 minutes »
        start += 1
    while end - start > 1 and words[end - 1] in EDGE_FILLERS and same(start, end - 1):
        end -= 1
    return " ".join(words[start:end]), start, best[1]


class QuickPlanner:
    """Analyse déterministe ; ``registry`` donne les outils disponibles et leurs contrôles (applications autorisées,
    couleurs, pièces)."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def _exists(self, name: str) -> bool:
        return self._registry.exists(name)

    def _checked(self, tool: str, param: str, value):
        """Valeur acceptée par le contrôle du paramètre (application autorisée...), sinon None."""
        spec = self._registry.get(tool).parameters[param]
        if spec.choices and value not in spec.choices:
            return None
        try:
            return spec.check(value) if spec.check else value
        except ToolError:
            return None

    def _names_room(self, text: str) -> bool:
        if not self._exists("light_on"):
            return False
        room = self._registry.get("light_on").parameters.get("room")
        return bool(room and room.resolve and room.resolve(text) != room.resolve(""))

    def _about_lights(self, norm: str, text: str) -> bool:
        return self._exists("light_on") and (_has(norm, LIGHT_WORDS) or self._names_room(text)
                                             or self._scene(norm) is not None)

    # --- Une commande --------------------------------------------------------------------------------

    def one(self, text: str, previous: dict | None = None) -> dict | None:
        """Une commande simple, ou None. ``previous`` : action précédente d'une demande enchaînée (« ..., en bleu »
        hérite des lumières)."""
        norm = normalize(text)
        memory = self._memory(norm, text) if norm else None
        if memory is None and (not norm or DISQUALIFIERS.search(norm)):
            return None
        if memory is not None:
            return {"type": "tool_call", "tool": memory[0], "parameters": memory[1]}
        for parse in (self._routine, self._sound, self._lights, self._apps, self._timer, self._reminder, self._alarm,
                      self._weather, self._lock):
            call = parse(norm, text, previous)
            if call is not None:
                return {"type": "tool_call", "tool": call[0], "parameters": call[1]}
        return None

    def _memory(self, norm, text):
        """« Retiens que ... », « Que sais-tu de moi ? », « Oublie que ... » (avant les règles qui excluent « ne »)."""
        if not self._exists("remember"):
            return None
        if _has(norm, RECALL):
            return "recall", {}
        for pattern, tool, key in ((REMEMBER, "remember", "fact"), (FORGET, "forget", "topic")):
            match = pattern.match(norm)
            if match and self._exists(tool):
                value = _original_tail(text, tokens(match.group(1)))
                return (tool, {key: value}) if value else None
        return None

    def _routine(self, norm, text, previous):
        """« Quelles routines sont programmées ? », « Lance la routine soir », « Supprime la routine réveil »."""
        if not (_has(norm, ("routine", "routines")) and self._exists("list_routines")):
            return None
        match = re.search(r"\b(lance|lancer|demarre|execute|joue|supprime|supprimer|efface|annule)\b.*?\broutine\s+(.+)$",
                          norm)
        if match:
            tool = "run_routine" if match.group(1) in ("lance", "lancer", "demarre", "execute", "joue") else "delete_routine"
            name = _original_tail(text, tokens(match.group(2)))
            return (tool, {"name": name}) if name and self._exists(tool) else None
        if _has(norm, ("quelles", "quels", "liste", "mes routines", "les routines", "programmees", "programme")):
            return "list_routines", {}
        return None

    def _sound(self, norm, text, previous):
        if _has(norm, UNMUTE) and self._exists("unmute_volume"):
            return "unmute_volume", {}
        if _has(norm, MUTE) and self._exists("mute_volume"):
            return "mute_volume", {}
        if (_has(norm, SOUND_WORDS) or (previous or {}).get("tool") == "set_volume") and self._exists("set_volume"):
            if self._about_lights(norm, text):
                return None
            level = _percent_raw(text)
            if level is not None:
                return "set_volume", {"volume": level}
        return None

    def _lights(self, norm, text, previous):
        inherited = (previous or {}).get("tool", "").startswith(("light_", "set_color", "set_brightness"))
        if not (self._about_lights(norm, text) or inherited and not _has(norm, SOUND_WORDS)):
            return None
        if _has(norm, STATUS_WORDS) and self._exists("light_status"):
            return "light_status", {}
        if _has(norm, OFF_VERBS):
            return "light_off", {}
        scene = self._scene(norm)
        if scene:
            return "set_scene", {"scene": scene}
        kelvin = KELVIN.search(norm)
        if kelvin and self._exists("set_color_temperature"):
            return "set_color_temperature", {"temperature": int(kelvin.group(1))}
        colours = self._registry.get("set_color").parameters["color"].choices if self._exists("set_color") else ()
        colour = _colour(norm, colours)
        if colour:
            return "set_color", {"color": colour}
        level = _percent_raw(text)
        if level is not None and level >= 1:
            if _has(norm, ON_VERBS):
                return "light_on", {"brightness": level}
            return ("set_brightness", {"brightness": level}) if self._exists("set_brightness") else None
        if _has(norm, ON_VERBS):
            return "light_on", {}
        if inherited and self._names_room(text) and len(norm.split()) <= 4:
            return previous["tool"], {k: v for k, v in previous.get("parameters", {}).items() if k != "room"}
        return None

    def _scene(self, norm: str) -> str | None:
        """Ambiance dite après « mode », « ambiance » ou « scène » (« mets les lumières en mode cinéma »)."""
        if not self._exists("set_scene"):
            return None
        names = self._registry.get("set_scene").parameters["scene"].choices
        padded = f" {norm} "
        for lead in SCENE_LEADS:
            for name in names:
                if f" {lead} {name} " in padded:
                    return name
        return None

    def _apps(self, norm, text, previous):
        for verbs, tool in ((OPEN_VERBS, "open_application"), (CLOSE_VERBS, "close_application")):
            if not self._exists(tool):
                continue
            for verb in sorted(verbs, key=len, reverse=True):
                match = re.search(rf"(?:^| ){verb} (?:moi )?(?:l |le |la |les )?(.+)$", norm)
                if not match:
                    continue
                name = match.group(1).strip()
                for candidate in (name, name.split()[0] if name else ""):
                    if candidate and self._checked(tool, "application", candidate) is not None:
                        return tool, {"application": candidate}
        return None

    def _timer(self, norm, text, previous):
        if not (_has(norm, TIMER_WORDS) and self._exists("create_timer")):
            return None
        if _has(norm, ("annule", "annuler", "arrete", "stoppe", "supprime")) and self._exists("cancel_timer"):
            found = _duration_phrase(text)
            return "cancel_timer", ({"duration": found[0]} if found else {})
        if _has(norm, ("quel", "quels", "combien", "reste", "liste")) and self._exists("list_timers"):
            return "list_timers", {}
        found = _duration_phrase(text)
        return ("create_timer", {"duration": found[0]}) if found else None

    def _reminder(self, norm, text, previous):
        if not (_has(norm, REMINDER_WORDS) and self._exists("create_reminder")):
            return None
        found = _duration_phrase(text, after=("dans",))
        if not found:
            return self._reminder_at(text)
        rest = tokens(text)[found[2]:]
        while rest and rest[0] in ("de", "d", "que", "qu", "pour", "a", "il", "faut"):
            rest = rest[1:]
        message = _original_tail(text, rest)
        return ("create_reminder", {"delay": found[0], "message": message}) if message else None

    def _reminder_at(self, text: str):
        """« Rappelle-moi à 18 h 30 d'appeler Paul » : heure précise puis message."""
        words = tokens(text)
        for start in range(len(words)):
            if words[start] != "a":
                continue
            for end in range(min(len(words), start + 7), start + 1, -1):
                segment = " ".join(words[start + 1:end])
                if parse_clock(segment) is None or not all(w.isdigit() or w in CLOCK_WORDS for w in words[start + 1:end]):
                    continue
                rest = words[end:]
                while rest and rest[0] in ("de", "d", "que", "qu", "pour", "il", "faut"):
                    rest = rest[1:]
                message = _original_tail(text, rest)
                if message:
                    hour, minute = parse_clock(segment)
                    return "create_reminder", {"time": f"{hour} heures {minute}" if minute else f"{hour} heures",
                                               "message": message}
        return None

    def _alarm(self, norm, text, previous):
        if not (_has(norm, ALARM_WORDS) and self._exists("create_alarm")):
            return None
        clock = parse_clock(text)
        if clock is None:
            return None
        hour, minute = clock
        return "create_alarm", {"time": f"{hour} heures {minute}" if minute else f"{hour} heures"}

    def _weather(self, norm, text, previous):
        if not (_has(norm, WEATHER_WORDS) and self._exists("get_weather")):
            return None
        params = {}
        day = next((value for word, value in DAYS if _has(norm, (word,))), None)
        if day:
            params["day"] = day
        moment = next((value for word, value in MOMENTS if _has(norm, (word,))), None)
        if moment:
            params["moment"] = moment
        from jarvis.weather.cities import mentioned_city

        city = mentioned_city(text)
        if city:
            params["location"] = city
        return "get_weather", params

    def _lock(self, norm, text, previous):
        if _has(norm, LOCK_WORDS) and self._exists("lock_pc") and \
                _has(norm, ("pc", "ordinateur", "ordi", "session", "ecran", "poste")):
            return "lock_pc", {}
        return None

    # --- Demande entière -----------------------------------------------------------------------------

    def plan(self, text: str, ignored: tuple[str, ...] = ("jarvis",)) -> dict | None:
        """Demande entière : une commande, ou plusieurs enchaînées (« allume la chambre et éteins l'entrée »).
        Une demande enchaînée dont une partie n'est pas reconnue est laissée au LLM : jamais d'action à moitié
        devinée (sauf un rappel, dont le message peut contenir « et »)."""
        names = {normalize(n) for n in ignored}
        segments = [s for s in SPLIT.split(text) if normalize(s) and normalize(s) not in names]
        if not segments:
            return None
        if len(segments) == 1:
            return self.one(segments[0])
        if len(segments) > 4:
            return None
        calls, previous = [], None
        for segment in segments:
            call = self.one(segment, previous)
            if call is None:
                break
            calls.append({**call, "segment": segment.strip()})
            previous = call
        if len(calls) == len(segments):
            return {"type": "tool_calls", "calls": calls}
        if calls and calls[0]["tool"] == "create_reminder":
            return self.one(text)
        return None


def _original_tail(text: str, wanted: list[str]) -> str:
    """Fin de la phrase d'origine (accents, majuscules) qui correspond aux mots normalisés ``wanted``."""
    if not wanted:
        return ""
    for match in re.finditer(r"[\w-]+", text):
        if tokens(text[match.start():]) == wanted:
            return text[match.start():].strip(" .?!")
    return " ".join(wanted)


def quick_plan(text: str, registry: ToolRegistry, ignored: tuple[str, ...] = ("jarvis",)) -> dict | None:
    return QuickPlanner(registry).plan(text, ignored)
