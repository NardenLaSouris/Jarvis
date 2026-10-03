"""JARVIS Control : fenêtre (WebView2), icône dans la zone de notification, raccourci Ctrl+Shift+J.

  pythonw -m jarvis.control            # ouvre l'interface
  pythonw -m jarvis.control --hidden   # démarre dans la zone de notification (démarrage automatique)
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from jarvis.control.bridge import Bridge
from jarvis.control.settings import SettingsStore, app_dir

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TITLE = "JARVIS Control"
STARTUP_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
    "JARVIS Control.lnk"
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT, WM_HOTKEY, WM_QUIT = 0x2, 0x4, 0x4000, 0x312, 0x12
SHORTCUT = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JC_LINK); $s.TargetPath = $env:JC_TARGET; "
            "$s.Arguments = '-m jarvis.control --hidden'; $s.WorkingDirectory = $env:JC_DIR; "
            "$s.Description = 'JARVIS Control'; $s.Save()")


def set_autostart(enabled: bool) -> None:
    """Raccourci dans le dossier Démarrage de l'utilisateur (aucun droit administrateur)."""
    if not enabled:
        STARTUP_LINK.unlink(missing_ok=True)
        return
    env = {**os.environ, "JC_LINK": str(STARTUP_LINK), "JC_TARGET": str(Path(sys.executable).with_name("pythonw.exe")),
           "JC_DIR": str(ROOT)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT], env=env, check=True,
                   capture_output=True, timeout=30)


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JARVISControl")
    return ctypes.windll.kernel32.GetLastError() != 183


class Hotkey(threading.Thread):
    """Ctrl+Shift+J, global : appelle ``action`` depuis son propre fil (boucle de messages Windows)."""

    def __init__(self, action):
        super().__init__(name="raccourci", daemon=True)
        self._action = action
        self._thread_id = 0

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, ord("J")):
            log.warning("Ctrl+Shift+J déjà utilisé par un autre programme")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_HOTKEY:
                self._action()
        user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def tray_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(8, 14, 26, 255), outline=(70, 214, 255, 255), width=5)
    draw.ellipse((22, 22, 42, 42), fill=(70, 214, 255, 255))
    return image


class ControlApp:
    def __init__(self, bridge: Bridge, hidden: bool):
        import webview

        self._webview = webview
        self.bridge = bridge
        self.visible = not hidden
        self.window = webview.create_window(TITLE, bridge.url, width=1240, height=820, min_size=(960, 620),
                                            background_color="#0b0f17", hidden=hidden)
        self.window.events.closing += self._on_closing
        self.hotkey = Hotkey(self.toggle)
        self.tray = None
        self._quitting = False

    def _on_closing(self):
        if self._quitting:
            return True
        self.hide()
        return False

    def show(self) -> None:
        self.window.show()
        self.window.restore()
        self.visible = True

    def hide(self) -> None:
        self.window.hide()
        self.visible = False

    def toggle(self, *_args) -> None:
        self.hide() if self.visible else self.show()

    def quit(self, *_args) -> None:
        self._quitting = True
        self.hotkey.stop()
        if self.tray is not None:
            self.tray.stop()
        self.bridge.stop()
        self.window.destroy()

    def run(self) -> None:
        import pystray

        menu = pystray.Menu(pystray.MenuItem("Ouvrir / masquer (Ctrl+Shift+J)", self.toggle, default=True),
                            pystray.MenuItem("Quitter JARVIS Control", self.quit))
        self.tray = pystray.Icon("jarvis-control", tray_image(), TITLE, menu)
        self.tray.run_detached()
        self.hotkey.start()
        self._webview.start(gui="edgechromium", private_mode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.control", description=TITLE)
    parser.add_argument("--hidden", action="store_true", help="démarrer dans la zone de notification")
    args = parser.parse_args(argv)
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=folder / "control.log", encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    if not single_instance():
        ctypes.windll.user32.MessageBoxW(None, "JARVIS Control est déjà ouvert : Ctrl+Shift+J ou l'icône près de "
                                               "l'horloge.", TITLE, 0x40)
        return 0
    bridge = Bridge(SettingsStore(folder), set_autostart)
    bridge.start()
    log.info("JARVIS Control démarré (Core : %s)", bridge.settings.core_url)
    ControlApp(bridge, args.hidden).run()
    return 0
