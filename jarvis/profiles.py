"""Profils utilisateurs et terminaux : qui parle, depuis quel appareil, dans quelle pièce, avec quels droits.

Profils (``[users.<id>]`` : name, role, title) et rôles :
- owner (propriétaire, « monsieur ») : tous les outils, confirmations comprises ;
- adult (membre de la famille) : tous les outils, confirmations comprises, sauf effacer toute la mémoire ;
- child (enfant) : informations, lumières, musique, minuteurs, rappels et réveils ; aucune action à confirmer,
  ni fichiers, ni applications, ni verrouillage, ni mémoire ;
- guest (invité) : informations, lumières et musique seulement.
Le rôle restreint ; il n'élargit jamais : un outil à confirmer reste à confirmer pour le propriétaire. Aucune
reconnaissance vocale ou faciale n'existe encore : l'identité vient du terminal (son utilisateur par défaut) ; même
quand une reconnaissance existera, elle ne suffira pas pour une action critique (la confirmation reste exigée).

Terminaux (``[terminals.<id>]`` : name, url, room, user) : le PC principal, un futur satellite du salon... Chaque
demande porte le contexte (terminal, pièce, utilisateur) ; le Core reste central. Sans section, le terminal
« main » est déduit de [audio] remote (ou du micro local), utilisateur « owner ».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.tools.base import Risk, Tool

OWNER, ADULT, CHILD, GUEST = "owner", "adult", "child", "guest"
ROLES = (OWNER, ADULT, CHILD, GUEST)
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Catégories d'outils (par préfixe ou nom exact) : la politique des rôles s'exprime par catégorie.
CATEGORIES = {
    "info": ("get_time", "get_date", "get_weather", "light_status", "list_timers", "list_reminders", "list_alarms",
             "list_routines"),
    "home": ("light_", "set_brightness", "set_color", "set_color_temperature", "set_scene", "set_face_theme"),
    "media": ("media_", "spotify_"),
    "schedule": ("create_timer", "cancel_timer", "create_reminder", "cancel_reminder", "create_alarm", "cancel_alarm"),
    "calendar": ("list_events", "next_events", "search_events", "free_slots", "add_event", "delete_event"),
    "routines": ("run_routine", "delete_routine"),
    "apps": ("open_application", "close_application", "open_url", "list_running_applications"),
    "system": ("set_volume", "mute_volume", "unmute_volume", "system_info", "lock_pc", "network_status"),
    "files": ("find_files", "read_text_file", "create_text_file", "copy_file", "move_file", "delete_file"),
    "memory": ("remember", "recall", "forget"),  # faits personnels : jamais lus à un enfant ou un invité
}
POLICY = {
    OWNER: {"categories": set(CATEGORIES), "confirm": True},
    ADULT: {"categories": set(CATEGORIES), "confirm": True},
    CHILD: {"categories": {"info", "home", "media", "schedule"}, "confirm": False},
    GUEST: {"categories": {"info", "home", "media"}, "confirm": False},
}


def category(tool_name: str) -> str | None:
    for name, members in CATEGORIES.items():
        if any(tool_name == m or (m.endswith("_") and tool_name.startswith(m)) for m in members):
            return name
    return None


@dataclass(frozen=True)
class UserProfile:
    id: str
    name: str
    role: str
    title: str = "monsieur"
    preferences: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Terminal:
    id: str
    name: str
    url: str = ""
    room: str = ""
    user: str = OWNER
    kind: str = "pc"


@dataclass(frozen=True)
class RequestContext:
    """Contexte d'une demande : d'où elle vient et qui la fait (device_id, room_id, user_id)."""

    device_id: str
    room_id: str
    user_id: str


class Profiles:
    def __init__(self, users: list[UserProfile] | None = None, terminals: list[Terminal] | None = None):
        users = users or [UserProfile(OWNER, "monsieur", OWNER)]
        self._users = {u.id: u for u in users}
        if OWNER not in {u.role for u in users}:
            raise ValueError("[users] : au moins un profil de rôle « owner »")
        self._terminals = {t.id: t for t in terminals or [Terminal("main", "terminal principal")]}
        for terminal in self._terminals.values():
            if terminal.user not in self._users:
                raise ValueError(f"[terminals.{terminal.id}] : utilisateur inconnu « {terminal.user} »")

    def user(self, user_id: str) -> UserProfile | None:
        return self._users.get(user_id)

    def users(self) -> list[UserProfile]:
        return list(self._users.values())

    def terminals(self) -> list[Terminal]:
        return list(self._terminals.values())

    def terminal(self, terminal_id: str) -> Terminal:
        return self._terminals[terminal_id]

    def context(self, terminal_id: str = "main") -> RequestContext:
        terminal = self._terminals.get(terminal_id) or next(iter(self._terminals.values()))
        return RequestContext(terminal.id, terminal.room, terminal.user)

    def allows(self, user_id: str, tool: Tool, parameters: dict) -> tuple[bool, str]:
        """(autorisé, raison). Le rôle ne fait que restreindre : risque et confirmation restent ceux de l'outil."""
        profile = self._users.get(user_id)
        if profile is None:
            return False, "utilisateur inconnu"
        policy = POLICY[profile.role]
        group = category(tool.name)
        if group is None and profile.role not in (OWNER, ADULT):
            return False, f"outil non classé, réservé aux adultes ({tool.name})"
        if group is not None and group not in policy["categories"]:
            return False, f"« {tool.name} » n'est pas permis au profil {profile.role}"
        if tool.risk is Risk.CONFIRMATION_REQUIRED and not policy["confirm"]:
            return False, f"action à confirmer, réservée aux adultes ({tool.name})"
        if tool.name == "forget" and profile.role != OWNER and str(parameters.get("topic", "")).strip().lower() == "tout":
            return False, "seul le propriétaire peut effacer toute la mémoire"
        return True, "permis par le profil"


def load_profiles(users: dict, terminals: dict, audio_remote: str = "") -> Profiles:
    """[users.<id>] et [terminals.<id>] -> profils ; valeurs par défaut : un propriétaire, un terminal « main »."""
    parsed_users = []
    for key, spec in (users or {}).items():
        if not KEY.match(key) or not isinstance(spec, dict):
            raise ValueError(f"[users.{key}] invalide")
        role = str(spec.get("role", GUEST))
        if role not in ROLES:
            raise ValueError(f"[users.{key}] role : {', '.join(ROLES)}")
        parsed_users.append(UserProfile(key, str(spec.get("name", key)), role, str(spec.get("title", "monsieur")),
                                        dict(spec.get("preferences", {}))))
    if not parsed_users:
        parsed_users = [UserProfile(OWNER, "monsieur", OWNER)]
    default_user = next((u.id for u in parsed_users if u.role == OWNER), None)
    if default_user is None:
        raise ValueError("[users] : au moins un profil de rôle « owner »")
    parsed_terminals = []
    for key, spec in (terminals or {}).items():
        if not KEY.match(key) or not isinstance(spec, dict):
            raise ValueError(f"[terminals.{key}] invalide")
        parsed_terminals.append(Terminal(key, str(spec.get("name", key)), str(spec.get("url", "")).rstrip("/"),
                                         str(spec.get("room", "")), str(spec.get("user", default_user)),
                                         str(spec.get("kind", "pc"))))
    if not parsed_terminals:
        parsed_terminals = [Terminal("main", "terminal principal", audio_remote.rstrip("/"), "", default_user)]
    return Profiles(parsed_users, parsed_terminals)
