"""Alimentation des PC : arrêt (sur le PC, par son agent) et allumage (réveil réseau envoyé par le Core).

- ``shutdown_pc`` : arrêt de Windows avec un délai (SHUTDOWN_DELAY s, annulable sur le PC par ``shutdown /a``),
  commande fixe sans shell ; confirmation toujours demandée par le Core.
- ``wake_pc`` : paquet de réveil (Wake-on-LAN) vers l'adresse matérielle de l'appareil ([tools.devices.<pc>] mac),
  sur le réseau local seulement. Le PC doit avoir le réveil réseau activé (BIOS et carte réseau).
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
from typing import Callable

from jarvis.tools.base import EXECUTION_FAILED, Param, Risk, Tool, ToolError

SHUTDOWN_DELAY = 60  # s : le temps d'enregistrer son travail, ou d'annuler sur le PC
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
WOL_PORT = 9


def shutdown_windows() -> bool:
    """Arrêt programmé de ce PC : commande fixe, sans shell. True si Windows l'a accepté."""
    if sys.platform != "win32":
        return False
    command = ["shutdown", "/s", "/t", str(SHUTDOWN_DELAY), "/c", "Arrêt demandé par ORION."]
    try:
        return subprocess.run(command, capture_output=True, timeout=10, shell=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def shutdown_tool(shutdown: Callable[[], bool] = shutdown_windows) -> Tool:
    def run() -> dict:
        if not shutdown():
            raise ToolError(EXECUTION_FAILED, "Je n'ai pas pu éteindre l'ordinateur.")
        return {"shutdown_in_seconds": SHUTDOWN_DELAY}

    return Tool("shutdown_pc", "Éteint l'ordinateur (arrêt de Windows dans une minute).", {},
                {"shutdown_in_seconds": "délai avant l'arrêt (s)"}, Risk.CONFIRMATION_REQUIRED, run,
                question=lambda p: "Voulez-vous que j'éteigne l'ordinateur ?",
                say=lambda r: "L'ordinateur s'éteint dans une minute.")


def magic_packet(mac: str) -> bytes:
    if not MAC.match(mac):
        raise ValueError(f"adresse matérielle invalide : {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet(mac), (broadcast, WOL_PORT))


def wake_tool(devices, send: Callable[[str], None] = send_magic_packet) -> Tool:
    """Allume l'appareil visé (déduit des mots de la demande, sinon l'appareil par défaut)."""

    def target(key: str):
        device = devices[key]
        if not device.mac:
            raise ToolError("device_forbidden", f"Je ne sais pas allumer {device.name}.")
        return key

    def run(device: str | None = None) -> dict:
        chosen = devices[device or devices.default.key]
        try:
            send(chosen.mac)
        except OSError as exc:
            raise ToolError(EXECUTION_FAILED, f"Je n'ai pas pu réveiller {chosen.name}.") from exc
        return {"device": chosen.name}

    device = Param(str, "appareil visé", required=False, max_length=32, choices=devices.keys(), check=target,
                   hidden=True, resolve=lambda text: (devices.find(text) or devices.default).key)
    return Tool("wake_pc", "Allume l'ordinateur à distance (réveil réseau).", {"device": device},
                {"device": "appareil réveillé"}, Risk.SAFE, run,
                say=lambda r: f"J'allume {r['device']} ; il sera prêt dans une minute environ.")
