"""Outils audio : régler le volume du système, couper et remettre le son.

Windows : API Core Audio (IAudioEndpointVolume du périphérique de sortie par défaut) appelée
directement par ctypes, sans dépendance. Linux : pactl (PulseAudio / PipeWire).
Chaque réglage est relu après coup : le résultat rapporté est l'état réel du système.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import uuid

from jarvis.tools.base import EXECUTION_FAILED, Param, Risk, Tool, ToolError

CLSID_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_ENDPOINT_VOLUME = "5CDF2C82-841E-4546-9722-0CF74078229A"
CLSCTX_ALL = 0x17
RENDER, CONSOLE = 0, 0
RPC_E_CHANGED_MODE = -2147417850


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    """Méthode n° ``index`` de la table virtuelle d'un objet COM."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])
    return lambda *args: function(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


class _Endpoint:
    """IAudioEndpointVolume du périphérique de sortie par défaut, le temps d'un bloc ``with``."""

    def __enter__(self):
        ole32 = ctypes.oledll.ole32
        self._uninitialize = True
        try:
            ole32.CoInitializeEx(None, 0)
        except OSError as exc:
            if exc.winerror != RPC_E_CHANGED_MODE:
                raise
            self._uninitialize = False
        self._objects: list[ctypes.c_void_p] = []
        enumerator, device, endpoint = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_DEVICE_ENUMERATOR)), None, CLSCTX_ALL,
                                   ctypes.byref(_guid(IID_DEVICE_ENUMERATOR)), ctypes.byref(enumerator))
            self._objects.append(enumerator)
            _method(enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                RENDER, CONSOLE, ctypes.byref(device))
            self._objects.append(device)
            _method(device, 3, ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid(IID_ENDPOINT_VOLUME)), CLSCTX_ALL, None, ctypes.byref(endpoint))
            self._objects.append(endpoint)
        except OSError:
            self.__exit__(None, None, None)
            raise
        return endpoint

    def __exit__(self, *exc):
        for obj in reversed(self._objects):
            _release(obj)
        if self._uninitialize:
            ctypes.oledll.ole32.CoUninitialize()
        return False


class VolumeControl:
    """Volume principal du système (0 à 100) et coupure du son."""

    def get(self) -> int:
        if sys.platform == "win32":
            with _Endpoint() as endpoint:
                level = ctypes.c_float()
                _method(endpoint, 9, ctypes.POINTER(ctypes.c_float))(ctypes.byref(level))
                return round(level.value * 100)
        out = self._pactl("get-sink-volume", "@DEFAULT_SINK@")
        match = re.search(r"(\d+)%", out)
        if not match:
            raise OSError("volume illisible")
        return int(match.group(1))

    def set(self, percent: int) -> None:
        if sys.platform == "win32":
            with _Endpoint() as endpoint:
                _method(endpoint, 7, ctypes.c_float, ctypes.c_void_p)(percent / 100.0, None)
            return
        self._pactl("set-sink-volume", "@DEFAULT_SINK@", f"{int(percent)}%")

    def muted(self) -> bool:
        if sys.platform == "win32":
            with _Endpoint() as endpoint:
                state = ctypes.c_int()
                _method(endpoint, 15, ctypes.POINTER(ctypes.c_int))(ctypes.byref(state))
                return bool(state.value)
        return "yes" in self._pactl("get-sink-mute", "@DEFAULT_SINK@").lower()

    def set_mute(self, mute: bool) -> None:
        if sys.platform == "win32":
            with _Endpoint() as endpoint:
                _method(endpoint, 14, ctypes.c_int, ctypes.c_void_p)(int(mute), None)
            return
        self._pactl("set-sink-mute", "@DEFAULT_SINK@", "1" if mute else "0")

    @staticmethod
    def _pactl(*args: str) -> str:
        done = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10, shell=False)
        if done.returncode != 0:
            raise OSError(done.stderr.strip() or "pactl a échoué")
        return done.stdout


def _failed(action: str) -> ToolError:
    return ToolError(EXECUTION_FAILED, f"Je n'ai pas pu {action}.")


def set_volume_tool(control: VolumeControl) -> Tool:
    def run(volume: int) -> dict:
        try:
            control.set(volume)
            actual, muted = control.get(), control.muted()
        except OSError as exc:
            raise _failed("régler le volume") from exc
        if abs(actual - volume) > 1:
            raise ToolError(EXECUTION_FAILED, f"Le volume est resté à {actual} %.")
        return {"volume": actual, "muted": muted}

    return Tool("set_volume", "Règle le volume du système sur un niveau précis, de 0 à 100 %.",
                {"volume": Param(int, "niveau de 0 à 100", minimum=0, maximum=100)},
                {"volume": "niveau réel après réglage", "muted": "son coupé ou non"}, Risk.SAFE, run,
                say=lambda r: f"Le volume est à {r['volume']} %." + (" Le son reste coupé." if r["muted"] else ""))


def mute_tool(control: VolumeControl, mute: bool) -> Tool:
    def run() -> dict:
        try:
            control.set_mute(mute)
            muted, volume = control.muted(), control.get()
        except OSError as exc:
            raise _failed("couper le son" if mute else "remettre le son") from exc
        if muted != mute:
            raise ToolError(EXECUTION_FAILED, "Le son n'a pas changé d'état.")
        return {"muted": muted, "volume": volume}

    if mute:
        return Tool("mute_volume", "Coupe le son du système.", {}, {"muted": "true", "volume": "niveau"},
                    Risk.SAFE, run, say=lambda r: "Le son est coupé.")
    return Tool("unmute_volume", "Remet le son du système (annule la coupure).", {},
                {"muted": "false", "volume": "niveau"}, Risk.SAFE, run,
                say=lambda r: f"Le son est revenu, à {r['volume']} %.")
