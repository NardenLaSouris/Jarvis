"""Tests du STT : repli CPU explicite quand CUDA est indisponible.

Lancement : python tests/test_stt.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.stt.faster_whisper as fw  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.factory import build_stt  # noqa: E402

CFG = load_config(ROOT / "config.toml", local=False)


def test_falls_back_to_cpu_when_no_cuda_device():
    original = fw.cuda_device_count
    fw.cuda_device_count = lambda: 0
    try:
        stt = build_stt(replace(CFG, stt=replace(CFG.stt, device="cuda", compute_type="float16")))
    finally:
        fw.cuda_device_count = original
    assert (stt.device, stt.compute_type) == ("cpu", "int8")
    assert stt.transcribe(np.zeros(16000, np.int16), 16000) == ""


def test_cpu_configuration_is_used_as_is():
    stt = build_stt(replace(CFG, stt=replace(CFG.stt, device="cpu", compute_type="int8")))
    assert (stt.device, stt.compute_type, stt.model_name) == ("cpu", "int8", CFG.stt.model)
    assert stt.load_time > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
