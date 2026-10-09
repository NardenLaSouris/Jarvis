"""Système d'outils et outils de contrôle du PC : registre, validation, permissions, confirmation,
outils système / applications / audio, sécurité, planificateur et intégration dans l'agent.

Aucun test n'agit réellement sur la machine : lancement, arrêt de processus, volume, verrouillage et
navigateur sont simulés, et toute tentative d'action réelle fait échouer le test (voir ``no_real_actions``).
Les vérifications réelles sont décrites dans la procédure de test manuel du README.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.tools.applications as apps_module  # noqa: E402
import jarvis.tools.audio as audio_module  # noqa: E402
from jarvis.agent import Agent, AgentSettings  # noqa: E402
from jarvis.audio.endpointing import EndpointerSettings, UtteranceRecorder  # noqa: E402
from jarvis.audio.files import ArraySource, RecordingSink  # noqa: E402
from jarvis.capabilities import CapabilityRegistry  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_corrector, build_tools  # noqa: E402
from jarvis.personality import load_personality  # noqa: E402
from jarvis.router import IntentRouter  # noqa: E402
from jarvis.tools import (  # noqa: E402
    CANCELLED, CONFIRM, DONE, REJECTED, ConfirmationManager, Decision, Param, PermissionManager, Risk, Tool,
    ToolCore, ToolError, ToolRegistry, ToolsCapability, builtin_tools, parse_request, plan,
)
from jarvis.tools.applications import Application, load_applications  # noqa: E402
from jarvis.tools.planner import plan_schema, planner_prompt  # noqa: E402

PERSONALITY = load_personality(ROOT / "personality.toml")
CLOCK = lambda: datetime(2026, 9, 26, 13, 42)  # noqa: E731
REAL_FIND_COMMAND = apps_module.find_command
EXPECTED_RISKS = {
    "get_time": Risk.SAFE, "get_date": Risk.SAFE, "system_info": Risk.SAFE, "open_url": Risk.SAFE,
    "open_application": Risk.SAFE, "close_application": Risk.CONFIRMATION_REQUIRED,
    "list_running_applications": Risk.SAFE, "set_volume": Risk.SAFE, "mute_volume": Risk.SAFE,
    "unmute_volume": Risk.SAFE, "lock_pc": Risk.CONFIRMATION_REQUIRED,
    "media_play_pause": Risk.SAFE, "media_next": Risk.SAFE, "media_previous": Risk.SAFE,
}


@pytest.fixture(autouse=True)
def no_real_actions(monkeypatch):
    """Filet de sécurité : aucun test ne lance, n'arrête, ne règle ni ne verrouille réellement quoi que ce soit."""
    def forbidden(*args, **kwargs):
        raise AssertionError("action réelle interdite dans les tests automatiques")

    monkeypatch.setattr(apps_module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(apps_module.subprocess, "run", forbidden)
    monkeypatch.setattr(audio_module.VolumeControl, "set", forbidden)
    monkeypatch.setattr(audio_module.VolumeControl, "set_mute", forbidden)
    monkeypatch.setattr(apps_module, "find_command", lambda entry: [f"C:/Apps/{entry.key}.exe"])
    # Les doublures de processus portent les noms Windows, quelle que soit la machine de test.
    monkeypatch.setattr(apps_module.Application, "processes", property(lambda self: self.app.windows_processes))


# --- Doublures ------------------------------------------------------------------------------------

def echo_tool(name="echo", risk=Risk.SAFE, run=None, **params):
    return Tool(name, "Outil de test.", params or {"text": Param(str, "texte")}, {"text": "texte"}, risk,
                run or (lambda **kw: dict(kw)))


class FakeProcesses:
    """Processus simulés : ``alive`` = noms en cours ; ``stubborn`` = ne s'arrêtent pas."""

    def __init__(self, alive=(), stubborn=False):
        self.alive = set(alive)
        self.stubborn = stubborn
        self.stopped = []

    def running(self, names):
        return any(n in self.alive for n in names)

    def stop(self, names, force):
        self.stopped.append((tuple(names), force))
        if not self.stubborn:
            self.alive -= set(names)


class Launcher:
    """Lanceur simulé ; ``starts`` : processus qui apparaissent après le lancement."""

    def __init__(self, processes=None, starts=(), error=None):
        self.calls = []
        self.processes, self.starts, self.error = processes, set(starts), error

    def __call__(self, argv):
        self.calls.append(argv)
        if self.error:
            raise self.error
        if self.processes is not None:
            self.processes.alive |= self.starts


class Browser:
    def __init__(self, ok=True):
        self.ok, self.opened = ok, []

    def __call__(self, url):
        self.opened.append(url)
        return self.ok


class FakeVolume:
    def __init__(self, level=30, muted=False, stuck=False, broken=False):
        self.level, self.is_muted, self.stuck, self.broken = level, muted, stuck, broken
        self.calls = []

    def _check(self):
        if self.broken:
            raise OSError("périphérique audio absent")

    def get(self):
        self._check()
        return self.level

    def set(self, percent):
        self._check()
        self.calls.append(("set", percent))
        if not self.stuck:
            self.level = percent

    def muted(self):
        self._check()
        return self.is_muted

    def set_mute(self, mute):
        self._check()
        self.calls.append(("mute", mute))
        if not self.stuck:
            self.is_muted = mute


class Locker:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def __call__(self):
        self.calls += 1
        return self.ok


def make_core(settings=None, launcher=None, opener=None, processes=None, volume=None, locker=None, timeout=5.0):
    processes = processes if processes is not None else FakeProcesses()
    registry = ToolRegistry()
    for tool in builtin_tools(settings, clock=CLOCK, opener=opener or Browser(),
                              launcher=launcher or Launcher(processes, ("Discord.exe",)), processes=processes,
                              volume=volume or FakeVolume(), locker=locker or Locker(), open_wait=0.3, close_wait=0.3):
        registry.register(tool)
    confirmations = ConfirmationManager(PERSONALITY.confirm_yes, PERSONALITY.confirm_no,
                                        ignored=(PERSONALITY.assistant_name, *PERSONALITY.former_names,
                                                 PERSONALITY.user_title))
    return ToolCore(registry, PermissionManager(), confirmations, timeout=timeout)


def call(tool, **parameters):
    return {"type": "tool_call", "tool": tool, "parameters": parameters}


# --- Registre ------------------------------------------------------------------------------------

def test_registry_register_get_exists_list():
    registry = ToolRegistry()
    tool = echo_tool()
    registry.register(tool)
    assert registry.get("echo") is tool and registry.exists("echo") and registry.list() == [tool] and len(registry) == 1


def test_registry_unknown_tool():
    registry = ToolRegistry()
    assert not registry.exists("rm") and not registry.exists(None)
    with pytest.raises(ToolError) as err:
        registry.get("rm")
    assert err.value.code == "tool_not_found"


def test_registry_refuses_duplicates_and_bad_definitions():
    registry = ToolRegistry()
    registry.register(echo_tool())
    with pytest.raises(ValueError):
        registry.register(echo_tool())
    for name in ("Echo", "1tool", "rm -rf", "a" * 60, "../x"):
        with pytest.raises(ValueError):
            registry.register(echo_tool(name=name))
    with pytest.raises(ValueError):
        registry.register(echo_tool(name="bad_risk", risk="safe"))


# --- Validation ----------------------------------------------------------------------------------

def registry_with_echo():
    registry = ToolRegistry()
    registry.register(echo_tool(count=Param(int, "nombre", required=False, minimum=0, maximum=10),
                                text=Param(str, "texte", max_length=10)))
    return registry


def test_valid_request():
    request = parse_request(call("echo", text="  bonjour ", count=3), registry_with_echo())
    assert request.tool == "echo" and dict(request.parameters) == {"text": "bonjour", "count": 3}
    assert parse_request(json.dumps(call("echo", text="ok")), registry_with_echo()).parameters == {"text": "ok"}


@pytest.mark.parametrize("data, code", [
    (call("rm", path="/"), "tool_not_found"),
    (call("echo"), "invalid_parameters"),
    (call("echo", text=42), "invalid_parameters"),
    (call("echo", text="ok", count="3"), "invalid_parameters"),
    (call("echo", text="ok", count=True), "invalid_parameters"),
    (call("echo", text="ok", count=11), "invalid_parameters"),
    (call("echo", text="ok", count=-1), "invalid_parameters"),
    (call("echo", text="ok", shell="calc.exe"), "invalid_parameters"),
    (call("echo", text="beaucoup trop long"), "invalid_parameters"),
    (call("echo", text="a\nb"), "invalid_parameters"),
    (call("echo", text="   "), "invalid_parameters"),
    ({"type": "tool_call", "tool": "echo", "parameters": "text=ok"}, "invalid_parameters"),
    ({"type": "answer", "tool": "echo", "parameters": {"text": "ok"}}, "invalid_parameters"),
    ({**call("echo", text="ok"), "confirmed": True}, "invalid_parameters"),
    ({**call("echo", text="ok"), "risk": "safe"}, "invalid_parameters"),
    ({**call("echo", text="ok"), "user": "admin"}, "invalid_parameters"),
    (["echo"], "invalid_parameters"),
    ("pas du json", "invalid_parameters"),
])
def test_invalid_requests_are_rejected(data, code):
    with pytest.raises(ToolError) as err:
        parse_request(data, registry_with_echo())
    assert err.value.code == code


# --- Permissions ---------------------------------------------------------------------------------

def test_permissions_follow_the_risk_level():
    manager = PermissionManager()
    assert manager.decide("owner", echo_tool(), {}).decision is Decision.ALLOW
    assert manager.decide("owner", echo_tool(risk=Risk.CONFIRMATION_REQUIRED), {}).decision is Decision.REQUIRES_CONFIRMATION
    assert manager.decide("owner", echo_tool(risk=Risk.CONFIRMATION_REQUIRED), {}, confirmed=True).decision is Decision.ALLOW
    assert manager.decide("owner", echo_tool(risk=Risk.RESTRICTED), {}).decision is Decision.DENY
    assert manager.decide("owner", echo_tool(risk=Risk.RESTRICTED), {}, confirmed=True).decision is Decision.DENY
    assert manager.decide("inconnu", echo_tool(), {}).decision is Decision.DENY


def test_restricted_tools_are_never_executed():
    ran = []
    registry = ToolRegistry()
    registry.register(echo_tool(name="shutdown", risk=Risk.RESTRICTED, run=lambda **kw: ran.append(kw) or {}))
    outcome = ToolCore(registry).submit(call("shutdown", text="now"))
    assert outcome.status == REJECTED and outcome.result.error == "permission_denied" and ran == []


def test_each_pc_tool_has_the_risk_level_of_the_mission():
    assert {t.name: t.risk for t in make_core().registry.list()} == EXPECTED_RISKS


def test_configuration_can_only_make_tools_stricter(caplog):
    core = make_core({"open_application": {"confirm": True}, "close_application": {"confirm": False},
                      "lock_pc": {"confirm": False}})
    risks = {t.name: t.risk for t in core.registry.list()}
    assert risks["open_application"] is Risk.CONFIRMATION_REQUIRED
    assert risks["close_application"] is Risk.CONFIRMATION_REQUIRED and risks["lock_pc"] is Risk.CONFIRMATION_REQUIRED
    assert "confirm = false ignoré" in caplog.text


# --- Confirmation --------------------------------------------------------------------------------

@pytest.mark.parametrize("tool, parameters", [
    ("get_time", {}), ("open_application", {"application": "discord"}), ("open_url", {"url": "https://example.com"}),
    ("set_volume", {"volume": 40}), ("mute_volume", {}), ("unmute_volume", {}), ("list_running_applications", {}),
    ("system_info", {}),
])
def test_safe_tools_run_without_confirmation(tool, parameters):
    outcome = make_core(processes=FakeProcesses({"Discord.exe"})).submit(call(tool, **parameters))
    assert outcome.status == DONE and outcome.result.success, outcome


@pytest.mark.parametrize("answer", ["Oui.", "Oui Jarvis.", "D'accord.", "Vas-y !", "Oui, monsieur.", "OK"])
def test_confirmation_accepted(answer):
    processes = FakeProcesses({"Discord.exe"})
    core = make_core(processes=processes)
    outcome = core.submit(call("close_application", application="Discord"))
    assert outcome.status == CONFIRM and outcome.question == "Voulez-vous que je ferme Discord ?"
    assert processes.stopped == []
    outcome = core.answer(answer)
    assert outcome.status == DONE and outcome.result.success and processes.stopped == [(("Discord.exe",), True)]


@pytest.mark.parametrize("answer", ["Non.", "Non merci.", "Annule.", "Laisse tomber, Jarvis."])
def test_confirmation_refused(answer):
    locker = Locker()
    core = make_core(locker=locker)
    assert core.submit(call("lock_pc")).status == CONFIRM
    outcome = core.answer(answer)
    assert outcome.status == CANCELLED and outcome.result.error == "confirmation_refused" and locker.calls == 0


def test_any_other_sentence_abandons_the_pending_action():
    locker = Locker()
    core = make_core(locker=locker)
    core.submit(call("lock_pc"))
    assert core.answer("Oui mais d'abord dis-moi l'heure") is None
    assert core.answer("Oui") is None and locker.calls == 0


def test_confirmation_expires_and_nothing_pending_means_no_answer():
    clock = [0.0]
    manager = ConfirmationManager(["oui"], ["non"], ttl=30, clock=lambda: clock[0])
    assert manager.answer("oui") == ("none", None)
    manager.ask(parse_request(call("echo", text="x"), registry_with_echo()), "Sûr ?")
    clock[0] = 31
    assert manager.answer("oui") == ("none", None)


# --- Outils système ------------------------------------------------------------------------------

def test_get_time_and_get_date():
    core = make_core()
    assert core.submit(call("get_time")).result.result == {"time": "13:42", "spoken": "13 heures 42"}
    date = core.submit(call("get_date")).result.result
    assert date == {"date": "2026-09-26", "weekday": "samedi", "spoken": "samedi 26 septembre 2026"}


def test_system_info_is_useful_and_leaks_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SECRET", "sk-ne-doit-jamais-sortir")
    info = make_core().submit(call("system_info")).result.result
    assert set(info) == {"os", "os_version", "architecture", "python", "cpu", "cpu_cores", "memory_total_gb",
                         "memory_available_gb", "disk_total_gb", "disk_free_gb", "gpu", "uptime_hours"}
    dumped = json.dumps(info)
    assert "sk-ne-doit-jamais-sortir" not in dumped and os.environ.get("USERNAME", "§") not in dumped
    assert info["cpu_cores"] >= 1 and isinstance(info["gpu"], list)
    assert info["disk_free_gb"] is None or 0 <= info["disk_free_gb"] <= info["disk_total_gb"]


def test_lock_pc_asks_then_locks():
    locker = Locker()
    core = make_core(locker=locker)
    outcome = core.submit(call("lock_pc"))
    assert outcome.status == CONFIRM and outcome.question == "Voulez-vous que je verrouille l'ordinateur ?"
    assert locker.calls == 0
    result = core.answer("oui").result
    assert result.success and result.result == {"locked": True} and locker.calls == 1


def test_lock_failure_is_reported():
    core = make_core(locker=Locker(ok=False))
    core.submit(call("lock_pc"))
    result = core.answer("oui").result
    assert not result.success and result.error == "execution_failed"


# --- Applications --------------------------------------------------------------------------------

def test_open_application_reports_the_real_state():
    processes = FakeProcesses()
    launcher = Launcher(processes, ("Discord.exe",))
    result = make_core(launcher=launcher, processes=processes).submit(call("open_application", application="Discord")).result
    assert result.success and result.result == {"application": "discord", "label": "Discord", "status": "ouverte"}
    assert launcher.calls == [["C:/Apps/discord.exe"]]


def test_open_application_already_open_or_still_starting():
    processes = FakeProcesses({"steam.exe"})
    result = make_core(launcher=Launcher(processes), processes=processes).submit(call("open_application", application="steam"))
    assert result.result.result["status"] == "déjà ouverte"
    result = make_core(launcher=Launcher(), processes=FakeProcesses()).submit(call("open_application", application="spotify"))
    assert result.result.success and result.result.result["status"] == "lancement en cours"


@pytest.mark.parametrize("said, key", [("VS Code", "vscode"), ("visual studio code", "vscode"),
                                       ("Google Chrome", "chrome"), ("bloc-notes", "notepad"), ("Spotify", "spotify")])
def test_application_aliases(said, key):
    launcher = Launcher()
    make_core(launcher=launcher).submit(call("open_application", application=said))
    assert launcher.calls == [[f"C:/Apps/{key}.exe"]]


def test_application_not_installed(monkeypatch):
    monkeypatch.setattr(apps_module, "find_command", lambda entry: None)
    launcher = Launcher()
    result = make_core(launcher=launcher).submit(call("open_application", application="discord")).result
    assert not result.success and result.error == "application_not_found"
    assert result.message == "Discord n'est pas installé sur cette machine." and launcher.calls == []


def test_launch_error_is_reported():
    result = make_core(launcher=Launcher(error=OSError("accès refusé"))).submit(
        call("open_application", application="chrome")).result
    assert not result.success and result.error == "execution_failed"


def test_close_application_modes():
    processes = FakeProcesses({"Notepad.exe", "Discord.exe"})
    core = make_core(processes=processes)
    core.submit(call("close_application", application="bloc-notes"))
    assert core.answer("oui").result.result == {"application": "notepad", "label": "le Bloc-notes", "closed": True}
    core.submit(call("close_application", application="discord"))
    core.answer("oui")
    assert processes.stopped == [(("Notepad.exe",), False), (("Discord.exe",), True)]


def test_steam_is_closed_with_its_own_shutdown_command(monkeypatch):
    monkeypatch.setattr(apps_module, "find_command", lambda entry: ["C:/Steam/steam.exe"])
    processes = FakeProcesses({"steam.exe"})
    launcher = Launcher()

    def shutdown(argv):
        launcher(argv)
        processes.alive.clear()

    core = make_core(processes=processes, launcher=shutdown)
    core.submit(call("close_application", application="steam"))
    assert core.answer("oui").result.success
    assert launcher.calls == [["C:/Steam/steam.exe", "-shutdown"]] and processes.stopped == []


def test_close_errors():
    core = make_core(processes=FakeProcesses())
    core.submit(call("close_application", application="discord"))
    result = core.answer("oui").result
    assert result.error == "application_not_running" and result.message == "Discord n'est pas ouvert."
    core = make_core(processes=FakeProcesses({"chrome.exe"}, stubborn=True))
    core.submit(call("close_application", application="chrome"))
    result = core.answer("oui").result
    assert result.error == "execution_failed" and result.message == "Google Chrome ne s'est pas fermé complètement."


def test_list_running_applications_only_covers_configured_apps():
    processes = FakeProcesses({"Discord.exe", "svchost.exe", "explorer.exe", "lsass.exe"})
    settings = {"applications": {"discord": {"enabled": True}, "steam": {"enabled": True}, "chrome": {"enabled": False}}}
    result = make_core(settings, processes=processes).submit(call("list_running_applications")).result.result
    assert result == {"applications": [{"application": "discord", "label": "Discord", "running": True},
                                       {"application": "steam", "label": "Steam", "running": False}]}
    assert "svchost" not in json.dumps(result)


def test_open_url_is_immediate():
    browser = Browser()
    outcome = make_core(opener=browser).submit(call("open_url", url="https://www.youtube.com/watch?v=abc"))
    assert outcome.status == DONE and browser.opened == ["https://www.youtube.com/watch?v=abc"]
    assert outcome.result.result == {"url": "https://www.youtube.com/watch?v=abc", "site": "youtube.com", "opened": True}
    failed = make_core(opener=Browser(ok=False)).submit(call("open_url", url="https://example.com")).result
    assert not failed.success and failed.error == "execution_failed"


# --- Configuration des applications ---------------------------------------------------------------

def test_applications_come_from_configuration():
    obs = os.path.abspath("obs/obs64.exe")
    apps = load_applications({
        "discord": {"enabled": True, "executable": "D:/Discord/Discord.exe"},
        "steam": {"enabled": False},
        "obs": {"label": "OBS Studio", "executable": obs, "process": "obs64.exe"},
        "sanschemin": {"process": "x.exe"},
        "relatif": {"executable": "obs64.exe", "process": "obs64.exe"},
        "bad name!": {"executable": "C:/x.exe", "process": "x.exe"},
        "piege": {"executable": "C:/x.exe", "process": "x.exe & calc"},
    })
    assert list(apps) == ["discord", "obs"]
    assert apps["discord"].executable == "D:/Discord/Discord.exe" and apps["obs"].label == "OBS Studio"
    assert load_applications(None).keys() == apps_module.CATALOG.keys()
    core = make_core({"applications": {"discord": {}}})
    assert core.submit(call("open_application", application="steam")).result.error == "invalid_parameters"


def test_find_command_prefers_configured_executable_then_detection(tmp_path, monkeypatch):
    exe = tmp_path / "Discord.exe"
    exe.write_text("")
    entry = Application("discord", apps_module.CATALOG["discord"], str(exe))
    assert REAL_FIND_COMMAND(entry) == [str(exe)]
    monkeypatch.setattr(apps_module, "_app_path", lambda name: None)
    monkeypatch.setattr(apps_module, "_resolve", lambda command: None)
    monkeypatch.setattr(apps_module.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    spotify = Application("spotify", apps_module.CATALOG["spotify"])
    assert REAL_FIND_COMMAND(spotify) is None
    (tmp_path / "Packages" / "SpotifyAB.SpotifyMusic_zpdnekdrzrea0").mkdir(parents=True)
    argv = REAL_FIND_COMMAND(spotify)
    assert argv[0].lower().endswith("explorer.exe")
    assert argv[1] == "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"


# --- Audio ---------------------------------------------------------------------------------------

def test_set_volume_reads_back_the_real_level():
    volume = FakeVolume(level=30)
    result = make_core(volume=volume).submit(call("set_volume", volume=40)).result
    assert result.success and result.result == {"volume": 40, "muted": False} and volume.calls == [("set", 40)]


def test_volume_that_does_not_change_is_a_failure():
    result = make_core(volume=FakeVolume(level=30, stuck=True)).submit(call("set_volume", volume=80)).result
    assert not result.success and result.message == "Le volume est resté à 30 %."
    result = make_core(volume=FakeVolume(broken=True)).submit(call("set_volume", volume=80)).result
    assert result.error == "execution_failed" and result.message == "Je n'ai pas pu régler le volume."


@pytest.mark.parametrize("value", [-1, 101, 150, "50", 50.5, True, None])
def test_volume_out_of_range_or_wrong_type_is_rejected(value):
    volume = FakeVolume()
    outcome = make_core(volume=volume).submit(call("set_volume", volume=value))
    assert outcome.status == REJECTED and outcome.result.error == "invalid_parameters" and volume.calls == []


def test_mute_and_unmute():
    volume = FakeVolume(level=55)
    core = make_core(volume=volume)
    assert core.submit(call("mute_volume")).result.result == {"muted": True, "volume": 55}
    assert core.submit(call("unmute_volume")).result.result == {"muted": False, "volume": 55}
    stuck = make_core(volume=FakeVolume(muted=False, stuck=True)).submit(call("mute_volume")).result
    assert not stuck.success and stuck.error == "execution_failed"


# --- Sécurité ------------------------------------------------------------------------------------

@pytest.mark.parametrize("application", [
    "photoshop", "cmd", "cmd.exe /c calc", "powershell -Command Remove-Item C:\\ -Recurse",
    "C:\\Windows\\System32\\cmd.exe", "../../Windows/System32/calc.exe", "discord && calc", "discord; rm -rf /",
    "notepad.exe", "/bin/sh", "svchost.exe", "explorer", "lsass", "winlogon", "1234", "pid 4", "*",
])
def test_unknown_apps_paths_commands_pids_and_system_processes_are_refused(application):
    processes, launcher = FakeProcesses({"svchost.exe", "explorer.exe"}), Launcher()
    core = make_core(processes=processes, launcher=launcher)
    for tool in ("open_application", "close_application"):
        outcome = core.submit(call(tool, application=application))
        assert outcome.status == REJECTED and outcome.result.error == "invalid_parameters"
    assert launcher.calls == [] and processes.stopped == []


@pytest.mark.parametrize("data", [
    call("close_application", application="discord", pid=1234),
    call("close_application", pid=1234),
    call("open_application", application="discord", path="C:/evil.exe"),
    call("open_application", application="discord", arguments="--inject"),
    call("set_volume", volume=40, device="all"),
    call("lock_pc", force=True),
    call("kill_process", pid=4),
    call("run_command", command="shutdown /s"),
])
def test_extra_parameters_pids_and_unknown_tools_are_refused(data):
    outcome = make_core(processes=FakeProcesses({"Discord.exe"})).submit(data)
    assert outcome.status == REJECTED and outcome.result.error in ("invalid_parameters", "tool_not_found")


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/System32/config/SAM", "javascript:alert(1)", "data:text/html,<script>", "vbscript:msgbox",
    "ms-settings:privacy", "ftp://site.fr/x", "calc.exe", "https://", "https://exa mple.com",
    "https://user:pass@example.com", 'https://example.com/"&calc.exe', "https://example.com/`whoami`",
    "https://example.com/|cmd", "https://example.com:99999/", "https://-bad-.com",
    "https://example.com/" + "a" * 3000, "\\\\serveur\\partage", "https://example.com\n/&calc",
    # Liens qui téléchargent directement un programme (session QA : install.exe ouvert sans un mot).
    "https://evil.example/install.exe", "https://evil.example/dl/Setup.MSI", "https://x.example/a.ps1",
    "https://x.example/run.bat?x=1",
])
def test_invalid_or_dangerous_urls_are_refused(url):
    browser = Browser()
    outcome = make_core(opener=browser).submit(call("open_url", url=url))
    assert outcome.status == REJECTED and outcome.result.error in ("invalid_url", "invalid_parameters")
    assert browser.opened == []


