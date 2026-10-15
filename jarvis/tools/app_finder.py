"""Applications installées sur ce PC, pour « ouvre <nom> » au-delà des applications connues (jarvis.tools.applications).

Sources, toutes locales (rien n'est cherché ni téléchargé sur Internet) :
- raccourcis du menu Démarrer (utilisateur et commun) et du bureau (utilisateur et public) : ``.lnk`` lus ici sans
  dépendance (format MS-SHLLINK : chemin cible et arguments), ``.url`` de Steam (``steam://rungameid/<n>``) ;
- registre : applications déclarées (« App Paths ») et programmes installés (clés de désinstallation, icône .exe) ;
- bibliothèques Steam (``appmanifest_*.acf``), lancées par ``steam.exe -applaunch <n>`` ;
- navigateur par défaut (« lance mon navigateur »).

Chaque application trouvée garde une identité stable (chemin réel de l'exécutable, ``steam:<n>``) : c'est elle que la
liste des applications interdites compare, jamais le seul nom. Une cible qui n'est pas un programme ordinaire (script,
interpréteur de commandes, désinstalleur, fichier absent) est marquée ``problem`` et n'est jamais lancée.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from jarvis.personality import normalize

log = logging.getLogger(__name__)

# Programmes qui exécutent un script ou une commande : jamais lancés comme une application, quel que soit le raccourci.
SCRIPT_HOSTS = frozenset({
    "cmd.exe", "powershell.exe", "powershell_ise.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe",
    "rundll32.exe", "regsvr32.exe", "msiexec.exe", "python.exe", "pythonw.exe", "py.exe", "pyw.exe", "node.exe",
    "java.exe", "javaw.exe", "bash.exe", "wsl.exe", "wslhost.exe", "conhost.exe", "explorer.exe", "schtasks.exe",
    "reg.exe", "sc.exe", "netsh.exe", "certutil.exe", "bitsadmin.exe", "msbuild.exe", "installutil.exe",
    "regasm.exe", "regsvcs.exe", "cmstp.exe", "forfiles.exe", "wmic.exe", "hh.exe", "runas.exe", "pcalua.exe",
})
UNINSTALLER = re.compile(r"^(unins\d*|uninstall\w*|uninst\w*|setup|update-?helper)\.exe$", re.IGNORECASE)
# Raccourcis qui ne sont pas l'application elle-même (désinstallation, aide, site...).
NOT_AN_APP = re.compile(r"\b(desinstall\w*|uninstall\w*|readme|lisez moi|aide|help|manuel|manual|documentation|"
                        r"site web|website|release notes|notes de version|license|licence)\b")
# Mots qui n'aident pas à reconnaître une application (« ouvre l'application Steam », « lance mon jeu Wallpaper Engine »).
NOISE = {"l", "le", "la", "les", "un", "une", "mon", "ma", "mes", "ton", "ta", "application", "appli", "app",
         "logiciel", "programme", "jeu"}
BROWSER_WORDS = {"navigateur", "navigateur web", "navigateur internet", "browser", "internet"}
STEAM_APP = re.compile(r"^\d{1,10}$")
# Programmes installés qui ne sont pas des applications à ouvrir (composants, pilotes, mises à jour).
COMPONENT = re.compile(r"redistributable|runtime|\bsdk\b|driver|pilote|\bupdate\b|hotfix|language pack|"
                       r"module linguistique", re.IGNORECASE)
# Source préférée quand plusieurs entrées portent exactement le même nom (deux raccourcis « Discord »).
SOURCE_RANK = ("raccourci", "raccourci Steam", "Steam", "navigateur par défaut", "application déclarée",
               "programme installé")


@dataclass(frozen=True)
class Found:
    label: str
    argv: tuple[str, ...]
    identity: str
    source: str
    processes: tuple[str, ...] = ()
    problem: str = ""


# --- Validation des cibles ------------------------------------------------------------------------

def target_problem(argv: tuple[str, ...] | list[str]) -> str:
    """Raison de refuser la commande (vide si c'est un programme ordinaire installé)."""
    if not argv:
        return "aucune cible"
    program = argv[0]
    if not os.path.isabs(program) or "%" in program:
        return "chemin non absolu"
    name = os.path.basename(program).lower()
    if not name.endswith(".exe"):
        return "pas un programme (.exe)"
    if name in SCRIPT_HOSTS:
        return "interpréteur de commandes ou de scripts"
    if UNINSTALLER.match(name):
        return "installeur ou désinstalleur"
    if not os.path.isfile(program):
        return "fichier introuvable"
    return ""


def identity_of(program: str) -> str:
    return os.path.normcase(os.path.realpath(program))


def _found(label: str, argv: list[str] | tuple[str, ...], source: str) -> Found:
    argv = tuple(str(a) for a in argv)
    problem = target_problem(argv)
    identity = identity_of(argv[0]) if argv and os.path.isabs(argv[0]) else (argv[0] if argv else "")
    process = (os.path.basename(argv[0]),) if argv and not problem else ()
    return Found(label.strip(), argv, identity, source, process, problem)


# --- Arguments à la manière de Windows -------------------------------------------------------------

def split_windows_args(line: str) -> list[str]:
    """Découpe une ligne d'arguments selon les règles de Windows (guillemets, antislashs) : liste structurée, jamais
    réinterprétée par un shell."""
    args: list[str] = []
    current: list[str] = []
    i, quoted, started = 0, False, False
    while i < len(line):
        char = line[i]
        if char == "\\":
            run = 0
            while i < len(line) and line[i] == "\\":
                run, i = run + 1, i + 1
            if i < len(line) and line[i] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    i += 1
            else:
                current.append("\\" * run)
            started = True
            continue
        if char == '"':
            if quoted and i + 1 < len(line) and line[i + 1] == '"':
                current.append('"')
                i += 2
                continue
            quoted, started = not quoted, True
        elif char in " \t" and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        i += 1
    if started:
        args.append("".join(current))
    return args


# --- Raccourcis ------------------------------------------------------------------------------------

def _codepage() -> str:
    return "mbcs" if sys.platform == "win32" else "cp1252"


def _ansi(data: bytes, start: int) -> str:
    end = data.index(b"\0", start)
    return data[start:end].decode(_codepage(), errors="replace")


def _wide(data: bytes, start: int) -> str:
    end = start
    while data[end:end + 2] != b"\0\0":
        end += 2
    return data[start:end].decode("utf-16-le", errors="replace")


def read_lnk(data: bytes) -> tuple[str, str] | None:
    """(chemin cible, arguments) d'un raccourci Windows ``.lnk`` (format MS-SHLLINK), ou None s'il n'a pas de chemin
    local (raccourci « publié » d'un installeur, lien réseau...)."""
    if len(data) < 76 or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return None
    flags = struct.unpack_from("<I", data, 20)[0]
    unicode = bool(flags & 0x80)
    pos = 76
    if flags & 0x01:  # HasLinkTargetIDList
        pos += 2 + struct.unpack_from("<H", data, pos)[0]
    target = ""
    if flags & 0x02:  # HasLinkInfo
        size, header, info_flags, _, base, _, suffix = struct.unpack_from("<7I", data, pos)
        if info_flags & 0x01:  # VolumeIDAndLocalBasePath
            if header >= 0x24:
                base_u, suffix_u = struct.unpack_from("<2I", data, pos + 28)
                target = _wide(data, pos + base_u) + _wide(data, pos + suffix_u)
            else:
                target = _ansi(data, pos + base) + _ansi(data, pos + suffix)
        pos += size
    strings = []
    for bit in (0x04, 0x08, 0x10, 0x20):  # nom, chemin relatif, dossier de travail, arguments
        if flags & bit:
            count = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            length = count * 2 if unicode else count
            strings.append(data[pos:pos + length].decode("utf-16-le" if unicode else _codepage(), errors="replace"))
            pos += length
        else:
            strings.append("")
    if not target and strings[1]:
        target = strings[1]  # chemin relatif au raccourci, résolu par l'appelant
    return (target, strings[3]) if target else None


def shortcut(path: Path, steam: str | None) -> Found | None:
    """Application d'un raccourci ``.lnk`` ou ``.url`` (Steam seulement) ; None si ce n'est pas une application."""
    label = path.stem
    if NOT_AN_APP.search(normalize(label)):
        return None
    try:
        if path.suffix.lower() == ".url":
            text = path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^URL=steam://rungameid/(\d{1,10})\s*$", text, re.M)
            return steam_game(label, match.group(1), steam, "raccourci Steam") if match else None
        parsed = read_lnk(path.read_bytes())
    except (OSError, ValueError, struct.error, IndexError):
        return None
    if parsed is None:
        return None
    target, arguments = parsed
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(path.parent, target))
    return _found(label, [target, *split_windows_args(arguments)], "raccourci")


