"""Contexte conversationnel temporaire : comprendre les compléments courts d'une demande d'outil.

« Allume la lumière. » -> « Dans la chambre. » -> « À 30 %. » -> « Non, l'entrée. » -> « En bleu. »
« Quelle heure est-il ? » -> « Et demain ? » (la date de demain)
« Quel temps fait-il ? » -> « Et demain ? » / « Et à Lyon ? »

Seule la dernière action réussie de la conversation est gardée, en mémoire, et oubliée au retour en veille : ce
n'est pas une mémoire (voir jarvis.memory). Un complément n'est compris que s'il est court et ne contient qu'une
pièce, un pourcentage, une couleur, une ambiance ou un jour ; tout le reste suit le chemin normal. L'action
produite est une demande d'outil ordinaire, validée par le Core comme les autres.
"""

from __future__ import annotations

import re

from jarvis.personality import normalize
from jarvis.tools.quick import QuickPlanner, _has, _percent_raw
from jarvis.tools.registry import ToolRegistry

MAX_WORDS = 6
LEADS = re.compile(r"^(?:(?:non|non non|plutot|pardon|en fait|mais|et|et puis|alors|euh|ah|oui|ok|d accord)\s+)+")
FILLERS = {"dans", "le", "la", "les", "l", "de", "du", "des", "a", "au", "aux", "en", "pour", "sur", "s", "il", "te",
           "plait", "stp", "svp", "jarvis", "monsieur", "ma", "mon", "mes", "notre", "celle", "celle de", "ci", "mets", "met", "mettez", "aussi", "plutot", "lumiere",
           "lumieres", "lampe", "pourcent", "pour", "cent", "mode", "ambiance", "scene", "et", "puis"}
LIGHTS = ("light_on", "light_off", "light_toggle", "set_brightness", "set_color", "set_color_temperature",
          "set_scene", "light_status")
SOUND = ("set_volume", "mute_volume", "unmute_volume")
MUSIC = ("spotify_play", "spotify_pause", "spotify_next", "spotify_previous", "spotify_volume")
ON_OFF = {"allume": "light_on", "rallume": "light_on", "allumes": "light_on", "eteins": "light_off",
          "eteint": "light_off", "reteins": "light_off", "coupe": "light_off"}
OTHER_ONE = {"l autre", "l autre aussi", "et l autre", "l autre lumiere", "l autre piece", "l autre aussi stp"}
WRONG_ONE = {"pas celle la", "pas celle ci", "pas celle la l autre", "l autre plutot", "pas la bonne", "c est l autre",
             "pas celle la plutot l autre", "je voulais dire l autre", "mauvaise piece"}
DAY_WORDS = {"demain": "tomorrow", "apres demain": "day_after_tomorrow", "aujourd hui": "today", "hier": "yesterday"}
WEATHER_DAYS = {"demain": "tomorrow", "apres demain": "day_after_tomorrow", "aujourd hui": "today"}