def test_system_tools_are_called_without_shell(monkeypatch):
    seen = []
    monkeypatch.setattr(apps_module.sys, "platform", "win32")
    monkeypatch.setattr(apps_module.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(apps_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(apps_module.subprocess, "run", lambda argv, **options: seen.append((argv, options)))
    monkeypatch.setattr(apps_module.subprocess, "Popen", lambda argv, **options: seen.append((argv, options)))
    apps_module.Processes().stop(("Discord.exe",), force=True)
    apps_module.Processes().stop(("Notepad.exe",), force=False)
    apps_module._launch(["C:/Programmes/app.exe", "--flag"])
    (force, force_opts), (soft, _), (launch, launch_opts) = seen
    assert Path(force[0]).name.lower() == "taskkill.exe" and Path(force[0]).parent.name.lower() == "system32"
    assert force[1:] == ["/IM", "Discord.exe", "/F", "/T"] and soft[1:] == ["/IM", "Notepad.exe"]
    assert force_opts["shell"] is False and launch_opts["shell"] is False
    assert launch == ["C:/Programmes/app.exe", "--flag"]


def test_timeout_and_unexpected_errors_are_structured():
    registry = ToolRegistry()
    registry.register(echo_tool(name="slow", run=lambda **kw: time.sleep(1) or {}))
    registry.register(echo_tool(name="broken", run=lambda **kw: 1 / 0))
    core = ToolCore(registry, timeout=0.1)
    assert core.submit(call("slow", text="x")).result.error == "timeout"
    result = core.submit(call("broken", text="x")).result
    assert result.error == "execution_failed" and "ZeroDivision" not in result.message and "Traceback" not in result.message


def test_tool_results_have_the_expected_shape():
    result = make_core().submit(call("open_application", application="discord")).result.as_dict()
    assert result == {"type": "tool_result", "tool": "open_application", "success": True,
                      "result": {"application": "discord", "label": "Discord", "status": "ouverte"}}
    failure = make_core().submit(call("nope")).result.as_dict()
    assert failure["success"] is False and failure["error"] == "tool_not_found" and "result" not in failure


def test_every_execution_is_logged_without_secrets(caplog):
    caplog.set_level(logging.INFO, logger="jarvis.tools")
    core = make_core()
    core.submit(call("set_volume", volume=40))
    core.submit(call("open_url", url="https://example.com/reset?token=SECRET123#frag"))
    core.submit(call("lock_pc"))
    core.answer("non")
    records = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records if r.name == "jarvis.tools"]
    assert records[0]["tool"] == "set_volume" and records[0]["parameters"] == {"volume": 40}
    assert records[0]["decision"] == "allow" and records[0]["success"] is True and records[0]["user"] == "owner"
    assert records[1]["tool"] == "open_url" and records[1]["success"] is True
    assert records[2]["decision"] == "requires_confirmation" and records[2]["confirmation"] == "asked"
    assert records[3]["confirmation"] == "refused" and records[3]["duration_ms"] >= 0
    assert "SECRET123" not in caplog.text and "frag" not in caplog.text


# --- Planificateur (LLM -> demande structurée) ---------------------------------------------------

class PlannerLLM:
    def __init__(self, *plans, reply="C'est fait."):
        self.plans = list(plans)
        self.reply = reply
        self.planned, self.calls = [], []

    def chat_json(self, messages, schema):
        self.planned.append((messages, schema))
        plan_ = self.plans.pop(0) if self.plans else {"type": "none"}
        if isinstance(plan_, Exception):
            raise plan_
        return plan_

    def chat(self, messages):
        self.calls.append(messages)
        return self.reply


def test_planner_schema_describes_each_registered_tool_exactly():
    registry = make_core({"open_url": {"enabled": False}, "lock_pc": {"enabled": False}}).registry
    options = plan_schema(registry)["anyOf"]
    assert options[0]["properties"]["type"] == {"const": "none"}
    assert options[-1]["properties"]["type"] == {"const": "tool_calls"}
    assert options[-1]["properties"]["calls"]["maxItems"] == 4
    tools = {o["properties"]["tool"]["const"]: o["properties"]["parameters"] for o in options[1:-1]}
    assert "open_url" not in tools and "lock_pc" not in tools and "set_volume" in tools
    assert tools["set_volume"]["properties"] == {"volume": {"type": "integer"}}
    assert tools["set_volume"]["required"] == ["volume"] and tools["set_volume"]["additionalProperties"] is False
    assert tools["mute_volume"]["properties"] == {}
    prompt = planner_prompt(registry)
    assert "open_url" not in prompt and "lock_pc" not in prompt and "set_volume" in prompt


def test_planner_results():
    registry = make_core().registry
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "discord"}})
    assert plan(llm, "Tu peux ouvrir Discord ?", registry)["tool"] == "open_application"
    assert llm.planned[0][0][1].content == "Tu peux ouvrir Discord ?"
    assert plan(PlannerLLM({"type": "none"}), "Éteins l'ordinateur", registry) is None
    assert plan(PlannerLLM(RuntimeError("Ollama")), "Ouvre Discord", registry) is None
    assert plan(object(), "Ouvre Discord", registry) is None


