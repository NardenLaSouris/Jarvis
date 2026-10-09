"""Moteur de politique Cedar pour les outils d'ORION (prototype isolé, non branché sur le service).

Chaîne visée : le modèle PROPOSE un appel d'outil -> le Core construit un contexte DE CONFIANCE (profil de
l'utilisateur, outil et risque tirés du registre, confirmation recueillie par le Core, origine de la demande,
appareil) -> le moteur DÉCIDE (Cedar) -> le Core APPLIQUE (exécute, demande confirmation ou refuse).

Rien de ce que renvoie le modèle n'entre dans le contexte : un champ « confirmed » ou « role » ajouté par le modèle
est ignoré. Toute anomalie (politique illisible, erreur d'évaluation, absence de décision, délai dépassé, exception)
donne un refus. Chaque décision est journalisée sans aucune valeur de paramètre.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXECUTABLE = re.compile(r"\.(exe|msi|bat|cmd|ps1|scr|vbs|jar|apk|dmg|sh)$", re.IGNORECASE)


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_CONFIRMATION = "requires_confirmation"


@dataclass(frozen=True)
class ToolInfo:
    name: str
    risk: str  # safe | confirm | restricted (registre d'ORION)
    category: str | None


@dataclass(frozen=True)
class TrustedContext:
    """Construit par le Core uniquement."""

    user: str
    role: str
    tool: ToolInfo
    parameters: dict
    confirmed: bool = False
    origin: str = "user"  # "user" : demande de l'utilisateur ; "content" : déduite d'un mail, d'une page, d'un fichier
    device_forbidden: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    policies: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default=(), repr=False)


def build_context(proposal: dict, user: str, role: str, tools: dict[str, ToolInfo], confirmed: bool,
                  origin: str, forbidden_devices: frozenset[str] = frozenset({"mini"})) -> TrustedContext | None:
    """Contexte de confiance pour une proposition du modèle ; None si l'outil n'existe pas dans le registre.
    Seuls le nom de l'outil et ses paramètres sont lus dans la proposition."""
    tool = tools.get(str(proposal.get("tool", "")))
    if tool is None:
        return None
    parameters = proposal.get("parameters") if isinstance(proposal.get("parameters"), dict) else {}
    device = str(parameters.get("device", "")).strip().lower()
    return TrustedContext(user, role, tool, dict(parameters), confirmed=confirmed, origin=origin,
                          device_forbidden=device in forbidden_devices)


