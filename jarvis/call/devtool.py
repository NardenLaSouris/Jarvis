"""Essai d'un appel de bout en bout sans le Core en service : ce script joue le rôle du Core (même gestionnaire, même
lien avec la passerelle, même bascule audio), avec un micro de maison silencieux et sans conversation réelle.

    python -m jarvis.call.devtool "Ceci est un appel d'essai." [--talk 15]

Il demande l'appel à la passerelle (qui doit tourner : python -m jarvis.call.gateway), affiche chaque état, et une
fois l'appel décroché « parle » ``--talk`` secondes (silence envoyé au téléphone), puis raccroche comme ORION à la
fin d'une conversation, sauf si le téléphone raccroche avant.
"""

from __future__ import annotations

import argparse
import logging
import time

import numpy as np


class SilentHome:
    """Micro de la maison simulé : silence au rythme réel."""

    sample_rate, frame_samples = 16000, 1280

    def read(self):
        time.sleep(self.frame_samples / self.sample_rate)
        return np.zeros(self.frame_samples, np.int16)

    def flush(self):
        pass


class Nowhere:
    def play(self, audio, rate):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.call.devtool")
    parser.add_argument("reason", nargs="?", default="Ceci est un appel d'essai.")
    parser.add_argument("--talk", type=float, default=15.0, help="secondes de conversation simulée")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s : %(message)s")
    from jarvis.audio.network import NetworkSink, NetworkSource
    from jarvis.audio.switch import SwitchableSink, SwitchableSource
    from jarvis.call import CallManager, CallRefused, CallSettings
    from jarvis.call.link import GatewayLink
    from jarvis.config import load_config, secret
    from jarvis.factory import ENV_FILE

    cfg = load_config(args.config)
    c = cfg.call
    token = secret("CALL_GATEWAY_TOKEN", ENV_FILE)
    if not (token and c.device):
        print("Il faut CALL_GATEWAY_TOKEN dans .env et [call] device (identifiant de l'appareil jumelé).")
        return 2
    source, sink = SwitchableSource(SilentHome()), SwitchableSink(Nowhere())
    link = GatewayLink(c.gateway, token, c.device)
    manager = CallManager(
        CallSettings(device=c.device, ring_timeout=c.ring_timeout, connect_timeout=c.connect_timeout),
        source, sink,
        lambda call: (NetworkSource(c.gateway, token, 16000, 1280), NetworkSink(c.gateway, token)),
        notifier=link, on_event=lambda kind, data: print(f"→ {kind} : {data['state']}"
                                                          f"{' (' + data['end_reason'] + ')' if data['end_reason'] else ''}"))
    link.manager = manager
    link.start()
    manager.start()
    try:
        call = manager.request(args.reason, "admin")
    except CallRefused as exc:
        print(f"Appel refusé par le Core : {exc}")
        return 1
    while not call.final:
        if manager.take_connected() is not None:
            print(f"Décroché : conversation simulée pendant {args.talk:.0f} s (silence envoyé au téléphone)")
            until = time.monotonic() + args.talk
            while manager.active() and time.monotonic() < until:
                sink.play(np.zeros(1600, np.int16), 16000)
                source.read()
            manager.finish()
        time.sleep(0.2)
    manager.finish()
    manager.stop()
    print(f"Fin : {call.state} ({call.end_reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