def test_planner_never_invents_a_number():
    registry = make_core().registry
    guess = {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}}
    assert plan(PlannerLLM(guess), "Monte un peu le son", registry) is None
    assert plan(PlannerLLM(guess), "Mets le son à 40 %", registry) is None
    assert plan(PlannerLLM(guess), "Mets le son à 50 %", registry) == guess
    assert plan(PlannerLLM(guess), "Jarvis, monte-moi le son à 50.", registry) == guess


# --- Intégration dans l'agent --------------------------------------------------------------------

SLEEP = "<veille>"


def run_agent(texts, llm, core=None, tools=True, corrector=None, confidence=None, min_confidence=None,
              **agent_options):
    class Stt:
        def __init__(self):
            self.replies = iter([t for t in texts if t != SLEEP])
            self.last_confidence = confidence

        def transcribe(self, audio, rate):
            return next(self.replies)

    class Tts:
        spoken = []

        def synthesize(self, text):
            Tts.spoken.append(text)
            return np.zeros(10, np.int16), 16000

    class Wake:
        def process(self, frame):
            return 1.0 if np.abs(frame).max() > 20000 else 0.0

        def reset(self):
            pass

    sr = 16000
    speech = lambda s: (3000 * np.sin(np.arange(int(s * sr)) / sr * 1400)).astype(np.int16)  # noqa: E731
    silence = lambda s: np.zeros(int(s * sr), np.int16)  # noqa: E731
    parts = [silence(1), np.full(3200, 30000, np.int16), silence(0.5)]
    for text in texts:
        # SLEEP : retour en veille (silence), puis nouveau wake word : une nouvelle conversation commence.
        parts += [silence(4), np.full(3200, 30000, np.int16), silence(0.5)] if text == SLEEP else [speech(1), silence(1.5)]
    source, events = ArraySource(np.concatenate(parts + [silence(3)]), sr, 1280), []
    core = core or make_core()
    capabilities = CapabilityRegistry()
    capabilities.register(ToolsCapability(core.registry))
    names = tuple(t.name for t in core.registry.list()) if tools else ()
    router = IntentRouter(PERSONALITY, capabilities, tools=names)
    Tts.spoken = []
    settings = AgentSettings("JARVIS", "Jarvis", 0.5, ("Oui, monsieur ?",), 3.0, 2.0, 6, min_confidence=min_confidence)
    Agent(settings, source, RecordingSink(), Wake(), UtteranceRecorder(source, EndpointerSettings()), Stt(), llm,
          Tts(), router, lambda kind, text: events.append((kind, text)), tools=core if tools else None,
          corrector=corrector, **agent_options).run()
    return Tts.spoken[1:], events


