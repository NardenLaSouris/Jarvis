"""Commandes simples reconnues sans LLM (déterministe) : lumières, son, applications, minuteurs, rappels, réveils,
météo, verrouillage.

Deux usages :
- mode dégradé : le worker LLM (Katana) est hors ligne, ORION exécute quand même les commandes simples ;
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
RUNNING_APPS = ("applications ouvertes", "application ouverte", "applications sont ouvertes", "programmes ouverts",
                "programmes sont ouverts", "qu est ce qui tourne", "qu est ce qui est ouvert", "applications en cours",
                "logiciels ouverts", "fenetres ouvertes", "quelles applications", "quels programmes")
MUSIC_STATUS = ("est ce que la musique joue", "la musique joue", "qu est ce qui joue", "c est quoi cette chanson",
                "c est quoi ce morceau", "quel est ce morceau", "quelle est cette chanson", "quel morceau joue",
                "quelle chanson joue", "qu est ce que tu joues", "qu est ce qu on ecoute", "c est quoi ce titre",
                "quel est le titre", "spotify joue", "la musique est en pause", "est ce que spotify")
FACE_WORDS = ("visage", "ton visage", "theme", "ton theme", "ta couleur", "tes couleurs", "ton interface",
              "ton ecran", "ta face", "ta tete", "habille toi", "change de couleur", "couleur de jarvis",
              "arc en ciel", "en arc en ciel", "toutes les couleurs")
PRESENCE_QUESTIONS = ("qui est a la maison", "quelqu un a la maison", "la maison est vide", "la maison est elle vide",
                      "qui est la", "qui est present", "qui est rentre", "est ce que je suis a la maison")
# Consultation des mails : sans LLM (rien n'y change dans la boîte). Lire un mail précis passe par le LLM.
MAIL_CHECK = ("j ai des mails", "j ai des nouveaux mails", "j ai du courrier", "j ai recu des mails", "des nouveaux mails",
              "nouveaux mails", "mes mails", "mes emails", "mes e mails", "ma boite mail", "ma boite de reception",
              "des mails", "un mail", "nouveau mail")
MAIL_LIST = ("liste mes mails", "liste les mails", "mes derniers mails", "les derniers mails", "mes mails recents")
# Une demande qui agit sur un mail ou en désigne un va au LLM (et aux confirmations), jamais à la consultation.
MAIL_ACTIONS = ("envoie", "envoyer", "ecris", "ecrire", "reponds", "repondre", "supprime", "efface", "archive",
                "marque", "lis le", "lis moi le", "lis la", "resume", "cherche", "trouve", "de la part", "premier",
                "deuxieme", "dernier", "troisieme", "transfere")
# « Réponds-lui que je suis d'accord » : le texte dit, mot pour mot (jamais reformulé), au mail lu en dernier.
REPLY = re.compile(r"^(?:jarvis\s+)?(?:reponds|repondez|repond)(?:\s+(?:lui|leur|moi))?"
                   r"(?:\s+(?:a ce mail|au mail|a ce message|a ce courriel))?\s+(?:que|qu|:)\s+(.+)$")
MAIL_IMPORTANT = ("mails importants", "mail important", "emails importants")
TIMER_WORDS = ("minuteur", "minuteurs", "timer", "timers", "minuterie", "compte a rebours")
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


NEGATIVE = re.compile(r"(?:\bmoins\s+|-\s*)\d")


def _percent(norm: str) -> int | None:
    if NEGATIVE.search(norm):  # « moins 10 » : pas un niveau (jamais lu comme 10)
        return None
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
    if NEGATIVE.search(text.lower()):
        return None
    match = re.search(r"(\d{1,3})\s*%", text)
    if match and 0 <= int(match.group(1)) <= 100:
        return int(match.group(1))
    return _percent(normalize(text))


PLACE_NAME = re.compile(r"\b(?:à|a|au|aux|en|sur|pour|vers)\s+((?:New |Los |Las |San |Saint-|Sainte-|Le |La |Les )?"
                        r"[A-ZÀÂÉÈÊÎÔÛÇ][\w'’-]{1,30}(?:[ -][A-ZÀÂÉÈÊÎÔÛÇ][\w'’-]{1,30})?)")


def _named_place(text: str) -> str | None:
    """Ville hors de la liste française, dite avec sa majuscule (« à Tokyo », « à New York ») : transmise telle
    quelle au service météo (qui la cherche, ou dit qu'il ne la trouve pas), plutôt que la ville par défaut sans
    prévenir (session QA : « la météo à Tokyo » donnait celle de Nantes)."""
    match = PLACE_NAME.search(text)
    if match is None:
        return None
    place = match.group(1).strip()
    return None if normalize(place) in ("jarvis", "monsieur") else place


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
        everything = re.match(r"^(?:eteins|eteint|eteindre|allume|allumer|rallume|coupe) (?:moi )?tout$", norm)
        return self._exists("light_on") and (_has(norm, LIGHT_WORDS) or self._names_room(text)
                                             or self._scene(norm) is not None or bool(everything))

    # --- Une commande --------------------------------------------------------------------------------

    def one(self, text: str, previous: dict | None = None) -> dict | None:
        """Une commande simple, ou None. ``previous`` : action précédente d'une demande enchaînée (« ..., en bleu »
        hérite des lumières)."""
        norm = normalize(text)
        combined = self._spotify_with_volume(norm, text) if norm and self._exists("spotify_play") else None
        if combined is not None:
            return combined
        memory = self._memory(norm, text) if norm else None
        if memory is None and (not norm or DISQUALIFIERS.search(norm)):
            return None
        if memory is not None:
            return {"type": "tool_call", "tool": memory[0], "parameters": memory[1]}
        parsers = (self._media, self._calendar, self._routine, self._face, self._sound, self._lights, self._apps,
                   self._timer, self._reminder, self._alarm, self._weather, self._lock, self._spotify)
        # Un rappel ou un réveil d'abord : son message peut nommer n'importe quoi (« rappelle-moi demain d'appeler le
        # garage », « ... d'éteindre la chambre »), qui ne doit jamais être exécuté tout de suite.
        if _has(norm, REMINDER_WORDS):
            parsers = (self._reminder, self._calendar)
        elif _has(norm, ALARM_WORDS):
            parsers = (self._alarm,)
        elif _has(norm, TIMER_WORDS):
            parsers = (self._timer,)
        for parse in parsers:
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

    def _media(self, norm, text, previous):
        """« Pause », « Reprends la musique », « Morceau suivant », « Chanson précédente »."""
        if self._exists("spotify_status") and _has(norm, MUSIC_STATUS):
            return "spotify_status", {}
        if self._exists("presence_status") and _has(norm, PRESENCE_QUESTIONS):
            return "presence_status", {}
        if self._exists("check_mail") and not _has(norm, MAIL_ACTIONS):
            if _has(norm, MAIL_IMPORTANT):
                return "list_mail", {"important_only": True}
            if _has(norm, MAIL_LIST):
                return "list_mail", {}
            if _has(norm, MAIL_CHECK):
                return "check_mail", {}
        spotify = self._exists("spotify_pause")
        if not (spotify or self._exists("media_play_pause")):
            return None
        if norm in ("pause", "mets pause", "mets en pause", "pause la musique", "mets la musique en pause",
                    "coupe la musique", "pause musique") or _has(norm, ("musique en pause", "spotify en pause")):
            return ("spotify_pause", {}) if spotify else ("media_play_pause", {})
        if _has(norm, ("reprends la musique", "reprend la musique", "remets la musique", "relance la musique",
                       "reprends la lecture", "reprend la lecture")) or norm in (
                    "reprends", "lecture", "play", "joue spotify", "mets spotify", "joue la musique", "mets la musique",
                    "mets de la musique", "joue de la musique", "remets spotify", "relance spotify"):
            return ("spotify_play", {}) if self._exists("spotify_play") else ("media_play_pause", {})
        if _has(norm, ("morceau suivant", "chanson suivante", "titre suivant", "piste suivante", "musique suivante",
                       "passe au suivant", "chanson d apres", "morceau d apres")):
            return ("spotify_next", {}) if spotify else ("media_next", {})
        if _has(norm, ("morceau precedent", "chanson precedente", "titre precedent", "piste precedente",
                       "chanson d avant", "morceau d avant", "reviens en arriere")):
            return ("spotify_previous", {}) if spotify else ("media_previous", {})
        return None

    def _spotify_with_volume(self, norm: str, text: str):
        """« Mets Back in Black d'AC/DC en volume 30 » : le morceau, puis le volume de Spotify (pas celui du PC)."""
        match = re.search(r"\s+(?:en volume|au volume|volume|a volume|avec le volume a|le volume a|et le volume a|"
                          r"et mets le volume a|et le son a|au son)\s+(?:a\s+)?(\d{1,3})(?:\s*(?:%|pour ?cent|pourcents?))?\W*$",
                          text, re.IGNORECASE)
        norm_match = re.search(r"\s(?:en volume|au volume|volume|le volume a|et le son a|au son)\s+(?:a\s+)?(\d{1,3})"
                               r"(?:\s+pour ?cent)?$", norm)
        if not (match and norm_match and self._exists("spotify_volume")) or int(norm_match.group(1)) > 100:
            return None
        play = self._spotify(normalize(text[:match.start()]), text[:match.start()], None)
        if play is None:
            return None
        volume = int(norm_match.group(1))
        return {"type": "tool_calls", "calls": [
            {"type": "tool_call", "tool": play[0], "parameters": play[1], "segment": text[:match.start()].strip()},
            {"type": "tool_call", "tool": "spotify_volume", "parameters": {"volume": volume},
             "segment": text[match.start():].strip(" .")}]}

    def _spotify(self, norm, text, previous):
        """« Mets Without Me d'Eminem », « Joue la playlist chill », « Mets de la musique de Daft Punk » ; « Mais » en
        tête (« mets » mal transcrit) est accepté pour cette forme seulement (l'agent répond normalement si Spotify ne
        trouve rien de cohérent)."""
        if not self._exists("spotify_play"):
            return None
        match = re.match(r"^(?:mets|met|mettez|mais|joue|jouer|lance|passe|ecoute|fais ecouter)(?: moi| nous)? (.+)$", norm)
        if not match:
            return None
        rest = re.sub(r"\b(sur spotify|s il te plait|s il vous plait|stp|svp)\b", " ", match.group(1)).strip()
        music = _has(norm, ("sur spotify", "chanson", "morceau", "titre", "album", "playlist", "musique", "du son"))
        if not music and not re.search(r"\b(de|d)\b", rest) and not rest.startswith("du "):
            return None
        kind, lead = "track", r"^(?:la |le |l )?(?:chanson |morceau |titre |son )?"
        if re.search(r"\bplaylist\b", rest):
            kind, lead = "playlist", r"^(?:ma |la |une )?playlist "
        elif re.search(r"\balbum\b", rest):
            kind, lead = "album", r"^(?:l |un )?album "
        elif re.match(r"^(?:de la musique de|un peu de|la musique de|du son de|des chansons de|une chanson de|un morceau de|un titre de|un son de|du) ", rest) \
                and not (rest.startswith("du ") and re.search(r"\s(?:de|d)\s", rest[3:])):  # « Du hast de Rammstein »
            kind, lead = "artist", r"^(?:de la musique de|un peu de|la musique de|du son de|des chansons de|une chanson de|un morceau de|un titre de|un son de|du) "
        rest = re.sub(lead, "", rest).strip()
        if not rest or len(rest.split()) > 10:
            return None
        query = _original_tail(text.replace("sur Spotify", "").replace("sur spotify", ""), tokens(rest)) or rest
        return "spotify_play", {"query": query.strip(" .?!"), "kind": kind}

    def _calendar(self, norm, text, previous):
        """« Qu'est-ce que j'ai de prévu demain ? », « Mes prochains rendez-vous », « Suis-je libre demain ? »."""
        if not self._exists("list_events"):
            return None
        if _has(norm, ("suis je libre", "je suis libre", "creneau libre", "creneaux libres", "des creneaux")):
            return "free_slots", {}
        if _has(norm, ("prochains rendez vous", "prochain rendez vous", "prochains evenements", "prochain evenement")):
            return "next_events", {}
        if _has(norm, ("de prevu", "mon agenda", "mon calendrier", "mon planning", "dans l agenda", "j ai quoi",
                       "mes rendez vous", "programme de la journee", "quel est mon programme", "quoi mon programme",
                       "au programme",
                       "programme du jour", "programme de demain")) and not _has(norm, ("ajoute", "ajouter", "note", "supprime", "annule")):
            return "list_events", {}
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

    def _face(self, norm, text, previous):
        """« Mets ton visage en vert », « Change de couleur : violet », « Passe en arc-en-ciel », « Remets ta couleur
        normale » : la couleur du visage (jamais une lumière, sauf pièce nommée)."""
        if not (self._exists("set_face_theme") and _has(norm, FACE_WORDS)) or self._names_room(text):
            return None
        from jarvis.face.themes import said_theme

        theme = said_theme(text)
        return ("set_face_theme", {"theme": theme}) if theme else None

    def _sound(self, norm, text, previous):
        if _has(norm, UNMUTE) and self._exists("unmute_volume"):
            return "unmute_volume", {}
        if _has(norm, MUTE) and self._exists("mute_volume"):
            return "mute_volume", {}
        negative = NEGATIVE.search(text.lower())
        if negative and _has(norm, SOUND_WORDS) and self._exists("set_volume"):
            # « Mets le volume à moins 10 » : l'outil explique la plage (0 à 100 %), plutôt qu'une réponse floue.
            level = re.search(r"\d{1,3}", text.lower()[negative.start():])
            return ("set_volume", {"volume": -int(level.group())}) if level else None
        if (_has(norm, SOUND_WORDS) or (previous or {}).get("tool") == "set_volume") and self._exists("set_volume"):
            if self._about_lights(norm, text):
                return None
            level = _percent_raw(text)
            if level is not None:
                music = (previous or {}).get("tool", "").startswith("spotify_")
                if self._exists("spotify_volume") and (music or _has(norm, ("spotify", "musique", "de la musique",
                                                                             "du morceau"))):
                    return "spotify_volume", {"volume": level}  # « le volume de Spotify » : pas celui du PC
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
        # « Quelles applications sont ouvertes ? » : l'outil de lecture, sans laisser le choix au LLM (session QA :
        # il n'en choisissait parfois aucun et répondait « je n'ai pas accès à cette information »).
        if self._exists("list_running_applications") and _has(norm, RUNNING_APPS):
            return "list_running_applications", {}
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
        if found:
            return "create_timer", {"duration": found[0]}
        # Durée dite mais refusée (« 0 seconde ») : le minuteur l'explique, plutôt qu'une recherche Spotify.
        said = re.search(r"\b\d+\s*(?:secondes?|minutes?|heures?|h|min|s)\b", norm)
        return ("create_timer", {"duration": said.group(0)}) if said else None

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
        if self._exists("cancel_alarm") and re.search(
                r"\b(?:annule|annuler|supprime|supprimer|enleve|desactive|retire)\b.*\breveils?\b", norm):
            clock = parse_clock(text)
            return "cancel_alarm", ({"time": f"{clock[0]} heures {clock[1]}" if clock[1] else f"{clock[0]} heures"}
                                    if clock else {})
        if not (_has(norm, ALARM_WORDS) and self._exists("create_alarm")):
            return None
        clock = parse_clock(text)
        if clock is None:
            # Heure dite mais impossible (« à 25 heures ») : le réveil la refuse avec une phrase claire, plutôt que
            # de laisser le LLM y voir autre chose.
            said = re.search(r"\b(\d{1,3})\s*(?:h|heures?)\b(?:\s*(\d{1,3}))?", norm)
            if said is None:
                return None
            return "create_alarm", {"time": f"{said.group(1)} heures" + (f" {said.group(2)}" if said.group(2) else "")}
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

        city = mentioned_city(text) or _named_place(text)
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
        reply = REPLY.match(normalize(text)) if self._exists("reply_mail") else None
        if reply is not None:
            # Avant le découpage : la réponse dictée peut contenir « et », « ne », « pas »...
            body = _original_tail(text, tokens(reply.group(1)))
            return {"type": "tool_call", "tool": "reply_mail", "parameters": {"body": body}} if body else None
        if self._exists("set_face_theme") and _has(normalize(text), FACE_WORDS):
            # « Mets ton thème en noir et blanc », « Change de couleur, violet » : une seule demande, jamais coupée.
            whole = self.one(text)
            if whole is not None and whole.get("tool") == "set_face_theme":
                return whole
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
            return {"type": "tool_calls", "calls": _spread_colour(calls)}
        if calls and calls[0]["tool"] == "create_reminder":
            return self.one(text)
        return None


def _spread_colour(calls: list[dict]) -> list[dict]:
    """« Allume l'entrée et la chambre en vert » : la couleur (ou l'ambiance) dite à la fin vaut aussi pour les pièces
    seulement allumées avant elle."""
    last = calls[-1]
    if last["tool"] not in ("set_color", "set_scene", "set_color_temperature"):
        return calls
    spread = []
    for call in calls[:-1]:
        if call["tool"] == "light_on" and not call["parameters"]:
            call = {**call, "tool": last["tool"], "parameters": dict(last["parameters"])}
        spread.append(call)
    return [*spread, last]


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
