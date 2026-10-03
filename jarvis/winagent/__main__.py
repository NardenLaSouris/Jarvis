"""Lance l'agent Windows : python -m jarvis.winagent [--config windows_agent.toml] [--env .env]"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from jarvis.winagent.config import load_agent_config
from jarvis.winagent.server import AgentServer

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.winagent", description="Agent Windows de JARVIS")
    parser.add_argument("--config", default=str(ROOT / "windows_agent.toml"), help="fichier de réglages")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="fichier contenant JARVIS_AGENT_TOKEN")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    try:
        config = load_agent_config(args.config, args.env)
    except (OSError, ValueError) as exc:
        print(f"Configuration de l'agent invalide : {exc}", file=sys.stderr)
        return 2
    server = AgentServer(config)
    try:
        server.start()
    except OSError as exc:
        print(f"Impossible d'écouter sur {config.host}:{config.port} : {exc}", file=sys.stderr)
        return 1
    host, port = server.address
    logging.info("Agent JARVIS Windows à l'écoute sur %s:%s (IP autorisées : %s)", host, port,
                 ", ".join(sorted(config.allowed_ips)))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Arrêt de l'agent")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