class ConversationContext:
    def __init__(self, registry: ToolRegistry, assistant_name: str = "JARVIS"):
        self._registry = registry
        self._quick = QuickPlanner(registry)
        self._name = normalize(assistant_name)
        self.last: dict | None = None  # {"tool", "parameters"} de la dernière action réussie

    def clear(self) -> None:
        self.last = None

    def record(self, tool: str, parameters: dict) -> None:
        self.last = {"tool": tool, "parameters": dict(parameters)}

    # --- Analyse -------------------------------------------------------------------------------------

    def _room_param(self, tool: str):
        if not self._registry.exists(tool):
            return None
        param = self._registry.get(tool).parameters.get("room")
        return param if param is not None and param.resolve is not None else None

    def _named_room(self, text: str) -> str | None:
        param = self._room_param("light_on")
        if param is None:
            return None
        found = param.resolve(text)
        return found if found != param.resolve("") else None

    def _room_words(self, rest: str) -> set[str]:
        """Mots du complément qui désignent à eux seuls une pièce (« entree », « chambre », « couloir »)."""
        param = self._room_param("light_on")
        nothing = param.resolve("")
        return {w for w in rest.split() if param.resolve(w) != nothing}

    def resolve(self, text: str) -> dict | None:
        """Demande d'outil complète pour un complément court, ou None."""
        if self.last is None:
            return None
        norm = normalize(text)
        norm = re.sub(rf"^{re.escape(self._name)}\s+", "", norm) if self._name else norm
        rest = LEADS.sub("", norm).strip()
        if not rest or len(rest.split()) > MAX_WORDS:
            return None
        tool, previous = self.last["tool"], self.last["parameters"]
        call = None
        if tool in ("get_time", "get_date"):
            call = self._day(rest, "get_date", DAY_WORDS)
        elif tool == "get_weather":
            call = self._day(rest, "get_weather", WEATHER_DAYS, previous)
        elif tool in LIGHTS:
            call = self._light(rest, text, tool, previous)
        elif tool in SOUND:
            level = _percent_raw(text)
            if level is not None and self._only(rest, {str(level)}):
                call = {"tool": "set_volume", "parameters": {"volume": level}}
        elif tool in MUSIC:
            # Musique en cours par Spotify : « mets le volume à 30 », « baisse à 20 % » visent le volume de Spotify.
            level = _percent_raw(text)
            if level is not None and 0 <= level <= 100 and self._only(rest, {str(level), "volume", "son", "baisse",
                                                                              "monte", "le", "au", "a"}):
                call = {"tool": "spotify_volume", "parameters": {"volume": level}}
        if call is None:
            return None
        if call.get("type") == "tool_calls":
            return call if all(self._registry.exists(c["tool"]) for c in call["calls"]) else None
        if not self._registry.exists(call["tool"]):
            return None
        return {"type": "tool_call", **call}

    def _only(self, rest: str, allowed: set[str]) -> bool:
        """Le complément ne contient que des mots vides et ``allowed`` (pas de nouveau verbe ni de nouvel objet)."""
        words = rest.split()
        allowed_words = {w for phrase in allowed for w in normalize(phrase).split()}
        return all(w in FILLERS or w in allowed_words for w in words)

    def _day(self, rest: str, tool: str, days: dict, previous: dict | None = None) -> dict | None:
        for said, day in sorted(days.items(), key=lambda kv: -len(kv[0])):
            if _has(rest, (said,)) and self._only(rest, {said, "quel jour", "sera", "serons", "on", "sera t"}):
                parameters = {k: v for k, v in (previous or {}).items() if k in ("location",)}
                if tool == "get_date":
                    return {"tool": tool, "parameters": {"day": day}}
                return {"tool": tool, "parameters": {**parameters, "day": day}}
        return None

    def _rooms(self):
        """Pièces des lumières (objet Rooms derrière le paramètre « room »), ou None."""
        param = self._room_param("light_on")
        return getattr(getattr(param, "check", None), "__self__", None) if param is not None else None

    def _other(self, rest: str, tool: str, previous: dict, visible: dict) -> dict | None:
        """« L'autre. » : même action sur l'autre pièce ; « Pas celle-là. » : annule l'allumage ou l'extinction de la
        pièce précédente puis fait l'action sur l'autre. Seulement quand « l'autre » est sans ambiguïté (deux pièces)."""
        switch = rest in WRONG_ONE
        if not switch and rest not in OTHER_ONE:
            return None
        rooms = self._rooms()
        keys = [r.key for r in rooms.rooms()] if rooms is not None else []
        current = previous.get("room")
        others = [k for k in keys if k != current]
        if current not in keys or len(others) != 1:
            return None
        calls = []
        if switch and tool in ("light_on", "light_off"):
            undo = "light_off" if tool == "light_on" else "light_on"
            calls.append({"tool": undo, "parameters": {"room": current}, "segment": rest})
        calls.append({"tool": tool, "parameters": {**visible, "room": others[0]}, "segment": rest})
        if len(calls) == 1:
            return {"tool": tool, "parameters": calls[0]["parameters"]}
        return {"type": "tool_calls", "calls": calls}

    def _light(self, rest: str, text: str, tool: str, previous: dict) -> dict | None:
        room = self._named_room(text)
        visible = {k: v for k, v in previous.items() if k != "room"}
        same_room = {"room": room} if room else ({"room": previous["room"]} if "room" in previous else {})
        room_words = (self._room_words(rest) | set(room[1:].split()) if room.startswith("?") else self._room_words(rest))             if room else set()
        other = self._other(rest, tool, previous, visible)
        if other is not None:
            return other
        verb = rest.split()[0] if rest.split() else ""
        if verb in ON_OFF and self._only(" ".join(rest.split()[1:]), {*room_words, "le", "la", "les", "aussi"}):
            # « Non, éteins. », « Rallume. », « Éteins-la » : même pièce que l'action précédente
            return {"tool": ON_OFF[verb], "parameters": same_room}
        level = _percent_raw(text)
        if level is not None and level >= 1 and self._only(rest, {str(level), *room_words}):
            return {"tool": "set_brightness", "parameters": {**same_room, "brightness": level}}
        call = self._quick.one(text, {"tool": tool, "parameters": previous})
        if call is not None and call["tool"] in ("set_color", "set_scene", "set_color_temperature") \
                and len(rest.split()) <= 4:
            own = {k: v for k, v in call["parameters"].items() if k != "room"}
            return {"tool": call["tool"], "parameters": {**same_room, **own}}
        if room and self._only(rest, room_words):
            return {"tool": tool, "parameters": {**visible, "room": room}}
        return None
