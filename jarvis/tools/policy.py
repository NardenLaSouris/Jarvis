"""Autorisation des outils par politiques Cedar, à côté du PermissionManager ([tools.policy] engine).

Chaîne : le LLM PROPOSE un appel d'outil (donnée non fiable) -> le Core valide la demande (registre, paramètres) et
construit un contexte DE CONFIANCE (utilisateur et rôle venus du Core, risque et catégorie venus du registre,
confirmation recueillie par le Core, origine de la demande, modèle de confiance ou non) -> la porte de décision
(PolicyGate) interroge le PermissionManager et, selon le mode, Cedar -> le Core APPLIQUE (exécute, demande
confirmation ou refuse).

Modes :
  builtin : PermissionManager seul (comportement historique, défaut).
  shadow  : PermissionManager seul décide ; Cedar est évalué et journalisé, écarts compris, sans aucun effet.
  both    : double verrou. Permis seulement si les deux permettent ; confirmation si l'un des deux l'exige ;
            refus si l'un refuse, si Cedar est en erreur, trop lent ou indisponible.

Cedar ne protège pas le système : il décide, le Core applique. Toute anomalie de Cedar vaut refus (une règle forbid
en erreur est ignorée par Cedar, qui peut alors répondre Allow : toute erreur d'évaluation est donc un refus).
Le journal de décisions ne contient jamais la valeur d'un paramètre.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.tools.base import Risk, Tool
from jarvis.tools.permissions import Decision, PermissionDecision

log = logging.getLogger(__name__)

MODES = ("builtin", "shadow", "both")
RISK = {Risk.SAFE: "safe", Risk.CONFIRMATION_REQUIRED: "confirm", Risk.RESTRICTED: "restricted"}
EXECUTABLE = re.compile(r"\.(exe|msi|msix|bat|cmd|com|ps1|scr|vbs|jse?|jar|apk|dmg|sh)$", re.IGNORECASE)
AUDIT_MAX_BYTES = 5_000_000
DENIED = "Je n'ai pas l'autorisation de faire cela."


@dataclass(frozen=True)
class CedarVerdict:
    decision: Decision
    reason: str
    policies: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class CedarPolicy:
    """Politiques Cedar chargées et validées contre leur schéma ; entités tirées du registre et des profils."""

    def __init__(self, policies: Path, schema: Path, registry, users: dict[str, str],
                 category, timeout: float = 0.5, llm_trusted: bool = True):
        import cedarpy

        self._cedar = cedarpy
        text = policies.read_text(encoding="utf-8")
        schema_text = schema.read_text(encoding="utf-8")
        validation = cedarpy.validate_policies(text, schema_text)
        if not validation.validation_passed:
            raise ValueError(f"politiques non conformes au schéma : {validation.errors}")
        self._ids = re.findall(r'@id\("([^"]+)"\)', text)
        self._policies = cedarpy.PolicySet.from_str(text)
        self._schema = cedarpy.Schema.from_json_str(schema_text)
        self._registry, self._users, self._category = registry, users, category
        self._entities: list[dict] = []
        self._known: tuple[str, ...] = ()
        self._timeout = timeout
        self._llm_trusted = llm_trusted
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="cedar")

    @staticmethod
    def _build_entities(tools: list[Tool], users: dict[str, str], category) -> list[dict]:
        roles = sorted(set(users.values()) | {"owner", "adult", "child", "guest"})
        groups = sorted({category(t.name) or "none" for t in tools})
        entities = [{"uid": {"type": "Role", "id": r}, "attrs": {}, "parents": []} for r in roles]
        entities += [{"uid": {"type": "Category", "id": c}, "attrs": {}, "parents": []} for c in groups]
        entities += [{"uid": {"type": "User", "id": u}, "attrs": {}, "parents": [{"type": "Role", "id": r}]}
                     for u, r in users.items()]
        entities += [{"uid": {"type": "Tool", "id": t.name}, "attrs": {"risk": RISK.get(t.risk, "unknown")},
                      "parents": [{"type": "Category", "id": category(t.name) or "none"}]} for t in tools]
        return entities

    def _request(self, user: str, tool: Tool, parameters: dict, confirmed: bool, origin: str) -> dict:
        url = str(parameters.get("url", "")) if tool.name == "open_url" else ""
        device = str(parameters.get("device", "")).strip().lower()
        return {"principal": f'User::"{user}"', "action": 'Action::"execute"', "resource": f'Tool::"{tool.name}"',
                "context": {"confirmed": bool(confirmed), "origin": str(origin),
                            "device_forbidden": device == "mini",
                            "url_executable": bool(EXECUTABLE.search(re.split(r"[?#]", url)[0])),
                            "forget_all": tool.name == "forget"
                            and str(parameters.get("topic", "")).strip().lower() == "tout",
                            "llm_trusted": bool(self._llm_trusted)}}

    def _current_entities(self) -> list[dict]:
        """Entités du registre tel qu'il est maintenant (des outils s'ajoutent après la création du Core)."""
        tools = list(self._registry.list())
        names = tuple(t.name for t in tools)
        if names != self._known:
            self._entities, self._known = self._build_entities(tools, self._users, self._category), names
        return self._entities

    def _evaluate(self, request: dict):
        return self._cedar.is_authorized(request, self._policies, self._current_entities(), schema=self._schema)

    def _once(self, request: dict) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        result = self._pool.submit(self._evaluate, request).result(timeout=self._timeout)
        reasons = tuple(self._name(p) for p in result.diagnostics.reasons)
        errors = tuple(str(e)[:200] for e in result.diagnostics.errors)
        return str(result.decision).rsplit(".", 1)[-1].lower(), reasons, errors

    def _name(self, policy_id: str) -> str:
        match = re.fullmatch(r"policy(\d+)", policy_id)
        if match and int(match.group(1)) < len(self._ids):
            return self._ids[int(match.group(1))]
        return policy_id

    def decide(self, user: str, tool: Tool, parameters: dict, confirmed: bool, origin: str) -> CedarVerdict:
        try:
            verdict, reasons, errors = self._once(self._request(user, tool, parameters, confirmed, origin))
            if errors or verdict not in ("allow", "deny"):
                return CedarVerdict(Decision.DENY, "moteur de politique en erreur", reasons, errors)
            if verdict == "allow":
                return CedarVerdict(Decision.ALLOW, "permis par la politique", reasons)
            if not confirmed and reasons == ("confirmation_required",):
                again, _, errors = self._once(self._request(user, tool, parameters, True, origin))
                if again == "allow" and not errors:
                    return CedarVerdict(Decision.REQUIRES_CONFIRMATION, "action à confirmer", reasons)
            return CedarVerdict(Decision.DENY, "refusé par la politique" if reasons else "aucune règle ne le permet",
                                reasons, errors)
        except concurrent.futures.TimeoutError:
            return CedarVerdict(Decision.DENY, "moteur de politique trop lent")
        except Exception as exc:  # noqa: BLE001 - toute panne du moteur vaut refus
            return CedarVerdict(Decision.DENY, "moteur de politique en erreur", (), (type(exc).__name__,))


class PolicyGate:
    """Même interface que PermissionManager.decide ; ajoute Cedar selon le mode et journalise chaque décision."""

    def __init__(self, builtin, mode: str = "builtin", cedar: CedarPolicy | None = None,
                 audit: Path | None = None, cedar_error: str = ""):
        if mode not in MODES:
            raise ValueError(f"[tools.policy] engine : {', '.join(MODES)}")
        self.builtin = builtin
        self.mode = mode
        self.cedar = cedar
        self._audit = audit
        self._cedar_error = cedar_error
        self._lock = threading.Lock()

    def decide(self, user: str, tool: Tool, parameters: dict, confirmed: bool = False,
               origin: str = "user") -> PermissionDecision:
        started = time.perf_counter()
        builtin = self.builtin.decide(user, tool, parameters, confirmed=confirmed, origin=origin)
        if self.mode == "builtin":
            return builtin
        if self.cedar is None:
            cedar = CedarVerdict(Decision.DENY, "moteur de politique indisponible", (), (self._cedar_error or "absent",))
        else:
            cedar = self.cedar.decide(user, tool, parameters, confirmed, origin)
        final = builtin if self.mode == "shadow" else self._both(builtin, cedar)
        self._record(user, tool, parameters, confirmed, origin, builtin, cedar, final, time.perf_counter() - started)
        return final

    @staticmethod
    def _both(builtin: PermissionDecision, cedar: CedarVerdict) -> PermissionDecision:
        if builtin.decision is Decision.DENY:
            return builtin
        if cedar.decision is Decision.DENY:
            return PermissionDecision(Decision.DENY, f"refusé par la politique ({cedar.reason})")
        if Decision.REQUIRES_CONFIRMATION in (builtin.decision, cedar.decision):
            return PermissionDecision(Decision.REQUIRES_CONFIRMATION, "action à confirmer")
        return builtin

    def _record(self, user, tool, parameters, confirmed, origin, builtin, cedar, final, seconds) -> None:
        diverged = builtin.decision is not cedar.decision
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": self.mode, "user": user, "tool": tool.name,
                 "origin": origin, "confirmed": bool(confirmed), "parameter_names": sorted(map(str, parameters)),
                 "builtin": builtin.decision.value, "cedar": cedar.decision.value, "final": final.decision.value,
                 "policies": list(cedar.policies), "divergence": diverged, "ms": round(seconds * 1000, 2)}
        if cedar.errors:
            entry["errors"] = [re.sub(r"`[^`]*`", "`…`", e) for e in cedar.errors]
        if diverged or cedar.errors:
            log.warning("Politique : écart ou erreur %s", json.dumps(entry, ensure_ascii=False))
        if self._audit is None:
            return
        try:
            with self._lock:
                self._audit.parent.mkdir(parents=True, exist_ok=True)
                if self._audit.exists() and self._audit.stat().st_size > AUDIT_MAX_BYTES:
                    self._audit.replace(self._audit.with_suffix(self._audit.suffix + ".1"))
                with self._audit.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("Journal des décisions illisible (%s)", exc)


def build_gate(builtin, settings: dict, registry, users: dict[str, str], category, base: Path,
               llm_trusted: bool = True) -> PolicyGate:
    """[tools.policy] -> porte de décision. Cedar absent ou politiques invalides : en « both », tout est refusé."""
    mode = str(settings.get("engine", "builtin"))
    audit = settings.get("audit", "data/policy_decisions.jsonl")
    audit_path = (base / audit) if audit else None
    if mode == "builtin":
        return PolicyGate(builtin, mode)
    try:
        cedar = CedarPolicy(base / settings.get("policies", "policies/orion.cedar"),
                            base / settings.get("schema", "policies/orion.cedarschema.json"), registry, users, category,
                            float(settings.get("timeout", 0.5)), llm_trusted)
        return PolicyGate(builtin, mode, cedar, audit_path)
    except Exception as exc:  # noqa: BLE001 - « both » refusera tout, « shadow » le journalise
        log.error("Moteur de politique Cedar indisponible (%s) : mode %s", exc, mode)
        return PolicyGate(builtin, mode, None, audit_path, cedar_error=f"{type(exc).__name__}: {exc}"[:200])
