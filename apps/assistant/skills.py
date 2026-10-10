"""The IRGF Agent's skills: instructions plus the tools that go with them.

A skill is a Markdown file in `apps/assistant/skills/` with a short front
matter (name, title, description, tools) and the instructions the model follows
when the skill applies. Its tools are functions the model may call to look
things up -- the documentation, IRGF's codes, the decision rules the platform
enforces, the user's own organization -- so answers rest on what the platform
knows rather than on what a model remembers.

Adding a skill is adding a file; adding a tool is one entry in `TOOLS`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.assistant.knowledge import extract, knowledge

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
FRAMEWORK_DOCS = ("framework",)
PLATFORM_DOCS = ("irgf", "guide", "readme")


@dataclass
class Skill:
    name: str
    title: str
    description: str
    tools: list[str]
    instructions: str


def load_skills() -> list[Skill]:
    skills = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not match:
            continue
        meta = dict(
            (key.strip(), value.strip())
            for key, _, value in (line.partition(":") for line in match.group(1).splitlines())
            if key.strip()
        )
        skills.append(Skill(
            name=meta.get("name", path.stem),
            title=meta.get("title", path.stem),
            description=meta.get("description", ""),
            tools=[tool.strip() for tool in meta.get("tools", "").split(",") if tool.strip()],
            instructions=match.group(2).strip(),
        ))
    return skills


@dataclass
class ToolContext:
    """What a tool may read, and what the answer collects as it runs."""

    organization: object = None
    sources: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)

    def cite(self, section) -> None:
        entry = {"title": section.breadcrumb, "url": section.url, "doc": section.doc_title}
        if entry not in self.sources:
            self.sources.append(entry)

    def link(self, label: str, url_name: str) -> None:
        try:
            entry = {"label": label, "url": reverse(url_name)}
        except NoReverseMatch:
            return
        if entry not in self.links:
            self.links.append(entry)


# -- Tool implementations ---------------------------------------------------------------


def _search(context: ToolContext, query: str, docs) -> dict:
    results = knowledge().search(query, limit=3, docs=docs)
    if not results:
        return {"results": [], "note": "Nothing in these documents matches. Say so; do not guess."}
    out = []
    for score, section in results:
        context.cite(section)
        out.append({"section": section.breadcrumb, "document": section.doc_title,
                    "text": extract(section, query, 1800)})
    return {"results": out}


def search_irgf_framework(context: ToolContext, query: str) -> dict:
    return _search(context, query, FRAMEWORK_DOCS)


def search_control_tower_docs(context: ToolContext, query: str) -> dict:
    return _search(context, query, PLATFORM_DOCS)


def define_irgf_code(context: ToolContext, code: str) -> dict:
    from apps.assistant.answers import FRAMEWORK_URL, IMPLEMENTED, framework_terms

    raw = code.strip().upper().replace(" ", "")
    match = re.fullmatch(r"(?:CHAIN)?LINK0?([1-8])", raw)
    key = f"L0{match.group(1)}" if match else raw
    term = framework_terms().get(key)
    if term is None:
        return {"code": code, "found": False,
                "note": f"Not an IRGF code the reference defines. See {FRAMEWORK_URL}."}
    implemented = IMPLEMENTED.get(key)
    if implemented and implemented.link[1]:
        context.link(*implemented.link)
    return {
        "code": key, "found": True, "name": term[0], "definition": term[1],
        "in_the_control_tower": implemented.note if implemented else "Not implemented by the Control Tower yet.",
    }


def get_decision_rights(context: ToolContext, gate: str, tier: int | None = None) -> dict:
    from apps.lifecycle import irgf

    gate = gate.strip().upper()
    gate = {"S7": "G3", "S9": "G4"}.get(gate, gate)
    if gate not in irgf.DECISION_RIGHTS:
        return {"error": f"Decision rights are recorded for G3, G4 and M17, not {gate}."}
    tiers = [int(tier)] if tier else [1, 2, 3, 4]
    context.link({"G3": "Deployment Authorizations", "G4": "Changes", "M17": "Exceptions"}[gate],
                 {"G3": "lifecycle:authorizations", "G4": "lifecycle:changes", "M17": "lifecycle:exceptions"}[gate])
    return {
        "decision": irgf.GATE_LABELS[gate],
        "signatures_by_tier": {f"Tier {t}": irgf.describe_rights(gate, t) for t in tiers},
        "rules": [
            "Tier 4 is dual authorization: two signatures from two different people.",
            f"From Tier {irgf.INDEPENDENT_FROM_TIER}, the system's owner and the requester may not sign.",
            "Nobody decides a change they made or an exception they requested; service accounts never sign.",
        ],
    }


def get_governance_status(context: ToolContext) -> dict:
    from apps.assistant.answers import governance_facts

    if context.organization is None:
        return {"error": "No organization is selected."}
    context.link("Control Plane", "controlplane:overview")
    return {"organization": context.organization.name,
            "facts": {label: value for label, value, _ in governance_facts(context.organization)}}


def get_finops_summary(context: ToolContext) -> dict:
    from apps.costs.devtools import DevToolAnalytics, month_start
    from apps.costs.governance import GovernanceService
    from apps.costs.models import BudgetAlert
    from apps.costs.optimization import savings_summary
    from apps.costs.services import CostAnalyticsService

    organization = context.organization
    if organization is None:
        return {"error": "No organization is selected."}
    analytics = CostAnalyticsService(organization, days=30)
    summary = analytics.summary()
    today = timezone.now().date()
    copilot = sum(row["total"] for row in DevToolAnalytics(organization, month_start(today), today + timedelta(days=1)).individuals())
    governance = GovernanceService(organization)
    savings = savings_summary(organization)
    context.link("Cost Explorer", "costs:explorer")
    context.link("Cost Governance", "costs:governance")
    return {
        "organization": organization.name,
        "currency": "USD",
        "ai_runtime_spend": {"today": float(summary["today"]), "this_month": float(summary["month"]),
                             "projected_month": float(summary["projected_month"]),
                             "last_30_days": float(summary["window_total"])},
        "github_copilot_this_month": round(copilot, 2),
        "top_agents_last_30_days": [
            {"agent": row["label"], "spend": round(float(row["cost"]), 2)}
            for row in analytics.by("agent", "agent__name", limit=5)
        ],
        "budget_health": governance.health_counts(governance.budget_states()),
        "unacknowledged_budget_alerts": BudgetAlert.objects.for_organization(organization)
        .filter(acknowledged_at__isnull=True).count(),
        "optimization": {"open_recommendations": savings["open_count"],
                         "potential_monthly_savings": round(float(savings["open_savings"]), 2)},
    }


#: name -> (implementation, description, JSON schema of its parameters)
TOOLS = {
    "search_irgf_framework": (
        search_irgf_framework,
        "Search the IRGF framework reference (summarised from irgframework.org) for a topic: stages, gates, "
        "tiers, dimensions, modules, the agent governance chain, governance bodies.",
        {"type": "object", "properties": {"query": {"type": "string", "description": "What to look up."}},
         "required": ["query"]},
    ),
    "define_irgf_code": (
        define_irgf_code,
        "Define one IRGF identifier (S0-S10, G1-G5, D1-D5, M1-M20, or a chain link such as 'link 06'), and say "
        "whether and how the Control Tower implements it.",
        {"type": "object", "properties": {"code": {"type": "string", "description": "For example G3, M17, link 06."}},
         "required": ["code"]},
    ),
    "search_control_tower_docs": (
        search_control_tower_docs,
        "Search the AI Control Tower's documentation: how it implements IRGF and how each feature works.",
        {"type": "object", "properties": {"query": {"type": "string", "description": "What to look up."}},
         "required": ["query"]},
    ),
    "get_decision_rights": (
        get_decision_rights,
        "Who must sign a G3 (deployment authorization), G4 (material change) or M17 (exception) decision, by "
        "tier, read from the rules the platform enforces.",
        {"type": "object", "properties": {
            "gate": {"type": "string", "enum": ["G3", "G4", "M17"]},
            "tier": {"type": "integer", "minimum": 1, "maximum": 4, "description": "Omit for all tiers."}},
         "required": ["gate"]},
    ),
    "get_governance_status": (
        get_governance_status,
        "The user's organization right now: classifications by tier, G3 authorizations, G4 changes, M17 "
        "exceptions and unresolved drift.",
        {"type": "object", "properties": {}},
    ),
    "get_finops_summary": (
        get_finops_summary,
        "The user's organization's AI spend: runtime spend, GitHub Copilot, top agents, budget health, alerts "
        "and optimization savings.",
        {"type": "object", "properties": {}},
    ),
}


def tool_specs(skills: list[Skill]) -> list[dict]:
    """OpenAI function-calling definitions for the tools the skills declare."""
    names = []
    for skill in skills:
        names.extend(name for name in skill.tools if name in TOOLS and name not in names)
    return [
        {"type": "function", "function": {"name": name, "description": TOOLS[name][1], "parameters": TOOLS[name][2]}}
        for name in names
    ]


def run_tool(name: str, arguments: str, context: ToolContext) -> str:
    """Run one tool call and return its result as JSON for the model."""
    if name not in TOOLS:
        return json.dumps({"error": f"Unknown tool {name}."})
    try:
        kwargs = json.loads(arguments or "{}")
        if not isinstance(kwargs, dict):
            raise ValueError("arguments must be an object")
        return json.dumps(TOOLS[name][0](context, **kwargs), default=str)
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": f"Bad arguments for {name}: {exc}"})
