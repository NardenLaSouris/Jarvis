"""Diagnostics : jamais de plantage, état lisible, code de sortie selon les composants indispensables.

Configuration de test sans réseau joignable (ports fermés) : rien n'est contacté hors de la machine.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import diagnostics  # noqa: E402
from jarvis.config import load_config  # noqa: E402


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def offline_config(tmp_path):
    cfg = load_config(ROOT / "config.toml", local=False)
    port = closed_port()
    return replace(cfg, llm=replace(cfg.llm, host=f"http://127.0.0.1:{port}"),
                   web=replace(cfg.web, base_url=f"http://127.0.0.1:{port}"),
                   memory=replace(cfg.memory, path=tmp_path / "memory.json"),
                   timers=replace(cfg.timers, path=tmp_path / "schedule.json"),
                   routines=replace(cfg.routines, path=tmp_path / "routines.json"))


def test_a_failing_check_never_crashes():
    check = diagnostics._timed("x", True, lambda: 1 / 0)
    assert check.status == diagnostics.FAIL and "ZeroDivisionError" in check.detail


def test_health_reports_an_unreachable_llm_as_critical(tmp_path, capsys):
    cfg = offline_config(tmp_path)
    checks = {c.name: c for c in diagnostics.run_health(cfg)}
    assert checks["LLM principal (worker)"].status == diagnostics.FAIL
    assert "injoignable" in checks["LLM principal (worker)"].detail
    assert checks["Recherche Web (SearXNG)"].status == diagnostics.FAIL
    assert checks["Mémoire"].status == diagnostics.OK
    assert diagnostics.print_checks(list(checks.values())) == 1
    assert "indispensable" in capsys.readouterr().out


def test_health_reads_the_json_stores(tmp_path):
    cfg = offline_config(tmp_path)
    (tmp_path / "memory.json").write_text('[{"id": "a", "text": "x"}]', encoding="utf-8")
    (tmp_path / "routines.json").write_text("{cassé", encoding="utf-8")
    checks = {c.name: c for c in diagnostics.run_health(cfg)}
    assert checks["Mémoire"].detail == "faits retenus : 1 élément(s)"
    assert checks["Routines"].status == diagnostics.FAIL


def test_cli_options_exist():
    import jarvis.__main__ as main

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert all(option in source for option in ("--health", "--diagnostics", "--benchmark"))
