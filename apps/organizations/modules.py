"""Which parts of the platform an organization has switched on.

Site administrators turn modules on and off per organization. A switched-off
module disappears from the menus, and its pages answer with a short "not
enabled" page instead of their content. Its data is kept, so switching it back
on picks up where it left off.

The switches live in `Organization.settings["modules"]` as `{key: bool}`. A key
that is absent means enabled, so an organization created before a module
existed -- or before this file did -- keeps everything it had.

The core of the platform is not switchable: the dashboard, the AI Landscape
registry, people and access, notifications and the audit trail are what every
other module stands on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    group: str
    description: str
    #: URL namespaces whose pages belong to the module.
    apps: tuple[str, ...]


MODULES: tuple[Module, ...] = (
    Module("observability", "Observability", "Operations",
           "Executions and the live monitor.", ("executions",)),
    Module("ai_gateway", "AI Gateway", "Gateways",
           "The model gateway, model routing, the routing simulator and routing decisions.", ("gateway", "routing")),
    Module("mcp_gateway", "MCP Gateway", "Gateways",
           "MCP servers, the tool catalog, grants and tool calls.", ("mcp_gateway",)),
    Module("a2a_gateway", "A2A Gateway", "Gateways",
           "Agent-to-agent publishing, the skill catalog, contracts and communications.", ("a2a",)),
    Module("finops", "AI FinOps", "Operations",
           "Cost overview and explorer, developer tools, budgets, cost governance and optimization.", ("costs",)),
    Module("intake", "Use-Case Intake (G1)", "AI Governance", "New AI use cases and Gate G1.", ("intake",)),
    Module("classification", "Risk Classification (S2)", "AI Governance",
           "IRGF risk classification of AI systems.", ("classification",)),
    Module("lifecycle", "Authorizations, Changes and Exceptions", "AI Governance",
           "Deployment authorizations (G3), changes (G4) and exceptions (M17).", ("lifecycle",)),
    Module("controlplane", "Control Plane and Drift", "AI Governance",
           "Action verification (M12) and drift (M13).", ("controlplane",)),
    Module("compliance", "Compliance and Evidence", "AI Governance", "Evidence packs and attestations.", ("compliance",)),
    Module("risks", "Risks", "AI Governance", "The risk register and risk matrix.", ("risks",)),
    Module("approvals", "Approvals", "AI Governance", "The approval centre and approval rules.", ("approvals",)),
    Module("policies", "Policies", "AI Governance", "Control policies and their findings.", ("policies",)),
    Module("evaluations", "Evaluations", "AI Governance", "Evaluation suites and runs.", ("evaluations",)),
    Module("studio", "AI Studio", "Tools", "The Solution Advisor and the AI Sandbox.", ("studio",)),
    Module("onboarding", "Onboarding", "Tools", "The onboarding hub.", ("onboarding",)),
    Module("irgf_agent", "IRGF Agent", "Tools",
           "The in-app assistant that answers questions about the estate and the IRGF.", ("assistant",)),
)

MODULES_BY_KEY = {module.key: module for module in MODULES}
MODULE_FOR_APP = {app: module for module in MODULES for app in module.apps}
GROUPS = tuple(dict.fromkeys(module.group for module in MODULES))


def module_states(organization) -> dict[str, bool]:
    """Every module key, and whether the organization has it on."""
    stored = ((getattr(organization, "settings", None) or {}).get("modules") or {}) if organization else {}
    return {module.key: bool(stored.get(module.key, True)) for module in MODULES}


def is_enabled(organization, key: str) -> bool:
    if key not in MODULES_BY_KEY:
        return True
    return module_states(organization)[key]


def module_for_app(app_name: str | None) -> Module | None:
    return MODULE_FOR_APP.get(app_name or "")


def set_module_states(organization, states: dict[str, bool]) -> None:
    """Record the switches on the organization. The caller saves it."""
    settings = dict(organization.settings or {})
    settings["modules"] = {module.key: bool(states.get(module.key, True)) for module in MODULES}
    organization.settings = settings