def result_sent_to_llm(llm, index=-1):
    user = llm.calls[index][-1].content
    return json.loads(user.split("<<<RESULTAT_OUTIL>>>\n")[-1].split("<<<FIN_RESULTAT_OUTIL>>>")[0])


def routes(events):
    return [t for k, t in events if k == "routing"]


def test_request_without_tool_goes_to_the_llm_after_the_planner_says_none():
    llm = PlannerLLM(reply="Voici une blague.")
    spoken, events = run_agent(["Raconte-moi une blague"], llm)
    assert routes(events) == ["llm"] and len(llm.planned) == 1  # le planificateur est consulté, puis répond « none »
    assert spoken == ["Voici une blague."] and "RESULTAT_OUTIL" not in llm.calls[0][-1].content


def test_time_and_date_are_answered_by_the_tool_without_the_llm():
    llm = PlannerLLM()
    spoken, events = run_agent(["Jarvis, quelle heure est-il ?", "Quel jour sommes-nous ?"], llm)
    assert routes(events) == ["tool:time", "tool:date"] and llm.planned == [] and llm.calls == []
    assert spoken[0] == "Il est 13 heures 42." and spoken[1].startswith("Nous sommes le ")


def test_open_discord_by_voice_without_confirmation():
    processes = FakeProcesses()
    launcher = Launcher(processes, ("Discord.exe",))
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "discord"}},
                     reply="Discord est ouvert, monsieur.")
    spoken, events = run_agent(["Jarvis, ouvre Discord."], llm, make_core(launcher=launcher, processes=processes))
    assert spoken == ["Discord est ouvert."] and launcher.calls == [["C:/Apps/discord.exe"]] and llm.calls == []


