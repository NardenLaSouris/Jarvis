"""Appareils mobiles jumelés à la passerelle d'appel, et codes de jumelage à usage unique.

- Un code de jumelage est créé sur le mini-PC (``python -m orion.call.gateway --pair``) : 8 caractères, valable
  ``PAIRING_TTL`` secondes, utilisable une seule fois, jamais journalisé en entier.
- Le téléphone l'échange contre un identifiant et un jeton (32 octets aléatoires). Seule l'empreinte SHA-256 du
  jeton est gardée (fichier en 600) : une copie du fichier ne permet pas de se faire passer pour le téléphone.
- Trop de codes faux d'affilée : le jumelage est bloqué un moment (aucune devinette possible).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from orion.persist import load_json_list

PAIRING_TTL = 300.0
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sans 0/O ni 1/I/L : se recopie sans erreur
MAX_FAILURES = 5
LOCKOUT = 600.0


class PairingError(ValueError):
    pass


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Device:
    id: str
    name: str
    token_sha256: str
    paired_at: float
    last_seen: float = 0.0


class DeviceRegistry:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time):
        self._path = Path(path)
        self._clock = clock
        self._devices: dict[str, Device] = {}
        self._codes: dict[str, float] = {}  # code -> expiration
        self._failures = 0
        self._locked_until = 0.0
        for item in load_json_list(self._path, "appareils jumelés"):
            try:
                device = Device(**item)
            except TypeError:
                continue
            self._devices[device.id] = device

    # --- Jumelage ------------------------------------------------------------------------------------------

    def new_code(self) -> str:
        now = self._clock()
        self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
        code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
        self._codes[code] = now + PAIRING_TTL
        return code

    def pair(self, code: str, name: str) -> tuple[Device, str]:
        """Code valide -> (appareil, jeton en clair, rendu une seule fois). PairingError sinon."""
        now = self._clock()
        if now < self._locked_until:
            raise PairingError("jumelage bloqué quelques minutes (trop de codes faux)")
        code = str(code or "").strip().upper().replace(" ", "").replace("-", "")
        expires = next((exp for c, exp in self._codes.items() if hmac.compare_digest(c, code)), None)
        if expires is None or expires <= now:
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._locked_until, self._failures = now + LOCKOUT, 0
            raise PairingError("code de jumelage invalide ou expiré")
        del self._codes[code]
        self._failures = 0
        token = secrets.token_urlsafe(32)
        device = Device(secrets.token_hex(8), str(name or "téléphone").strip()[:40] or "téléphone", _digest(token), now)
        self._devices[device.id] = device
        self._save()
        return device, token

    # --- Authentification ----------------------------------------------------------------------------------

    def authenticate(self, device_id: str, token: str) -> Device | None:
        device = self._devices.get(str(device_id or ""))
        if device is None or not token or not hmac.compare_digest(device.token_sha256, _digest(token)):
            return None
        device.last_seen = self._clock()
        return device

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def list(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.paired_at)

    def forget(self, device_id: str) -> bool:
        removed = self._devices.pop(device_id, None) is not None
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in self._devices.values()], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path)
