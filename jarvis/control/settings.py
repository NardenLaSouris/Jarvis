"""Paramètres d'ORION Control (%APPDATA%\\ORION Control) ; le jeton est rangé à part et jamais renvoyé à l'interface.

Sans jeton enregistré, celui du dépôt est utilisé (JARVIS_AGENT_TOKEN dans .env, le même que pour l'agent).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from jarvis.config import secret

log = logging.getLogger(__name__)

REPO_ENV = Path(__file__).resolve().parents[2] / ".env"
# Thèmes du visage d'ORION (jour bleu, nuit noir et blanc, rouge comme le visage en erreur ; « auto » : nuit de 22 h à 7 h comme le visage),
# plus les thèmes génériques.
THEMES = ("auto", "visage", "jarvis", "nuit", "rouge", "system", "dark", "light")


def app_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home())
    folder, legacy = base / "ORION Control", base / "JARVIS Control"
    if not folder.exists() and legacy.is_dir():  # réglages et journal de JARVIS Control repris tels quels
        try:
            import shutil

            shutil.copytree(legacy, folder)
        except OSError:
            pass
    return folder


@dataclass
class Settings:
    core_url: str = "http://192.168.1.91:8766"
    pc_name: str = "PC fixe"
    theme: str = "auto"
    autostart: bool = False
    notifications: bool = True
    auto_connect: bool = True
    refresh_seconds: int = 15

    def validated(self) -> "Settings":
        url = self.core_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")) or " " in url:
            raise ValueError("Adresse du Core : http://<adresse>:<port> attendue.")
        if self.theme not in THEMES:
            raise ValueError("Thème inconnu.")
        if not 5 <= int(self.refresh_seconds) <= 3600:
            raise ValueError("Rafraîchissement : de 5 à 3600 secondes.")
        name = self.pc_name.strip()[:40] or "PC fixe"
        return Settings(url, name, self.theme, bool(self.autostart), bool(self.notifications), bool(self.auto_connect),
                        int(self.refresh_seconds))


class SettingsStore:
    def __init__(self, folder: Path | None = None, env_file: Path = REPO_ENV):
        self.folder = Path(folder) if folder else app_dir()
        self._env_file = env_file

    @property
    def _settings_file(self) -> Path:
        return self.folder / "settings.json"

    @property
    def _token_file(self) -> Path:
        return self.folder / "token"

    def load(self) -> Settings:
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8-sig"))
            known = {f.name for f in fields(Settings)}
            return Settings(**{k: v for k, v in data.items() if k in known}).validated()
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Paramètres illisibles, valeurs par défaut utilisées : %s", exc)
            return Settings()

    def save(self, settings: Settings) -> Settings:
        settings = settings.validated()
        self.folder.mkdir(parents=True, exist_ok=True)
        self._settings_file.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        return settings

    def token(self) -> str:
        try:
            saved = self._token_file.read_text(encoding="utf-8-sig").strip()
        except OSError:
            saved = ""
        return saved or secret("JARVIS_AGENT_TOKEN", self._env_file)

    def set_token(self, token: str) -> None:
        token = token.strip()
        if len(token) < 32 or not token.isprintable() or " " in token:
            raise ValueError("Jeton invalide (32 caractères au moins, sans espace).")
        self.folder.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token, encoding="utf-8")