def steam_game(label: str, appid: str, steam: str | None, source: str = "Steam") -> Found | None:
    if not steam or not STEAM_APP.match(appid):
        return None
    found = _found(label, [steam, "-applaunch", appid], source)
    # Le jeu est lancé par Steam : son processus n'est pas connu d'avance.
    return replace(found, identity=f"steam:{appid}", processes=())


# --- Sources Windows -------------------------------------------------------------------------------

def _env_dir(name: str, *parts: str) -> Path | None:
    base = os.environ.get(name)
    return Path(base, *parts) if base else None


def shortcut_dirs() -> list[Path]:
    menu = ("Microsoft", "Windows", "Start Menu", "Programs")
    found = [_env_dir("APPDATA", *menu), _env_dir("ProgramData", *menu), _env_dir("USERPROFILE", "Desktop"),
             _env_dir("PUBLIC", "Desktop")]
    return [d for d in found if d is not None and d.is_dir()]


def _registry_values(root, path: str, names: Iterable[str]) -> dict:
    import winreg

    values = {}
    with winreg.OpenKey(root, path) as key:
        for name in names:
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except OSError:
                pass
    return values


def _subkeys(root, path: str) -> list[str]:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            return [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
    except OSError:
        return []


def registry_apps() -> list[Found]:
    """Programmes installés (clés de désinstallation, icône .exe) et applications déclarées (« App Paths »)."""
    import winreg

    found = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            for sub in _subkeys(root, base):
                try:
                    values = _registry_values(root, f"{base}\\{sub}", ("DisplayName", "DisplayIcon", "SystemComponent"))
                except OSError:
                    continue
                icon = str(values.get("DisplayIcon") or "").split(",")[0].strip().strip('"')
                name = str(values.get("DisplayName") or "")
                if name and icon.lower().endswith(".exe") and not values.get("SystemComponent") and                         not COMPONENT.search(name):
                    found.append(_found(name, [icon], "programme installé"))
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for sub in _subkeys(root, base):
            try:
                program = str(_registry_values(root, f"{base}\\{sub}", ("",)).get("") or "").strip('"')
            except OSError:
                continue
            if program:
                found.append(_found(_title(Path(sub).stem), [os.path.expandvars(program)], "application déclarée"))
    return found


def steam_exe() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        path = _registry_values(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamExe",)).get("SteamExe")
    except OSError:
        return None
    path = os.path.normpath(str(path)) if path else None
    return path if path and os.path.isfile(path) else None


def steam_library(steam: str | None) -> list[Found]:
    """Jeux et logiciels installés par Steam (Wallpaper Engine...), d'après les manifestes des bibliothèques."""
    if not steam:
        return []
    root = Path(steam).parent
    libraries = {root}
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libraries |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)}
    except OSError:
        pass
    found = []
    for library in sorted(libraries):
        for manifest in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid, name = re.search(r'"appid"\s+"(\d+)"', text), re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and not re.search(r"redistributable|steamworks|proton|runtime", name.group(1), re.I):
                game = steam_game(name.group(1), appid.group(1), steam)
                if game:
                    found.append(game)
    return found


