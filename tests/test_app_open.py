"""Ouverture de toute application installée sauf celles de la liste interdite ([tools.blocked_applications]).

Découverte, validation et lancement isolés par des doubles : aucune application n'est ouverte, aucune commande
exécutée, le vrai menu Démarrer n'est jamais lu.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orion.tools.applications as apps_module  # noqa: E402
from orion.tools.app_finder import (  # noqa: E402
    AppFinder, Found, _found, read_lnk, shortcut, split_windows_args, steam_game, steam_library, store_app,
)
from orion.tools.planner import plan  # noqa: E402
from orion.tools.quick import quick_plan  # noqa: E402
from test_tools import CONFIRM, REJECTED, FakeProcesses, Launcher, PlannerLLM, call, make_core  # noqa: E402


@pytest.fixture(autouse=True)
def known_apps_installed(monkeypatch):
    """Applications connues (catalogue) trouvées à un chemin fictif, comme dans test_tools."""
    monkeypatch.setattr(apps_module, "find_command", lambda entry: [f"/apps/{entry.key}.exe"])


def exe(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return str(path)


def core_with(apps: list[Found], settings=None, launcher=None, processes=None, browser=None):
    processes = processes if processes is not None else FakeProcesses()
    launcher = launcher if launcher is not None else Launcher(processes)
    finder = AppFinder(lambda: apps, lambda: browser)
    return make_core(settings, launcher=launcher, processes=processes, finder=finder), launcher


def opened(core, name):
    return core.submit(call("open_application", application=name)).result


# --- Ouvrir une application installée ------------------------------------------------------------

def test_an_installed_application_absent_from_the_blocklist_is_opened(tmp_path):
    steam = exe(tmp_path, "Steam/steam.exe")
    obs = exe(tmp_path, "obs-studio/bin/64bit/obs64.exe")
    processes = FakeProcesses()
    core, launcher = core_with([steam_game("Wallpaper Engine", "431960", steam),
                                _found("OBS Studio", [obs], "raccourci")],
                               launcher=Launcher(processes, ("obs64.exe",)), processes=processes)
    result = opened(core, "obs studio")
    assert result.success and result.result["status"] == "ouverte" and result.message == "OBS Studio est ouvert."
    result = opened(core, "Wallpaper Engine")
    assert result.success and result.result["label"] == "Wallpaper Engine"
    assert result.result["status"] == "lancement en cours"  # lancé par Steam : jamais annoncé « ouvert » d'avance
    assert result.message == "Wallpaper Engine est en cours de lancement."
    assert launcher.calls == [[obs], [steam, "-applaunch", "431960"]]


def test_known_applications_keep_their_tested_commands():
    core, launcher = core_with([])
    result = opened(core, "VS Code")
    assert result.success and result.result["application"] == "vscode" and launcher.calls == [["/apps/vscode.exe"]]


def test_my_browser_is_the_default_browser(tmp_path):
    firefox = exe(tmp_path, "Mozilla Firefox/firefox.exe")
    core, launcher = core_with([], browser=_found("Firefox", [firefox], "navigateur par défaut"))
    assert opened(core, "mon navigateur").result["label"] == "Firefox" and launcher.calls == [[firefox]]
    core, _ = core_with([])
    assert opened(core, "navigateur").message == "Je ne trouve pas de navigateur par défaut sur ce PC."


def test_close_names_typos_and_noise_words_are_tolerated(tmp_path):
    obs = exe(tmp_path, "obs64.exe")
    core, launcher = core_with([_found("OBS Studio", [obs], "raccourci")])
    assert opened(core, "l'application OBS").success  # « l'application » ignoré, « OBS » contenu dans le nom
    assert opened(core, "OBS Studioo").success  # faute de frappe
    assert launcher.calls == [[obs], [obs]]


def test_the_same_program_seen_twice_is_not_ambiguous(tmp_path):
    obs = exe(tmp_path, "obs64.exe")
    core, launcher = core_with([_found("OBS Studio", [obs], "raccourci"),
                                _found("OBS Studio (64bit)", [obs], "programme installé")])
    assert opened(core, "OBS Studio").success and launcher.calls == [[obs]]


# --- Liste interdite -----------------------------------------------------------------------------

def test_a_blocked_application_is_never_launched(tmp_path):
    steam = exe(tmp_path, "Steam/steam.exe")
    obs = exe(tmp_path, "obs64.exe")
    tor = exe(tmp_path, "Tor Browser/firefox.exe")
    settings = {"blocked_applications": {"steam": ["431960"], "executables": ["OBS64.exe"], "paths": [tor]}}
    core, launcher = core_with([steam_game("Wallpaper Engine", "431960", steam),
                                _found("OBS Studio", [obs], "raccourci"),
                                _found("Tor Browser", [tor], "raccourci")], settings)
    for name, label in (("Wallpaper Engine", "Wallpaper Engine"), ("OBS", "OBS Studio"), ("Tor", "Tor Browser")):
        result = opened(core, name)
        assert not result.success and result.error == "application_blocked"
        assert result.message == f"{label} fait partie des applications que je n'ouvre pas."
    assert launcher.calls == []


def test_the_blocklist_checks_the_resolved_target_not_the_name(tmp_path):
    tool = exe(tmp_path, "outils/capture.exe")
    # Même programme sous un autre nom de raccourci : toujours interdit (chemin réel comparé).
    core, launcher = core_with([_found("Mon super lecteur", [tool], "raccourci")],
                               {"blocked_applications": {"paths": [str(tmp_path / "outils" / ".." / "outils" /
                                                                       "capture.exe")]}})
    assert opened(core, "super lecteur").error == "application_blocked" and launcher.calls == []


def test_system_administration_tools_are_blocked_by_default(tmp_path):
    regedit = exe(tmp_path, "Windows/regedit.exe")
    core, launcher = core_with([_found("Éditeur du Registre", [regedit], "raccourci")])
    assert opened(core, "éditeur du registre").error == "application_blocked" and launcher.calls == []


def test_blocking_the_launcher_blocks_what_it_launches(tmp_path):
    steam = exe(tmp_path, "Steam/steam.exe")
    core, launcher = core_with([steam_game("Wallpaper Engine", "431960", steam)],
                               {"blocked_applications": {"executables": ["steam.exe"]}})
    assert opened(core, "Wallpaper Engine").error == "application_blocked" and launcher.calls == []


def test_a_configured_application_cannot_be_an_interpreter(monkeypatch):
    monkeypatch.setattr(apps_module, "find_command", lambda entry: ["/windows/system32/cmd.exe", "/c", "calc"])
    core, launcher = core_with([], {"applications": {"outil": {"executable": "/windows/system32/cmd.exe",
                                                                "process": "cmd.exe"}}})
    assert opened(core, "outil").error == "invalid_target" and launcher.calls == []


def test_a_known_application_can_be_blocked_too():
    core, launcher = core_with([], {"blocked_applications": {"paths": ["/apps/discord.exe"]}})
    assert opened(core, "Discord").error == "application_blocked" and launcher.calls == []


# --- Ambiguïté, absence, cibles invalides ----------------------------------------------------------

def test_several_matching_applications_lead_to_a_question(tmp_path):
    word, excel = exe(tmp_path, "Office/WINWORD.EXE"), exe(tmp_path, "Office/EXCEL.EXE")
    core, launcher = core_with([_found("Microsoft Word", [word], "raccourci"),
                                _found("Microsoft Excel", [excel], "raccourci")])
    result = opened(core, "Microsoft")
    assert result.error == "ambiguous_target" and launcher.calls == []
    assert result.message == "Plusieurs applications correspondent : « Microsoft Excel », « Microsoft Word ». Laquelle ?"
    assert opened(core, "Microsoft Word").success and launcher.calls == [[word]]


def test_no_matching_application_is_said_plainly(tmp_path):
    core, launcher = core_with([_found("OBS Studio", [exe(tmp_path, "obs64.exe")], "raccourci")])
    result = opened(core, "Photoshop")
    assert result.error == "application_not_found" and launcher.calls == []
    assert result.message == "Je ne trouve pas d'application « Photoshop » sur ce PC."


@pytest.mark.parametrize("argv, problem", [
    (["/nulle/part/absent.exe"], "fichier introuvable"),
    (["relatif/app.exe"], "chemin non absolu"),
    (["%TEMP%/app.exe"], "chemin non absolu"),
])
def test_missing_or_invalid_paths_are_never_launched(argv, problem):
    found = _found("Appli fantôme", argv, "raccourci")
    assert found.problem == problem
    core, launcher = core_with([found])
    result = opened(core, "Appli fantôme")
    assert result.error == "invalid_target" and launcher.calls == []


@pytest.mark.parametrize("name, problem", [
    ("lancer.bat", "pas un programme (.exe)"), ("script.ps1", "pas un programme (.exe)"),
    ("cmd.exe", "interpréteur de commandes ou de scripts"), ("powershell.exe", "interpréteur de commandes ou de scripts"),
    ("mshta.exe", "interpréteur de commandes ou de scripts"), ("unins000.exe", "installeur ou désinstalleur"),
])
def test_shortcuts_to_scripts_interpreters_or_uninstallers_are_refused(tmp_path, name, problem):
    target = exe(tmp_path, name)
    link = tmp_path / "Mon outil.lnk"
    link.write_bytes(make_lnk(target, "/c del C:\\*"))
    found = shortcut(link, None)
    assert found.problem == problem
    core, launcher = core_with([found])
    result = opened(core, "mon outil")
    assert result.error == "invalid_target" and launcher.calls == []
    assert result.message.startswith("Je ne lance pas Mon outil : son raccourci ne mène pas à un programme ordinaire")


def test_a_valid_target_wins_over_a_refused_one_with_the_same_name(tmp_path):
    good, bad = exe(tmp_path, "App/app.exe"), exe(tmp_path, "App/app.bat")
    core, launcher = core_with([_found("Mon App", [bad], "raccourci"), _found("Mon App", [good], "raccourci")])
    assert opened(core, "Mon App").success and launcher.calls == [[good]]


def test_help_and_uninstall_shortcuts_are_not_applications(tmp_path):
    target = exe(tmp_path, "app.exe")
    for label in ("Désinstaller Mon App", "Uninstall Mon App", "Mon App - Aide", "Site web de Mon App"):
        link = tmp_path / f"{label}.lnk"
        link.write_bytes(make_lnk(target, ""))
        assert shortcut(link, None) is None


# --- Injection et caractères spéciaux --------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "discord && calc", "discord; rm -rf /", "C:\\Windows\\System32\\cmd.exe", "../../Windows/calc.exe",
    'obs" & calc', "$(calc)", "`calc`", "obs | calc", "obs > fichier", "%COMSPEC%", "steam://rungameid/1",
    "-applaunch 1", "obs\n calc", "*",
])
def test_command_injection_in_the_name_is_refused(tmp_path, name):
    core, launcher = core_with([_found("OBS Studio", [exe(tmp_path, "obs64.exe")], "raccourci")])
    outcome = core.submit(call("open_application", application=name))
    assert outcome.status == REJECTED and outcome.result.error == "invalid_parameters" and launcher.calls == []


def test_shortcut_arguments_with_special_characters_stay_structured(tmp_path):
    target = exe(tmp_path, "Mon Jeu/jeu.exe")
    link = tmp_path / "Mon Jeu.lnk"
    link.write_bytes(make_lnk(target, '--profil "Mon Profil" --cle=a&b "dit \\"bonjour\\"" C:\\a\\\\'))
    core, launcher = core_with([shortcut(link, None)])
    assert opened(core, "Mon Jeu").success
    assert launcher.calls == [[target, "--profil", "Mon Profil", "--cle=a&b", 'dit "bonjour"', "C:\\a\\\\"]]


def test_windows_argument_rules():
    assert split_windows_args('a "b c" d') == ["a", "b c", "d"]
    assert split_windows_args('"" x') == ["", "x"]
    assert split_windows_args('a\\\\"b c"') == ["a\\b c"]
    assert split_windows_args("a\\\\b") == ["a\\\\b"]
    assert split_windows_args('"a""b"') == ['a"b']


# --- Échecs, permissions, raccourci vocal ------------------------------------------------------------

def test_a_launch_error_is_reported_with_its_reason(tmp_path):
    obs = exe(tmp_path, "obs64.exe")
    core, launcher = core_with([_found("OBS Studio", [obs], "raccourci")],
                               launcher=Launcher(error=PermissionError(13, "Accès refusé")))
    result = opened(core, "OBS Studio")
    assert not result.success and result.error == "execution_failed"
    assert result.message == "Je n'ai pas pu lancer OBS Studio (Accès refusé)."


def test_existing_permissions_and_confirmations_are_kept(tmp_path):
    obs = exe(tmp_path, "obs64.exe")
    core, launcher = core_with([_found("OBS Studio", [obs], "raccourci")], {"open_application": {"confirm": True}})
    outcome = core.submit(call("open_application", application="OBS Studio"))
    assert outcome.status == CONFIRM and outcome.question == "Voulez-vous que j'ouvre OBS Studio ?"
    assert launcher.calls == []
    assert core.answer("oui").result.success and launcher.calls == [[obs]]
    # Fermer reste limité aux applications connues, avec confirmation.
    assert core.submit(call("close_application", application="OBS Studio")).result.error == "invalid_parameters"


def test_the_llm_cannot_open_an_application_that_was_not_asked_for():
    core, _ = core_with([])
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "regedit"}})
    assert plan(llm, "Ouvre Steam", core.registry) is None
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "vscode"}})
    assert plan(llm, "Ouvre VS Code", core.registry)["parameters"] == {"application": "vscode"}


def test_without_llm_only_known_applications_are_opened():
    core, _ = core_with([])
    assert quick_plan("Ouvre Discord", core.registry)["parameters"] == {"application": "discord"}
    for text in ("Lance un diagnostic", "Ouvre la porte", "Lance Wallpaper Engine"):
        found = quick_plan(text, core.registry)
        assert found is None or found["tool"] != "open_application", text


# --- Sources ----------------------------------------------------------------------------------------

def make_lnk(target: str, arguments: str) -> bytes:
    """Raccourci minimal (MS-SHLLINK) : LinkInfo avec chemin local ANSI, arguments en UTF-16."""
    header = struct.pack("<I16sIIQQQIIIHHII", 0x4C, b"\x01\x14\x02\0\0\0\0\0\xc0\0\0\0\0\0\0F",
                         0x02 | 0x20 | 0x80, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0)
    volume = struct.pack("<IIII", 17, 3, 0, 16) + b"\0"
    base = target.encode("cp1252") + b"\0"
    info_header = 0x1C
    size = info_header + len(volume) + len(base) + 1
    info = struct.pack("<7I", size, info_header, 1, info_header, info_header + len(volume), 0,
                       info_header + len(volume) + len(base)) + volume + base + b"\0"
    args = struct.pack("<H", len(arguments)) + arguments.encode("utf-16-le")
    return header + info + args


def test_shortcut_target_and_arguments_are_read(tmp_path):
    target = exe(tmp_path, "Discord/Update.exe")
    assert read_lnk(make_lnk(target, "--processStart Discord.exe")) == (target, "--processStart Discord.exe")
    link = tmp_path / "Discord.lnk"
    link.write_bytes(make_lnk(target, "--processStart Discord.exe"))
    found = shortcut(link, None)
    assert found.argv == (target, "--processStart", "Discord.exe") and not found.problem
    assert read_lnk(b"pas un raccourci") is None


def test_steam_games_come_from_the_library_manifests(tmp_path):
    steam = exe(tmp_path, "Steam/steam.exe")
    other = tmp_path / "D" / "SteamLibrary"
    (tmp_path / "Steam" / "steamapps").mkdir(parents=True)
    (other / "steamapps").mkdir(parents=True)
    (tmp_path / "Steam" / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n "1"\n {\n  "path"  "' + str(other) + '"\n }\n}\n', encoding="utf-8")
    (other / "steamapps" / "appmanifest_431960.acf").write_text(
        '"AppState"\n{\n "appid" "431960"\n "name" "Wallpaper Engine"\n}\n', encoding="utf-8")
    (other / "steamapps" / "appmanifest_228980.acf").write_text(
        '"AppState"\n{\n "appid" "228980"\n "name" "Steamworks Common Redistributables"\n}\n', encoding="utf-8")
    games = steam_library(steam)
    assert [(g.label, g.argv, g.identity) for g in games] == [
        ("Wallpaper Engine", (steam, "-applaunch", "431960"), "steam:431960")]
    url = tmp_path / "Wallpaper Engine.url"
    url.write_text("[InternetShortcut]\nURL=steam://rungameid/431960\n", encoding="utf-8")
    assert shortcut(url, steam).identity == "steam:431960"
    url.write_text("[InternetShortcut]\nURL=https://example.com/\n", encoding="utf-8")
    assert shortcut(url, steam) is None  # lien Web : pas une application
    assert steam_game("Piège", "1 & calc", steam) is None


def test_two_entries_with_the_very_same_name_are_not_a_question(tmp_path):
    update, app = exe(tmp_path, "Discord/Update.exe"), exe(tmp_path, "Discord/app-1.0/Discord.exe")
    finder = AppFinder(lambda: [_found("Discord", [app], "programme installé"),
                                _found("Discord", [update, "--processStart", "Discord.exe"], "raccourci")],
                       lambda: None)
    assert [f.argv[0] for f in finder.find("Discord")] == [update]  # le raccourci, source la plus fiable


def test_acronyms_find_the_game(tmp_path):
    steam = exe(tmp_path, "Steam/steam.exe")
    core, launcher = core_with([steam_game("Counter-Strike 2", "730", steam),
                                _found("Counter-Strike 2 Tracker", [exe(tmp_path, "tracker.exe")], "raccourci")])
    for name in ("CS", "CS2", "cs 2"):
        assert opened(core, name).result["label"] == "Counter-Strike 2", name
    assert opened(core, "Counter-Strike").error == "ambiguous_target"  # le jeu ou son outil : question
    assert launcher.calls == [[steam, "-applaunch", "730"]] * 3


def test_store_and_game_pass_applications(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    forza = store_app("Forza Horizon 6", "Microsoft.ForteBaseGame_8wekyb3d8bbwe!Forzahorizon6")
    assert forza.argv == (str(tmp_path / "explorer.exe"),
                          "shell:AppsFolder\\Microsoft.ForteBaseGame_8wekyb3d8bbwe!Forzahorizon6")
    assert forza.identity == "store:Microsoft.ForteBaseGame_8wekyb3d8bbwe" and not forza.problem
    for bad in ("steam://rungameid/730", "overwolf.anicjdiagpncphndefggchijaioofjmigjnpkjde", "x_8wekyb3d8bbwe!a b",
                "C:\\Windows\\cmd.exe", "a_8wekyb3d8bbwe!x & calc"):
        assert store_app("Piège", bad) is None, bad
    core, launcher = core_with([forza])
    assert opened(core, "Forza").result["label"] == "Forza Horizon 6" and launcher.calls == [list(forza.argv)]
    core, launcher = core_with([forza], {"blocked_applications": {"store": ["Microsoft.ForteBaseGame_8wekyb3d8bbwe"]}})
    assert opened(core, "Forza Horizon").error == "application_blocked" and launcher.calls == []


def test_the_installed_list_is_read_once_then_cached():
    calls = []
    clock = [0.0]

    def sources():
        calls.append(1)
        return []

    finder = AppFinder(sources, lambda: None, ttl=120, clock=lambda: clock[0])
    finder.find("a")
    finder.find("b")
    clock[0] = 121
    finder.find("c")
    assert len(calls) == 2
