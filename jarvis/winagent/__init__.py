"""Agent Windows : petit serveur HTTP du réseau local par lequel le Core ORION (mini-PC) demande des
actions explicitement enregistrées sur le PC Windows. Aucune commande libre, aucun shell."""

from jarvis.winagent.config import AgentConfig, load_agent_config
from jarvis.winagent.server import AGENT_NAME, AgentServer

__all__ = ["AGENT_NAME", "AgentConfig", "AgentServer", "load_agent_config"]