def test_volume_by_voice():
    volume = FakeVolume(level=20)
    llm = PlannerLLM({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 40}},
                     {"type": "tool_call", "tool": "mute_volume", "parameters": {}}, reply="C'est fait, monsieur.")
    spoken, events = run_agent(["Jarvis, mets le son à 40 %.", "Coupe le son."], llm, make_core(volume=volume))
    assert volume.level == 40 and volume.is_muted and llm.calls == []
    assert spoken == ["Le volume est à 40 %.", "Le son est coupé."]


def test_unmute_by_voice_goes_straight_to_the_tool():
    volume = FakeVolume(muted=True)
    llm = PlannerLLM(reply="Le son est revenu.")
    spoken, events = run_agent(["Jarvis, remets le son."], llm, make_core(volume=volume))
    assert routes(events) == ["tool:unmute"] and llm.planned == [] and not volume.is_muted


def test_close_discord_asks_then_closes():
    processes = FakeProcesses({"Discord.exe"})
    llm = PlannerLLM({"type": "tool_call", "tool": "close_application", "parameters": {"application": "discord"}},
                     reply="Discord est fermé, monsieur.")
    spoken, events = run_agent(["Jarvis, ferme Discord.", "Oui."], llm, make_core(processes=processes))
    assert spoken == ["Voulez-vous que je ferme Discord ?", "Discord est fermé."]
    assert processes.stopped == [(("Discord.exe",), True)] and llm.calls == []


