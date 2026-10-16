"""Essai d'un appel de bout en bout sans le Core en service : ce script joue le rôle du Core (même gestionnaire, même
lien avec la passerelle, même bascule audio), avec un micro de maison silencieux et sans conversation réelle.

    python -m orion.call.devtool "Ceci est un appel d'essai." [--talk 15] [--no-voice]

Il demande l'appel à la passerelle (qui doit tourner : python -m orion.call.gateway), affiche chaque état ; une fois
l'appel décroché, il dit la raison avec la voix d'ORION, puis renvoie votre voix en écho pendant ``--talk`` secondes
(les deux sens de l'audio), et raccroche comme ORION à la fin d'une conversation, sauf si le téléphone raccroche avant.
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


def echo(manager, source, sink, seconds: float, delay: float = 0.4) -> None:
    """Renvoie au téléphone ce que son micro capte, ``delay`` secondes plus tard : les deux sens de l'audio."""
    frames: list[np.ndarray] = []
    lag = max(1, round(delay * source.sample_rate / source.frame_samples))
    until = time.monotonic() + seconds
    while manager.active() and time.monotonic() < until:
        frames.append(source.read())
        if len(frames) > lag:
            sink.play(frames.pop(0), source.sample_rate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orion.call.devtool")
    parser.add_argument("reason", nargs="?", default="Ceci est un appel d'essai.")
    parser.add_argument("--talk", type=float, default=15.0, help="secondes d'écho de votre voix")
    parser.add_argument("--no-voice", action="store_true", help="sans la voix d'ORION (pas de synthèse)")
    parser.add_argument("--answer", action="store_true", help="attend les appels du téléphone au lieu d'appeler")
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s : %(message)s")
    from orion.audio.network import NetworkSink, NetworkSource
    from orion.audio.switch import SwitchableSink, SwitchableSource
    from orion.call import CallManager, CallRefused, CallSettings
    from orion.call.link import GatewayLink
    from orion.config import load_config, secret
    from orion.factory import ENV_FILE

    cfg = load_config(args.config)
    c = cfg.call
    token = secret("CALL_GATEWAY_TOKEN", ENV_FILE)
    if not (token and c.device):
        print("Il faut CALL_GATEWAY_TOKEN dans .env et [call] device (identifiant de l'appareil jumelé).")
        return 2
    tts = None
    if not args.no_voice:
        from orion.factory import build_tts

        tts = build_tts(cfg)  # voix d'ORION et son effet, chargée avant l'appel (aucune attente au décroché)
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

    def converse(text: str) -> None:
        if tts is not None:
            audio, rate = tts.synthesize(f"{text} Je vous renvoie maintenant votre voix, en écho.")
            sink.play(np.asarray(audio, dtype=np.int16), rate)
            sink.drain()
            source.flush()  # comme ORION après avoir parlé : rien de ce qui a été capté pendant ce temps
        print(f"En ligne : écho de votre voix pendant {args.talk:.0f} s")
        echo(manager, source, sink, args.talk)
        manager.finish()

    if args.answer:
        print("En attente des appels du téléphone (Ctrl+C pour arrêter)")
        try:
            while True:
                if manager.take_connected() is not None:
                    converse("Oui, monsieur ? C'est ORION.")
                time.sleep(0.2)
        except KeyboardInterrupt:
            manager.stop()
            return 0
    try:
        call = manager.request(args.reason, "admin")
    except CallRefused as exc:
        print(f"Appel refusé par le Core : {exc}")
        return 1
    while not call.final:
        if manager.take_connected() is not None:
            converse(f"Bonjour, c'est ORION. {args.reason}")
        time.sleep(0.2)
    manager.finish()
    manager.stop()
    print(f"Fin : {call.state} ({call.end_reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
