"""Détection de la machine sur laquelle tourne JARVIS."""

from __future__ import annotations

import os
import platform


def machine_summary() -> str:
    from jarvis.stt.faster_whisper import detect_device

    gpu = "GPU NVIDIA disponible" if detect_device()[0] == "cuda" else "pas de GPU NVIDIA (tout sur CPU)"
    cpu = platform.processor() or platform.machine()
    return f"{platform.system()} {platform.release()}, {cpu}, {os.cpu_count()} cœurs logiques, {gpu}"