def test_lock_pc_asks_and_respects_the_answer():
    locker = Locker()
    llm = PlannerLLM({"type": "tool_call", "tool": "lock_pc", "parameters": {}},
                     {"type": "tool_call", "tool": "lock_pc", "parameters": {}}, reply="Session verrouillée.")
    spoken, events = run_agent(["Jarvis, verrouille le PC.", "Non.", "Verrouille le PC", "Oui"], llm,
                               make_core(locker=locker))
    assert spoken[0] == spoken[2] == "Voulez-vous que je verrouille l'ordinateur ?"
    assert spoken[1] in [PERSONALITY.render(t) for t in PERSONALITY.phrases["tool_cancelled"]]
    assert locker.calls == 1 and spoken[3] == "L'ordinateur est verrouillé." and llm.calls == []


def test_failed_tool_is_never_reported_as_success(monkeypatch):
    monkeypatch.setattr(apps_module, "find_command", lambda entry: None)
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "discord"}},
                     reply="Discord est ouvert, monsieur.")
    spoken, events = run_agent(["Ouvre Discord"], llm)
    assert llm.calls == [] and spoken == ["Discord n'est pas installé sur cette machine."]
    assert any(kind == "tool" and '"error": "application_not_found"' in text for kind, text in events)


def test_llm_cannot_inject_fields_pids_or_commands():
    processes, launcher, locker = FakeProcesses({"Discord.exe"}), Launcher(), Locker()
    for proposal in ({"type": "tool_call", "tool": "close_application", "parameters": {"application": "discord"},
                      "confirmed": True},
                     {"type": "tool_call", "tool": "close_application", "parameters": {"pid": 4}},
                     {"type": "tool_call", "tool": "open_application", "parameters": {"application": "cmd.exe /c calc"}},
                     {"type": "tool_call", "tool": "run_shell", "parameters": {"command": "calc"}}):
        llm = PlannerLLM(proposal)
        spoken, events = run_agent(["Ferme Discord"], llm, make_core(processes=processes, launcher=launcher,
                                                                     locker=locker))
        assert "Voulez-vous" not in spoken[0]
        # Ouvrir n'est pas demandé (« ferme ») : proposition écartée dès le planificateur, qui laisse alors la main à
        # la conversation ; les autres sont refusées par le Core, sans LLM.
        assert llm.calls == [] or proposal["tool"] == "open_application"
    assert launcher.calls == [] and processes.stopped == [] and locker.calls == 0


def test_unsupported_action_falls_back_to_the_usual_answer():
    llm = PlannerLLM({"type": "none"}, reply="Je ne peux pas éteindre l'ordinateur, monsieur.")
    spoken, events = run_agent(["Ouvre le panneau de configuration et supprime mes fichiers"], llm)
    assert routes(events) == ["tool:tool.action", "unavailable:unavailable_computer (aucun outil)"]
    assert llm.calls == [] and ("aucune commande libre" in spoken[0] or "programme arbitraire" in spoken[0])
    llm = PlannerLLM({"type": "none"}, reply="Voici une idée de cadeau.")
    spoken, events = run_agent(["Ouvre ton cœur et donne-moi une idée de cadeau"], llm)
    assert routes(events) == ["tool:tool.action", "llm (aucun outil)"] and spoken == ["Voici une idée de cadeau."]


def test_new_request_while_waiting_for_confirmation_abandons_the_action():
    locker = Locker()
    llm = PlannerLLM({"type": "tool_call", "tool": "lock_pc", "parameters": {}}, reply="Il est 13 heures 42.")
    spoken, events = run_agent(["Verrouille le PC", "Quelle heure est-il ?"], llm, make_core(locker=locker))
    assert spoken == ["Voulez-vous que je verrouille l'ordinateur ?", "Il est 13 heures 42."] and locker.calls == 0


def test_tools_disabled_keep_the_v1_behaviour():
    llm = PlannerLLM()
    spoken, events = run_agent(["Quelle heure est-il ?", "Ouvre Steam"], llm, tools=False)
    assert routes(events) == ["predefined:time", "unavailable:unavailable_computer"]
    assert llm.planned == [] and llm.calls == []


