"""Hygiène des secrets : rien de secret ne peut entrer dans le dépôt (vérifié sur les fichiers suivis par Git)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "clé privée": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jeton GitHub": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "clé API style sk-": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "clé AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "jeton Hugging Face": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "secret affecté en clair": re.compile(r"^\s*(MAIL_PASSWORD|JARVIS_AGENT_TOKEN|HF_TOKEN|LIGHT_KEY_\w+)\s*=\s*\S{6,}",
                                          re.MULTILINE),
}


def tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ROOT / line for line in out.stdout.splitlines() if line]


def test_secret_files_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and "*.local.toml" in ignored


def test_no_secret_in_tracked_files():
    found = []
    for path in tracked():
        if path.suffix in (".onnx", ".npy", ".wav", ".mp3", ".png", ".woff2", ".ico", ".jpg") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        found += [f"{path.relative_to(ROOT)} : {name}" for name, pattern in PATTERNS.items() if pattern.search(text)]
    assert found == []
