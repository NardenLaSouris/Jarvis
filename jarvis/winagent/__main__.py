"""Lance l'agent Windows : python -m jarvis.winagent [--config windows_agent.toml] [--env .env]"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from jarvis.tools import ToolRegistry, builtin_tools
from jarvis.tools.builtin import PC_TOOLS
from jarvis.winagent.config import load_agent_config
from jarvis.winagent.server import AgentServer

ROOT = Path(__file__).resolve().parents[2]


def pc_actions(settings: dict) -> ToolRegistry:
    """Outils qui agissent sur ce PC (applications, pages Web, volume, verrouillage, description), réglables
    comme dans config.toml ([tools.<outil>] enabled, [tools.applications]). La confirmation est demandée par le
    Core avant tout appel."""
    registry = ToolRegistry()
    for tool in builtin_tools(settings):
        if tool.name in PC_TOOLS:
            registry.register(tool)
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis.winagent", description="Agent Windows d'ORION")
    parser.add_argument("--config", default=str(ROOT / "windows_agent.toml"), help="fichier de réglages")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="fichier contenant JARVIS_AGENT_TOKEN")
    parser.add_argument("--log-file", help="journal dans ce fichier plutôt qu'à l'écran (lancement sans fenêtre)")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    args = parser.parse_args(argv)
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, filename=args.log_file,
                        encoding="utf-8", format="%(asctime)s %(levelname)s %(name)s : %(message)s")

    def fail(message: str, code: int) -> int:
        if args.log_file:
            logging.error(message)
        else:
            print(message, file=sys.stderr)
        return code

    try:
        config = load_agent_config(args.config, args.env)
    except (OSError, ValueError) as exc:
        return fail(f"Configuration de l'agent invalide : {exc}", 2)
    server = AgentServer(config, pc_actions(config.tools))
    try:
        server.start()
    except OSError as exc:
        return fail(f"Impossible d'écouter sur {config.host}:{config.port} : {exc}", 1)
    host, port = server.address
    logging.info("Agent ORION Windows à l'écoute sur %s:%s (IP autorisées : %s ; actions : %s)", host, port,
                 ", ".join(sorted(config.allowed_ips)), ", ".join(t.name for t in server.actions.list()) or "aucune")
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