def test_corrected_transcription_opens_the_right_application():
    launcher = Launcher()
    llm = PlannerLLM({"type": "tool_call", "tool": "open_application", "parameters": {"application": "steam"}},
                     reply="Steam est lancé, monsieur.")
    corrector = build_corrector(load_config(ROOT / "config.toml", local=False), PERSONALITY)
    spoken, events = run_agent(["Jarvis, ou vos teams."], llm, make_core(launcher=launcher), corrector=corrector)
    assert spoken == ["Steam est en cours de lancement."] and launcher.calls == [["C:/Apps/steam.exe"]]
    assert ("correction", "« Jarvis, ou vos teams. » -> « ouvre steam »") in events


def test_configuration_builds_the_core_with_the_mission_risks():
    cfg = load_config(ROOT / "config.toml", local=False)
    core = build_tools(cfg, PERSONALITY)
    assert {t.name: t.risk for t in core.registry.list()} == EXPECTED_RISKS
    assert build_tools(replace(cfg, tools=replace(cfg.tools, enabled=False)), PERSONALITY) is None


def test_capability_description_is_a_short_summary():
    description = ToolsCapability(make_core().registry).description
    assert "régler le son" in description and "get_" not in description
    assert len(description) < 250


def test_tool_replies_speak_of_the_users_timer():
    from jarvis.agent import polish_web_sentence

    assert polish_web_sentence("Mon minuteur a été annulé.", "Annule mon minuteur.", True) == "Votre minuteur a été annulé."


def test_short_follow_up_reuses_the_previous_tool_request():
    first = {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 30}}
    llm = PlannerLLM(first, {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}},
                     reply="Le son est à 30 %.")
    volume = FakeVolume()
    spoken, events = run_agent(["Mets le son à 30 %.", "Et à 50 ?"], llm, make_core(volume=volume))
    # Le complément est compris sans le LLM (contexte de la conversation, voir jarvis.context).
    assert routes(events) == ["tool:tool.action", "tool:context"] and len(llm.planned) == 1
    assert volume.level == 50 and spoken[1] == "Le volume est à 50 %."


def test_follow_up_without_context_rule_still_reuses_the_previous_request():
    first = {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 30}}
    llm = PlannerLLM(first, {"type": "tool_call", "tool": "mute_volume", "parameters": {}})
    spoken, events = run_agent(["Mets le son à 30 %.", "Et coupe-le ensuite ?"], llm, make_core(volume=FakeVolume()))
    assert routes(events)[1] == "tool:tool.follow_up"
    assert llm.planned[1][0][1].content == "Mets le son à 30 % coupe-le ensuite ?"


def test_jarvis_prefix_is_removed_from_tool_replies():
    from jarvis.agent import polish_web_sentence

    assert polish_web_sentence("JARVIS. Votre minuteur est lancé.", "Mets un minuteur.", True) == "Votre minuteur est lancé."


def test_llm_prompts_are_primed_in_background():
    from jarvis.factory import prime_llm
    from jarvis.llm.ollama import OllamaLLM
    from jarvis.tools.planner import planner_prompt

    class PrimedLLM:
        def __init__(self):
            self.prompts = None

        def prime(self, prompts):
            self.prompts = prompts

    core = make_core()
    router = IntentRouter(PERSONALITY, CapabilityRegistry(), tools=tuple(t.name for t in core.registry.list()))
    llm = PrimedLLM()
    prime_llm(llm, router, core.registry).join(5)
    assert [m[0].content[:40] for m in llm.prompts] == [router.system_prompt()[:40], planner_prompt(core.registry)[:40]]
    assert prime_llm(object(), router, core.registry) is None

    sent = []
    ollama = OllamaLLM("http://127.0.0.1:9", "test", max_tokens=200)
    ollama._post = lambda path, payload, timeout=None: sent.append((path, payload, timeout)) or {}
    ollama.prime(llm.prompts)
    assert [(p, d["options"]["num_predict"], d["stream"]) for p, d, _ in sent] == [("/api/chat", 1, False)] * 2
    assert all(t >= 600 for _, _, t in sent)


def test_conversation_moment_is_at_the_end_of_the_system_prompt():
    router = IntentRouter(PERSONALITY, CapabilityRegistry())
    first, ongoing = router.system_prompt(), router.system_prompt(ongoing=True)
    common = next(i for i, (a, b) in enumerate(zip(first, ongoing)) if a != b)
    assert common > 0.9 * len(first)


