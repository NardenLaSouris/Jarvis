"""Garde-fous communs : les tests sont silencieux et sans effet physique.

Tout appel réel au son (lecture, micro), au navigateur ou à une ampoule Tuya pendant un test échoue aussitôt :
les tests passent par des faux (RecordingSink, FakeDriver, opener...). Le test de bout en bout, qui fait
tourner les vrais moteurs (synthèse vocale comprise, sans lecture), ne s'exécute qu'avec JARVIS_E2E=1.
"""

from __future__ import annotations

import pytest


class RealDeviceUsed(RuntimeError):
    pass


def _forbidden(what: str):
    def refuse(*args, **kwargs):
        raise RealDeviceUsed(f"{what} réel interdit pendant les tests")
    return refuse


@pytest.fixture(autouse=True)
def _silent(monkeypatch):
    try:
        import sounddevice as sd
    except Exception:  # pragma: no cover - sounddevice absent ou sans PortAudio
        sd = None
    if sd is not None:
        for name in ("play", "rec", "playrec", "OutputStream", "InputStream", "Stream", "RawOutputStream",
                     "RawInputStream", "RawStream"):
            if hasattr(sd, name):
                monkeypatch.setattr(sd, name, _forbidden(f"audio ({name})"))
    import webbrowser

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, _forbidden("navigateur"))
    try:
        import tinytuya
    except Exception:  # pragma: no cover
        tinytuya = None
    if tinytuya is not None:
        for name in ("BulbDevice", "Device", "OutletDevice"):
            if hasattr(tinytuya, name):
                monkeypatch.setattr(tinytuya, name, _forbidden(f"appareil Tuya ({name})"))
    yield
