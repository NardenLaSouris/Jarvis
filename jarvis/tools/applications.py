"""Outils applications : ouvrir une application installée, fermer ou lister les applications connues, ouvrir une
page Web.

Ouvrir : toute application présente sur le PC, sauf celles de la liste des applications interdites
([tools.blocked_applications]). Le LLM ne fournit qu'un nom (« Wallpaper Engine ») ; ORION le résout lui-même, d'abord
parmi les applications connues (catalogue et [tools.applications] : exécutable de la config, registre « App Paths »,
emplacements habituels, paquet du Microsoft Store), puis parmi les applications installées (jarvis.tools.app_finder).
La cible réelle est vérifiée (programme .exe existant, ni script, ni interpréteur) et comparée à la liste interdite par
son identité (chemin réel, numéro Steam), pas par son nom. Fermer et lister : applications connues seulement.
Jamais de shell, de chemin, de PID ni d'argument venant du LLM.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from jarvis.personality import normalize
from jarvis.tools.app_finder import SCRIPT_HOSTS, AppFinder, Found, identity_of, is_browser
from jarvis.tools.base import (
    APPLICATION_NOT_FOUND, APPLICATION_NOT_RUNNING, EXECUTION_FAILED, INVALID_PARAMETERS, INVALID_URL, Param, Risk,
    Tool, ToolError,
)

log = logging.getLogger(__name__)

MAX_URL = 2048
FORBIDDEN_URL_CHARS = set(" \t\r\n\"'`<>\\^|{}")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", re.IGNORECASE)
KEY = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
PROCESS = re.compile(r"^[A-Za-z0-9 ._-]{1,60}$")
# Nom d'application demandé : lettres, chiffres, espaces et quelques signes de noms commerciaux (« Paint.NET »,
# « Notepad++ », « Assassin's Creed »). Ni chemin, ni séparateur de commande, ni guillemet.
APP_NAME = re.compile(r"^\w[\w .'+-]{0,59}$")
APPLICATION_BLOCKED = "application_blocked"
INVALID_TARGET = "invalid_target"
AMBIGUOUS = "ambiguous_target"
# Interdites même sans configuration : outils d'administration du système (la liste configurée s'y ajoute).
DEFAULT_BLOCKED = ("regedit.exe", "regedt32.exe", "mmc.exe", "bcdedit.exe", "diskpart.exe", "vssadmin.exe",
                   "wbadmin.exe", "taskkill.exe", "shutdown.exe", "msconfig.exe", "systemreset.exe")


@dataclass(frozen=True)
class App:
    """Application connue : comment la trouver, la lancer, la reconnaître et la fermer.

    ``close`` : "graceful" (fermeture normale, comme la croix de la fenêtre), "force" (processus
    arrêtés, pour les applications qui se cachent dans la zone de notification) ou "command"
    (l'exécutable suivi de ``close_args``, par exemple ``steam.exe -shutdown``).
    """

    label: str
    aliases: tuple[str, ...] = ()
    windows: tuple[tuple[str, ...], ...] = ()
    linux: tuple[tuple[str, ...], ...] = ()
    app_path: str = ""
    store: tuple[str, str] | None = None
    windows_processes: tuple[str, ...] = ()
    linux_processes: tuple[str, ...] = ()
    close: str = "graceful"
    close_args: tuple[str, ...] = ()


CATALOG = {
    "discord": App("Discord", ("discord",),
                   windows=(("%LOCALAPPDATA%\\Discord\\Update.exe", "--processStart", "Discord.exe"),),
                   linux=(("discord",),),
                   windows_processes=("Discord.exe",), linux_processes=("Discord", "discord"), close="force"),
    "steam": App("Steam", ("steam",),
                 windows=(("%ProgramFiles(x86)%\\Steam\\steam.exe",), ("%ProgramFiles%\\Steam\\steam.exe",)),
                 linux=(("steam",),), app_path="steam.exe",
                 windows_processes=("steam.exe",), linux_processes=("steam",), close="command",
                 close_args=("-shutdown",)),
    "chrome": App("Google Chrome", ("chrome", "google chrome", "google"),
                  windows=(("%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe",),
                           ("%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe",),
                           ("%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe",)),
                  linux=(("google-chrome",), ("chromium",), ("chromium-browser",)), app_path="chrome.exe",
                  windows_processes=("chrome.exe",), linux_processes=("chrome", "chromium", "chromium-browser")),
    "spotify": App("Spotify", ("spotify",),
                   windows=(("%APPDATA%\\Spotify\\Spotify.exe",),), linux=(("spotify",),), app_path="Spotify.exe",
                   store=("SpotifyAB.SpotifyMusic_zpdnekdrzrea0", "Spotify"),
                   windows_processes=("Spotify.exe",), linux_processes=("spotify",)),
    "vscode": App("Visual Studio Code", ("vscode", "vs code", "visual studio code", "code"),
                  windows=(("%LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe",),
                           ("%ProgramFiles%\\Microsoft VS Code\\Code.exe",)),
                  linux=(("code",),), app_path="Code.exe",
                  windows_processes=("Code.exe",), linux_processes=("code",)),
    "notepad": App("le Bloc-notes", ("notepad", "bloc notes", "blocnotes", "bloc note"),
                   windows=(("%SystemRoot%\\System32\\notepad.exe",),),
                   linux=(("gnome-text-editor",), ("gedit",), ("mousepad",), ("kate",)),
                   windows_processes=("Notepad.exe",),
                   linux_processes=("gnome-text-editor", "gedit", "mousepad", "kate")),
}


@dataclass(frozen=True)
class Application:
    """Application autorisée par la configuration : clé logique, description et exécutable imposé éventuel."""

    key: str
    app: App
    executable: str | None = None

    @property
    def label(self) -> str:
        return self.app.label

    @property
    def processes(self) -> tuple[str, ...]:
        return self.app.windows_processes if sys.platform == "win32" else self.app.linux_processes


def load_applications(table: dict | None) -> dict[str, Application]:
    """Section ``[tools.applications]`` -> applications autorisées (toutes celles du catalogue si absente).

    Une application hors catalogue doit fournir ``executable`` (chemin absolu) et ``process``.
    """
    if not table:
        return {key: Application(key, app) for key, app in CATALOG.items()}
    apps: dict[str, Application] = {}
    for key, spec in table.items():
        spec = spec if isinstance(spec, dict) else {}
        if not KEY.match(key) or not spec.get("enabled", True):
            continue
        executable = spec.get("executable")
        executable = str(executable) if executable else None
        if key in CATALOG:
            apps[key] = Application(key, CATALOG[key], executable)
            continue
        process = str(spec.get("process", ""))
        if not executable or not os.path.isabs(executable) or not PROCESS.match(process):
            log.warning("Application « %s » ignorée : executable (chemin absolu) et process sont requis.", key)
            continue
        app = App(str(spec.get("label", key)), tuple(str(a) for a in spec.get("aliases", [])),
                  windows_processes=(process,), linux_processes=(process,),
                  close="force" if spec.get("close") == "force" else "graceful")
        apps[key] = Application(key, app, executable)
    return apps


@dataclass(frozen=True)
class Blocklist:
    """Applications interdites ([tools.blocked_applications]) : chemins exacts (``paths``), noms de programme
    (``executables``, « regedit.exe »), jeux Steam (``steam``, numéros) et paquets du Store (``store``). Comparées à
    l'identité de la cible résolue, jamais au nom demandé."""

    paths: frozenset[str] = frozenset()
    executables: frozenset[str] = frozenset(DEFAULT_BLOCKED)
    steam: frozenset[str] = frozenset()
    store: frozenset[str] = frozenset()

    @classmethod
    def load(cls, table: dict | None) -> "Blocklist":
        table = table or {}
        items = lambda key: [str(x) for x in table.get(key, []) if str(x).strip()]  # noqa: E731
        return cls(frozenset(identity_of(os.path.expandvars(p)) for p in items("paths")),
                   frozenset({*DEFAULT_BLOCKED, *(e.lower() for e in items("executables"))}),
                   frozenset(items("steam")), frozenset(items("store")))

    def blocks(self, target: Found) -> bool:
        kind, _, ident = target.identity.partition(":")
        if (kind == "steam" and ident in self.steam) or (kind == "store" and ident in self.store):
            return True
        # Le programme lancé compte aussi : interdire steam.exe interdit les jeux qu'il lance.
        program = target.argv[0] if target.argv else ""
        return identity_of(program) in self.paths or target.identity in self.paths or             os.path.basename(program).lower() in self.executables


# --- Résolution et lancement ----------------------------------------------------------------------

def _app_path(name: str) -> str | None:
    """Chemin déclaré par l'installeur dans le registre (« App Paths »), sans dépendre de la machine."""
    if sys.platform != "win32" or not name:
        return None
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as key:
                value = winreg.QueryValueEx(key, "")[0]
                if value:
                    return str(value).strip('"')
        except OSError:
            continue
    return None


def _resolve(command: tuple[str, ...]) -> list[str] | None:
    program = os.path.expandvars(command[0])
    if "%" in program or "$" in program:
        return None
    if os.path.isabs(program):
        return [program, *command[1:]] if Path(program).is_file() else None
    found = shutil.which(program)
    return [found, *command[1:]] if found else None


def _store_command(store: tuple[str, str] | None) -> list[str] | None:
    if sys.platform != "win32" or store is None:
        return None
    package, app_id = store
    if not (Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / package).is_dir():
        return None
    return [_system_program("explorer.exe", system32=False), f"shell:AppsFolder\\{package}!{app_id}"]


def find_command(application: Application) -> list[str] | None:
    """Commande de lancement : exécutable de la config, registre, emplacements connus, Microsoft Store."""
    app = application.app
    commands: list[tuple[str, ...]] = []
    if application.executable:
        commands.append((application.executable,))
    registered = _app_path(app.app_path)
    if registered:
        commands.append((registered,))
    commands += list(app.windows if sys.platform == "win32" else app.linux)
    for command in commands:
        argv = _resolve(command)
        if argv is not None:
            return argv
    return _store_command(app.store)


def _launch(argv: list[str]) -> None:
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "close_fds": True, "shell": False}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(argv, **options)


