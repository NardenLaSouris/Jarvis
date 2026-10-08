"""Registre central des outils : seule source de vérité sur ce qu'ORION sait faire."""

from __future__ import annotations

import re

from jarvis.tools.base import TOOL_NOT_FOUND, Risk, Tool, ToolError

NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not NAME.match(tool.name):
            raise ValueError(f"Nom d'outil invalide : {tool.name!r}")
        if not isinstance(tool.risk, Risk):
            raise ValueError(f"Niveau de risque invalide pour {tool.name}")
        if tool.name in self._tools:
            raise ValueError(f"Outil déjà enregistré : {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolError(TOOL_NOT_FOUND, "Cet outil n'existe pas.")
        return tool

    def exists(self, name: str) -> bool:
        return isinstance(name, str) and name in self._tools

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