def default_browser() -> Found | None:
    """Navigateur par défaut (association https de l'utilisateur), lancé sans argument."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        prog = _registry_values(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                r"\UrlAssociations\https\UserChoice", ("ProgId",)).get("ProgId")
        command = _registry_values(winreg.HKEY_CLASSES_ROOT, f"{prog}\\shell\\open\\command", ("",)).get("")
    except OSError:
        return None
    argv = split_windows_args(str(command or ""))
    if not argv:
        return None
    try:
        name = _registry_values(winreg.HKEY_CLASSES_ROOT, f"{prog}\\Application", ("ApplicationName",))
        label = str(name.get("ApplicationName") or "") or Path(argv[0]).stem
    except OSError:
        label = Path(argv[0]).stem
    if label.startswith("@"):  # nom localisé tiré d'une ressource : le nom du programme à la place
        label = Path(argv[0]).stem
    label = _title(label)
    return _found(label, [argv[0]], "navigateur par défaut")


def _title(name: str) -> str:
    """« opera » -> « Opera » (nom tiré d'un fichier) ; un nom déjà écrit avec des majuscules est gardé."""
    return name[:1].upper() + name[1:] if name.islower() else name


def windows_sources() -> list[Found]:
    if sys.platform != "win32":
        return []
    steam = steam_exe()
    found: list[Found] = []
    for folder in shortcut_dirs():
        for path in [*folder.rglob("*.lnk"), *folder.rglob("*.url")]:
            item = shortcut(path, steam)
            if item is not None:
                found.append(item)
    for source in (registry_apps, lambda: steam_library(steam)):
        try:
            found += source()
        except OSError:
            log.exception("Recherche des applications installées incomplète")
    return found


# --- Recherche par le nom ----------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    words = normalize(text).split()
    return [w for w in words if w not in NOISE] or words  # « Mon App » : rien que des mots ignorés, gardés


def _tier(query: list[str], label: str) -> int:
    """3 : même nom ; 2 : tous les mots demandés dans le nom ; 1 : nom très proche (faute de frappe) ; 0 : non."""
    words = _words(label)
    if not words or not query:
        return 0
    if words == query or "".join(words) == "".join(query):
        return 3
    if all(w in words for w in query):
        return 2
    return 1 if difflib.SequenceMatcher(None, " ".join(query), " ".join(words)).ratio() >= 0.85 else 0


def is_browser(name: str) -> bool:
    return " ".join(_words(name)) in BROWSER_WORDS


class AppFinder:
    def __init__(self, sources: Callable[[], list[Found]] = windows_sources,
                 browser: Callable[[], Found | None] = default_browser, ttl: float = 120.0,
                 clock: Callable[[], float] = time.monotonic):
        self._sources, self._browser, self._ttl, self._clock = sources, browser, ttl, clock
        self._cache: tuple[float, list[Found]] | None = None

    def apps(self) -> list[Found]:
        now = self._clock()
        if self._cache is None or now - self._cache[0] > self._ttl:
            self._cache = (now, list(self._sources()))
        return self._cache[1]

    def find(self, name: str) -> list[Found]:
        """Applications les plus proches du nom (une par identité) ; plusieurs = à préciser, aucune = inconnue."""
        if is_browser(name):
            browser = self._browser()
            return [browser] if browser else []
        query = _words(name)
        best, matches = 0, {}
        for app in self.apps():
            tier = _tier(query, app.label)
            if not tier or tier < best:
                continue
            if tier > best:
                best, matches = tier, {}
            current = matches.get(app.identity)
            # Même programme vu par plusieurs sources : la cible valide plutôt qu'une cible refusée, puis le nom le
            # plus court (« Discord » plutôt que « Discord Inc. Discord »).
            if current is None or (bool(current.problem), len(current.label)) > (bool(app.problem), len(app.label)):
                matches[app.identity] = app
        found = list(matches.values())
        if len(found) > 1 and len({tuple(_words(f.label)) for f in found}) == 1:
            # Même nom partout (raccourci du menu et du bureau vers deux cibles) : impossible à départager par la
            # voix ; la cible valide de la source la plus fiable.
            rank = {source: i for i, source in enumerate(SOURCE_RANK)}
            found = [min(found, key=lambda f: (bool(f.problem), rank.get(f.source, len(rank))))]
        return found