def test_invented_optional_numbers_are_dropped_but_required_ones_reject_the_call():
    from jarvis.tools.planner import _grounded

    registry = make_core().registry
    assert _grounded({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}}, "Monte le son",
                     registry) is None
    assert _grounded({"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 50}}, "Mets le son à 50",
                     registry)["parameters"] == {"volume": 50}


def test_number_words_count_as_said():
    from jarvis.tools.planner import _grounded

    registry = make_core().registry
    call_ = {"type": "tool_call", "tool": "set_volume", "parameters": {"volume": 100}}
    assert _grounded(call_, "Mets le son à fond", registry) == call_
    assert _grounded(call_, "Mets le son plus fort", registry) is None


# --- Interruption, arrêt et transcriptions douteuses ------------------------------------------------

def test_unsure_transcription_is_not_sent_to_the_llm():
    llm = PlannerLLM(reply="Le gul est un poisson marin.")
    spoken, events = run_agent(["Regarde-moi le gul."], llm, confidence=-1.4, min_confidence=-1.0)
    assert routes(events) == ["unsure"] and llm.calls == []
    assert spoken[0] in [PERSONALITY.render(t) for t in PERSONALITY.phrases["not_understood"]]
    llm = PlannerLLM(reply="Canberra.")
    spoken, events = run_agent(["Quelle est la capitale de l'Australie ?"], llm, confidence=-0.3, min_confidence=-1.0)
    assert routes(events) == ["llm"] and spoken == ["Canberra."]


def test_stop_phrases_end_the_conversation():
    router = IntentRouter(PERSONALITY, CapabilityRegistry())
    for text in ("Ta gueule.", "Arrête.", "Tais-toi !", "Chut.", "La ferme.", "Ça suffit.", "Jarvis, arrête-toi."):
        route = router.route(text)
        assert route.name == "stop" and route.end_conversation, text
    assert router.route("Arrête le minuteur.").name != "stop"


def test_wake_word_during_speech_interrupts_playback():
    import threading

    from jarvis.agent import WakeWatcher

    class Source:
        def __init__(self, frames):
            self.frames = iter(frames)

        def read(self):
            return next(self.frames, None)

    class Detector:
        def process(self, frame):
            return float(frame[0])

        def reset(self):
            pass

    heard = threading.Event()
    scores = []
    watcher = WakeWatcher(Source([np.zeros(4), np.zeros(4), np.full(4, 0.95)]), Detector(), 0.9,
                          lambda score: (scores.append(score), heard.set()))
    watcher.start()
    assert heard.wait(2) and scores == [pytest.approx(0.95)]
    watcher.stop()


def test_wake_word_needs_consecutive_frames_and_logs_rejected_peaks(caplog):
    import logging

    from jarvis.agent import WakeTrigger

    trigger = WakeTrigger(0.7, patience=2)
    with caplog.at_level(logging.INFO):
        assert [trigger.update(s) for s in (0.1, 0.95, 0.2, 0.1)] == [False] * 4
        assert [trigger.update(s) for s in (0.75, 0.9)] == [False, True]
    assert "score 0.95, 1 image(s)" in caplog.text and trigger.detail.startswith("score 0.90, 2 image")
    single = WakeTrigger(0.7)
    assert single.update(0.71) is True


def test_hung_tools_never_block_the_others():
    import threading

    from jarvis.tools.base import Risk, Tool

    core = make_core(timeout=0.3)
    release = threading.Event()
    core.registry.register(Tool("hang", "bloque", {}, {}, Risk.SAFE, lambda: release.wait() and {}))
    try:
        for _ in range(4):
            assert core.submit(call("hang")).result.error == "timeout"
        assert core.submit(call("get_time")).result.success  # aucune file d'exécution saturée
    finally:
        release.set()


def test_unrecognised_action_is_not_called_an_unavailable_feature():
    llm = PlannerLLM({"type": "none"}, reply="C'est fait, la lumière est rallumée.")
    spoken, events = run_agent(["Rallume celle du fond."], llm, make_core())
    assert routes(events)[0] == "tool:tool.action"
    assert "pas encore disponible" not in spoken[0] and ("reformuler" in spoken[0] or "préciser" in spoken[0])


def test_unexpected_result_shape_never_turns_a_done_action_into_a_failure():
    # Session QA : un agent d'une autre version renvoyant un résultat sans la clé attendue faisait annoncer
    # « L'action a échoué » alors que l'action avait eu lieu.
    registry = ToolRegistry()
    done = []
    registry.register(Tool("odd", "outil", {}, {}, Risk.SAFE, lambda: done.append(1) or {"autre": 1},
                           say=lambda r: f"Volume à {r['volume']} %."))
    result = ToolCore(registry, PermissionManager()).submit({"tool": "odd", "parameters": {}}).result
    assert done and result.success and result.message == "C'est fait."


def test_malformed_llm_proposals_are_never_read_aloud():
    # Session QA : « Champs non autorisés dans la demande d'outil. » était prononcé tel quel.
    processes = FakeProcesses({"Discord.exe"})
    llm = PlannerLLM({"type": "tool_call", "tool": "close_application", "parameters": {"application": "discord"},
                      "confirmed": True})
    spoken, events = run_agent(["Ferme Discord"], llm, make_core(processes=processes))
    assert "Champs" not in spoken[0] and "demande d'outil" not in spoken[0] and processes.stopped == []


def test_the_rest_of_a_chained_request_runs_once_the_confirmation_is_accepted():
    # Session QA : « Verrouille le PC et mets le volume à 20 », « oui » : le PC était verrouillé, le volume oublié.
    volume, locker = FakeVolume(), Locker()
    spoken, events = run_agent(["Verrouille le PC et mets le volume à 20", "Oui."], PlannerLLM(),
                               make_core(volume=volume, locker=locker), fast_path=True)
    assert spoken[0].startswith("Voulez-vous") and locker.calls == 1 and volume.level == 20
    volume, locker = FakeVolume(), Locker()
    run_agent(["Verrouille le PC et mets le volume à 20", "Non."], PlannerLLM(),
              make_core(volume=volume, locker=locker), fast_path=True)
    assert locker.calls == 0 and volume.level == 30  # refusé : rien d'autre n'est fait
    volume, locker = FakeVolume(), Locker()
    run_agent(["Verrouille le PC et mets le volume à 20", "Quelle heure est-il ?", "Oui."], PlannerLLM(),
              make_core(volume=volume, locker=locker), fast_path=True)
    assert volume.level == 30 and locker.calls == 0  # autre demande entre-temps : tout ce qui attendait est oublié


def test_nothing_carries_over_to_the_next_conversation():
    # Session QA : « Et demain ? » au début d'une nouvelle conversation reprenait la météo de Lyon de la précédente.
    core = make_core()
    core.registry.register(Tool("get_weather", "météo", {"location": Param(str, "ville", required=False),
                                                         "day": Param(str, "jour", required=False)},
                                {}, Risk.SAFE, lambda **kw: {"ok": True, **kw}, say=lambda r: f"Météo {r}."))
    llm = PlannerLLM(reply="Je ne sais pas de quoi vous parlez.")
    spoken, events = run_agent(["Quel temps fait-il à Lyon ?", SLEEP, "Et demain ?"], llm, core, fast_path=True)
    assert "Lyon" in spoken[0] and "tool.follow_up" not in " ".join(routes(events))
    assert not any("Lyon" in s for s in spoken[1:])


def test_huge_tool_results_are_bounded_before_reaching_the_llm():
    # Session QA : une réponse d'agent de 2 Mo partait entière au LLM (10 s de lecture, réponse confuse).
    from jarvis.tools.base import ToolResult
    from jarvis.tools.planner import MAX_PAYLOAD_FOR_LLM, tool_request

    huge = ToolResult("list_running_applications", True,
                      result={"applications": [f"app{i}" for i in range(10_000)], "note": "A" * 2_000_000})
    message = tool_request("Quelles applications sont ouvertes ?", huge)
    assert len(message) < MAX_PAYLOAD_FOR_LLM + 500 and "tronqué" in message and "autres" in message
    small = ToolResult("read_text_file", True, result={"content": "Acheter du pain. " * 200})
    assert "tronqué" not in tool_request("Lis mes notes", small)  # un texte de fichier lu reste entier


def test_a_plan_left_with_one_action_runs_it_as_a_single_call():
    # Régression : l'action restante gardait son « segment », refusé par le Core (« je n'ai pas compris »).
    processes = FakeProcesses()
    launcher = Launcher(processes, ("Discord.exe",))
    llm = PlannerLLM({"type": "tool_calls", "calls": [
        {"tool": "open_application", "parameters": {"application": "discord"}, "segment": "ouvre Discord"},
        {"tool": "lock_pc", "parameters": {}, "segment": "ouvre Discord"}]})
    spoken, events = run_agent(["Jarvis, ouvre Discord."], llm, make_core(launcher=launcher, processes=processes))
    assert spoken == ["Discord est ouvert."] and launcher.calls == [["C:/Apps/discord.exe"]]


@pytest.mark.parametrize("url", ["https://example.com/telechargements", "https://github.com/user/projet.js",
                                 "https://example.com/exercice.html", "https://example.com/?file=a.exe"])
def test_ordinary_pages_still_open_without_confirmation(url):
    browser = Browser()
    outcome = make_core(opener=browser).submit(call("open_url", url=url))
    assert outcome.status == "done" and outcome.result.success and browser.opened