class PolicyEngine:
    def __init__(self, tools: dict[str, ToolInfo], users: dict[str, str], policies: Path = HERE / "policies.cedar",
                 schema: Path = HERE / "orion.cedarschema.json", audit: Path | None = None, timeout: float = 0.5):
        import cedarpy

        self._cedar = cedarpy
        text = policies.read_text(encoding="utf-8")
        self._ids = re.findall(r'@id\("([^"]+)"\)', text)
        schema_text = schema.read_text(encoding="utf-8")
        validation = cedarpy.validate_policies(text, schema_text)
        if not validation.validation_passed:
            raise ValueError(f"politiques non conformes au schéma : {validation.errors}")
        self._policies = cedarpy.PolicySet.from_str(text)
        self._schema = cedarpy.Schema.from_json_str(schema_text)
        self._entities = self._build_entities(tools, users)
        self._audit = audit
        self._timeout = timeout
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    @staticmethod
    def _build_entities(tools: dict[str, ToolInfo], users: dict[str, str]) -> list[dict]:
        roles = sorted(set(users.values()) | {"owner", "adult", "child", "guest"})
        categories = sorted({t.category or "none" for t in tools.values()})
        entities = [{"uid": {"type": "Role", "id": r}, "attrs": {}, "parents": []} for r in roles]
        entities += [{"uid": {"type": "Category", "id": c}, "attrs": {}, "parents": []} for c in categories]
        entities += [{"uid": {"type": "User", "id": u}, "attrs": {}, "parents": [{"type": "Role", "id": r}]}
                     for u, r in users.items()]
        entities += [{"uid": {"type": "Tool", "id": t.name}, "attrs": {"risk": t.risk},
                      "parents": [{"type": "Category", "id": t.category or "none"}]} for t in tools.values()]
        return entities

    def _request(self, ctx: TrustedContext, confirmed: bool) -> dict:
        url = str(ctx.parameters.get("url", "")) if ctx.tool.name == "open_url" else ""
        return {"principal": f'User::"{ctx.user}"', "action": 'Action::"execute"',
                "resource": f'Tool::"{ctx.tool.name}"',
                "context": {"confirmed": bool(confirmed), "origin": ctx.origin,
                            "device_forbidden": bool(ctx.device_forbidden),
                            "url_executable": bool(EXECUTABLE.search(re.split(r"[?#]", url)[0])),
                            "forget_all": ctx.tool.name == "forget"
                            and str(ctx.parameters.get("topic", "")).strip().lower() == "tout"}}

    def _evaluate(self, request: dict):
        return self._cedar.is_authorized(request, self._policies, self._entities, schema=self._schema)

    def _decide_once(self, ctx: TrustedContext, confirmed: bool) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        future = self._pool.submit(self._evaluate, self._request(ctx, confirmed))
        result = future.result(timeout=self._timeout)
        reasons = tuple(self._name(p) for p in result.diagnostics.reasons)
        errors = tuple(str(e)[:200] for e in result.diagnostics.errors)
        return str(result.decision).rsplit(".", 1)[-1].lower(), reasons, errors

    def _name(self, policy_id: str) -> str:
        match = re.fullmatch(r"policy(\d+)", policy_id)
        if match and int(match.group(1)) < len(self._ids):
            return self._ids[int(match.group(1))]
        return policy_id

    def decide(self, ctx: TrustedContext | None) -> PolicyDecision:
        started = time.perf_counter()
        if ctx is None:
            decision = PolicyDecision(Decision.DENY, "outil inconnu du registre")
        else:
            try:
                verdict, reasons, errors = self._decide_once(ctx, ctx.confirmed)
                if errors or verdict not in ("allow", "deny"):
                    decision = PolicyDecision(Decision.DENY, "moteur de politique en erreur", reasons, errors)
                elif verdict == "allow":
                    decision = PolicyDecision(Decision.ALLOW, "permis", reasons)
                elif not ctx.confirmed and reasons == ("confirmation_required",):
                    again, _, errors2 = self._decide_once(ctx, True)
                    decision = (PolicyDecision(Decision.REQUIRES_CONFIRMATION, "action à confirmer", reasons)
                                if again == "allow" and not errors2 else
                                PolicyDecision(Decision.DENY, "refusé", reasons, errors2))
                else:
                    decision = PolicyDecision(Decision.DENY, "refusé" if reasons else "aucune règle ne le permet",
                                              reasons)
            except concurrent.futures.TimeoutError:
                decision = PolicyDecision(Decision.DENY, "moteur de politique trop lent")
            except Exception as exc:  # noqa: BLE001 - toute panne du moteur vaut refus
                decision = PolicyDecision(Decision.DENY, "moteur de politique en erreur", (), (type(exc).__name__,))
        self._log(ctx, decision, time.perf_counter() - started)
        return decision

    def _log(self, ctx: TrustedContext | None, decision: PolicyDecision, seconds: float) -> None:
        if self._audit is None:
            return
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "decision": decision.decision.value,
                 "reason": decision.reason, "policies": list(decision.policies), "ms": round(seconds * 1000, 2)}
        if decision.errors:
            entry["errors"] = [re.sub(r"`[^`]*`", "`…`", e) for e in decision.errors]
        if ctx is not None:
            entry.update(user=ctx.user, role=ctx.role, tool=ctx.tool.name, origin=ctx.origin, confirmed=ctx.confirmed,
                         parameter_names=sorted(map(str, ctx.parameters)))
        with self._audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