def _system_program(name: str, system32: bool = True) -> str:
    root = os.environ.get("SystemRoot", "C:\\Windows")
    return os.path.join(root, "System32", name) if system32 else os.path.join(root, name)


class Processes:
    """Surveillance et arrêt des processus des applications autorisées, par les outils du système (sans shell)."""

    def running(self, names: tuple[str, ...]) -> bool:
        if sys.platform == "win32":
            out = subprocess.run([_system_program("tasklist.exe"), "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, shell=False).stdout
            images = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}
            return any(name.lower() in images for name in names)
        return any(subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=10, shell=False).returncode == 0
                   for name in names)

    def stop(self, names: tuple[str, ...], force: bool) -> None:
        for name in names:
            if sys.platform == "win32":
                argv = [_system_program("taskkill.exe"), "/IM", name] + (["/F", "/T"] if force else [])
            else:
                argv = ["pkill", *(["-KILL"] if force else []), "-x", name]
            subprocess.run(argv, capture_output=True, timeout=10, shell=False)


def _wait(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


# --- Outils ---------------------------------------------------------------------------------------

def _app_param(apps: dict[str, Application], verb: str) -> Param:
    names = {normalize(alias): key for key, a in apps.items() for alias in (key, *a.app.aliases)}
    listed = ", ".join(apps) or "aucune"

    def check(value: str) -> str:
        key = names.get(normalize(value))
        if key is None:
            # Exact : l'application n'est pas dans la liste configurée (elle peut exister sur le PC).
            raise ToolError(INVALID_PARAMETERS, f"« {value[:40]} » ne fait pas partie des applications que je peux "
                                                f"{verb} : {listed}.")
        return key

    return Param(str, f"identifiant de l'application, parmi : {listed}", max_length=40, check=check)


def _name_param(apps: dict[str, Application]) -> Param:
    def check(value: str) -> str:
        name = " ".join(value.split())
        if not APP_NAME.match(name):
            raise ToolError(INVALID_PARAMETERS, "Ce nom d'application n'est pas valide.")
        return name

    def said(value: str, text: str) -> bool:  # le nom vient de la demande (« vs code » pour « vscode » compris)
        return normalize(value).replace(" ", "") in normalize(text).replace(" ", "")

    known = ", ".join(a.label for a in apps.values()) or "aucune"
    return Param(str, f"nom de l'application tel que dit par l'utilisateur (par exemple : {known})", max_length=60,
                 check=check, evidence=said)


def _known_target(entry: Application) -> Found | None:
    argv = find_command(entry)
    if argv is None:
        return None
    store = argv[1].removeprefix("shell:AppsFolder\\").split("!")[0] if len(argv) > 1 and \
        argv[1].startswith("shell:AppsFolder\\") else ""
    identity = f"store:{store}" if store else identity_of(argv[0])
    # Commande de la configuration ou du catalogue : jamais un interpréteur (cmd, PowerShell...) pour autant.
    problem = "" if store or os.path.basename(argv[0]).lower() not in SCRIPT_HOSTS else         "interpréteur de commandes ou de scripts"
    return Found(entry.label, tuple(argv), identity, "application connue", entry.processes, problem)


def open_app_tool(apps: dict[str, Application], launcher: Callable[[list[str]], None], processes: Processes,
                  wait: float = 4.0, finder: AppFinder | None = None, blocked: Blocklist | None = None) -> Tool:
    names = {normalize(alias): key for key, a in apps.items() for alias in (key, *a.app.aliases)}
    finder = finder or AppFinder()
    blocked = blocked or Blocklist()

    def target(name: str) -> tuple[str, Found]:
        key = names.get(normalize(name))
        if key is not None:  # application connue : sa commande éprouvée, sinon ce qui est installé sous son nom
            found = _known_target(apps[key])
            if found is not None:
                return key, found
            name = apps[key].label
        candidates = finder.find(name)
        valid = [c for c in candidates if not c.problem]
        candidates = valid or candidates
        if not candidates:
            if key is not None:
                raise ToolError(APPLICATION_NOT_FOUND, f"{apps[key].label} n'est pas installé sur cette machine.")
            what = "de navigateur par défaut" if is_browser(name) else f"d'application « {name} »"
            raise ToolError(APPLICATION_NOT_FOUND, f"Je ne trouve pas {what} sur ce PC.")
        if len(candidates) > 1:
            listed = ", ".join(f"« {c.label} »" for c in sorted(candidates, key=lambda c: c.label)[:4])
            raise ToolError(AMBIGUOUS, f"Plusieurs applications correspondent : {listed}. Laquelle ?")
        return key or normalize(candidates[0].label).replace(" ", "_")[:40], candidates[0]

    def run(application: str) -> dict:
        key, found = target(application)
        if blocked.blocks(found):
            log.info("Ouverture refusée : %s est dans la liste des applications interdites", found.label)
            raise ToolError(APPLICATION_BLOCKED, f"{found.label} fait partie des applications que je n'ouvre pas.")
        if found.problem:
            log.info("Ouverture refusée : %s (%s, %s)", found.label, found.source, found.problem)
            raise ToolError(INVALID_TARGET, f"Je ne lance pas {found.label} : son raccourci ne mène pas à un "
                                            f"programme ordinaire ({found.problem}).")
        already = bool(found.processes) and processes.running(found.processes)
        try:
            launcher(list(found.argv))
        except OSError as exc:
            reason = f" ({exc.strerror})" if getattr(exc, "strerror", None) else ""
            raise ToolError(EXECUTION_FAILED, f"Je n'ai pas pu lancer {found.label}{reason}.") from exc
        if already:
            status = "déjà ouverte"
        elif found.processes and _wait(lambda: processes.running(found.processes), wait):
            status = "ouverte"
        else:
            status = "lancement en cours"
        return {"application": key, "label": found.label, "status": status}

    return Tool("open_application", "Ouvre une application installée sur le PC, désignée par son nom (sauf les "
                                    "applications interdites).",
                {"application": _name_param(apps)},
                {"application": "identifiant", "label": "nom", "status": "ouverte, déjà ouverte ou lancement en cours"},
                Risk.SAFE, run, question=lambda p: f"Voulez-vous que j'ouvre {p['application']} ?",
                say=lambda r: OPENED[r["status"]].format(label=r["label"]))


OPENED = {"ouverte": "{label} est ouvert.", "déjà ouverte": "{label} est déjà ouvert.",
          "lancement en cours": "{label} est en cours de lancement."}


def close_app_tool(apps: dict[str, Application], processes: Processes, launcher: Callable[[list[str]], None],
                   wait: float = 6.0) -> Tool:
    def run(application: str) -> dict:
        entry = apps[application]
        names = entry.processes
        if not names:
            raise ToolError(EXECUTION_FAILED, f"Je ne sais pas fermer {entry.label}.")
        if not processes.running(names):
            raise ToolError(APPLICATION_NOT_RUNNING, f"{entry.label} n'est pas ouvert.")
        argv = find_command(entry) if entry.app.close == "command" else None
        try:
            if argv is not None:
                launcher([argv[0], *entry.app.close_args])
            else:
                processes.stop(names, force=entry.app.close == "force")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError(EXECUTION_FAILED, f"Je n'ai pas pu fermer {entry.label}.") from exc
        if _wait(lambda: not processes.running(names), wait):
            return {"application": application, "label": entry.label, "closed": True}
        raise ToolError(EXECUTION_FAILED, f"{entry.label} ne s'est pas fermé complètement.")

    return Tool("close_application", f"Ferme une application autorisée ({', '.join(apps) or 'aucune'}).",
                {"application": _app_param(apps, "fermer")},
                {"application": "identifiant", "label": "nom", "closed": "true"},
                Risk.CONFIRMATION_REQUIRED, run,
                question=lambda p: f"Voulez-vous que je ferme {apps[p['application']].label} ?",
                say=lambda r: f"{r['label']} est fermé.")


def list_apps_tool(apps: dict[str, Application], processes: Processes) -> Tool:
    def run() -> dict:
        return {"applications": [
            {"application": key, "label": entry.label, "running": bool(entry.processes) and processes.running(entry.processes)}
            for key, entry in apps.items()
        ]}

    return Tool("list_running_applications", "Indique lesquelles des applications connues sont ouvertes.", {},
                {"applications": "liste de {application, label, running}"}, Risk.SAFE, run)


# --- URL ------------------------------------------------------------------------------------------

EXECUTABLE_DOWNLOADS = (".exe", ".msi", ".msix", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".jse",
                        ".wsf", ".hta", ".lnk", ".jar", ".dll", ".reg", ".appx", ".appxbundle", ".sh")


def validate_url(url: str) -> str:
    """URL http(s) sans caractère exotique ni identifiants ; lève ToolError(invalid_url) sinon."""
    if len(url) > MAX_URL or any(c in FORBIDDEN_URL_CHARS or ord(c) < 32 for c in url):
        raise ToolError(INVALID_URL, "Cette adresse n'est pas valide.")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise ToolError(INVALID_URL, "Cette adresse n'est pas valide.") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        raise ToolError(INVALID_URL, "Je n'ouvre que des adresses Web en http ou https.")
    host = parts.hostname
    try:
        valid_host = ipaddress.ip_address(host) is not None if ":" in host else bool(HOST.match(host))
    except ValueError:
        valid_host = False
    if not valid_host:
        raise ToolError(INVALID_URL, "Cette adresse n'est pas valide.")
    if parts.path.lower().rsplit("/", 1)[-1].endswith(EXECUTABLE_DOWNLOADS):
        # Ouvrir un site reste sans confirmation ; un lien qui télécharge directement un programme, jamais.
        raise ToolError(INVALID_URL, "Ce lien télécharge un programme : je ne l'ouvre pas.")
    return parts.geturl()


def site_name(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def url_tool(opener: Callable[[str], bool]) -> Tool:
    def run(url: str) -> dict:
        if not opener(url):
            raise ToolError(EXECUTION_FAILED, "Je n'ai pas pu ouvrir le navigateur.")
        return {"url": url, "site": site_name(url), "opened": True}

    return Tool("open_url", "Ouvre une page Web (http ou https) dans le navigateur par défaut.",
                {"url": Param(str, "adresse complète, par exemple https://www.example.com", max_length=MAX_URL,
                              check=validate_url)},
                {"url": "adresse ouverte", "site": "nom du site", "opened": "true"},
                Risk.SAFE, run, question=lambda p: f"Voulez-vous que j'ouvre la page {site_name(p['url'])} ?",
                say=lambda r: f"{r['site'].split('.')[0].capitalize()} est ouvert dans le navigateur.")
