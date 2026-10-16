"""Migration du nom JARVIS -> ORION : anciens secrets toujours lus, anciens points d entrée toujours valides."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orion.config import secret  # noqa: E402


def test_renamed_secrets_are_still_read_under_their_old_name(tmp_path, monkeypatch):
    for name in ("ORION_AGENT_TOKEN", "JARVIS_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("JARVIS_AGENT_TOKEN=ancien-nom-0123456789\n", encoding="utf-8")  # .env d avant la migration
    assert secret("ORION_AGENT_TOKEN", env) == "ancien-nom-0123456789"
    env.write_text("JARVIS_AGENT_TOKEN=ancien\nORION_AGENT_TOKEN=nouveau\n", encoding="utf-8")
    assert secret("ORION_AGENT_TOKEN", env) == "nouveau"  # le nouveau nom l emporte
    assert secret("ORION_INCONNU", env) == ""


@pytest.mark.parametrize("module", ["jarvis", "jarvis.winagent", "orion", "orion.winagent"])
def test_old_and_new_entry_points_start_orion(module):
    # Service systemd et raccourcis Windows installés avant la migration lancent encore « -m jarvis… ».
    out = subprocess.run([sys.executable, "-m", module, "--help"], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert "ORION" in out.stdout


def test_the_compatibility_package_only_forwards():
    shim = ROOT / "jarvis"
    files = sorted(p.relative_to(shim).as_posix() for p in shim.rglob("*.py"))
    assert files == ["__init__.py", "__main__.py", "call/__init__.py", "call/gateway.py", "control/__init__.py",
                     "control/__main__.py", "winagent/__init__.py", "winagent/__main__.py"]
    for path in shim.rglob("*.py"):
        code = path.read_text(encoding="utf-8")
        assert "def " not in code and "class " not in code, path  # aucun code propre : un renvoi vers orion
