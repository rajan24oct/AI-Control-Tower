"""Answering a question about IRGF or the Control Tower.

In order of preference:

1. **Decision rights** ("who signs G3 at Tier 4?") come straight from the rules
   the platform enforces (`apps.lifecycle.irgf`), not from prose about them.
2. **The estate's own status** ("how many authorizations are in force?") is
   read live from the organization the person is working in.
3. Everything else is **answered from the documentation**: the most relevant
   section, the passages in it that match the question, and links to read on.

An IRGF code the documentation covers gets a one-line definition first. One it
does not cover is said to be outside what this platform implements, rather
than guessed at.

When `ASSISTANT_AGENT` names an agent, the retrieved passages are handed to its
model through the AI Gateway to phrase the answer -- so the assistant is itself
a governed agent, under policy, budget, audit and cost tracking like any other.
Any gateway refusal or failure falls back to the documentation answer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.html import escape
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.assistant.knowledge import extract, knowledge, render

logger = logging.getLogger(__name__)

#: Below this BM25 score the best section is a coincidence, not an answer.
MIN_SCORE = 4.0
MAX_QUESTION = 600

# -- IRGF vocabulary -------------------------------------------------------------------

AGENT_NAME = "IRGF Agent"
FRAMEWORK_URL = "https://irgframework.org/"
FRAMEWORK_DOC = "framework"


@dataclass(frozen=True)
class Implementation:
    """Where the Control Tower implements an IRGF element, and the page it lives on."""

    note: str
    section: str  # the platform guide's section that explains it
    link: tuple[str, str] = ("", "")  # (label, url name)


#: The framework defines every code (see docs/irgf-framework-reference.md, read
#: by `framework_terms`). This says where the platform implements one.
IMPLEMENTED = {
    "S2": Implementation("scored, tiered and countersigned in the Risk Classification register.",
                         "3. Risk classification: stage S2", ("Risk Classification", "classification:register")),
    "S3": Implementation("the Agent Card is on every agent's page and is required at Gate G3.",
                         "7. The Agent Card and the governance chain", ("Agents Registry", "inventory:agents")),
    "S6": Implementation("evaluations from the Evaluation Centre count as assurance evidence at Gate G3.",
                         "Deployment authorization (S7 · Gate G3)", ("Evaluation Centre", "evaluations:home")),
    "S7": Implementation("deployment authorizations with entry criteria scaled to the confirmed tier.",
                         "Deployment authorization (S7 · Gate G3)", ("Deployment Authorizations", "lifecycle:authorizations")),
    "S8": Implementation("the Control Plane compares running agents with what G3 authorized.",
                         "11. Phase 3: the Control Plane", ("Control Plane", "controlplane:overview")),
    "S9": Implementation("every difference from the authorized configuration becomes an AI Change Record.",
                         "Change (S9 · Gate G4)", ("Changes", "lifecycle:changes")),
    "S10": Implementation("the gateways refuse agents that are inactive or retired.",
                          "6. The kill-switch: chain link 06", ("Agents Registry", "inventory:agents")),
    "S0": Implementation("Use-Case Intake records the need, its measures and the non-AI alternative.",
                         "15. Use-case intake: S0, S1 and Gate G1", ("Use-Case Intake", "intake:list")),
    "S1": Implementation("Use-Case Intake records the decision supported, who is affected, launch autonomy, the "
                         "data path and provisional D1–D5 scores.",
                         "15. Use-case intake: S0, S1 and Gate G1", ("Use-Case Intake", "intake:list")),
    "G1": Implementation("Use-Case Intake decides G1 with IRGF's five outcomes: approved by rule at Tier 1, by the "
                         "Risk and Compliance Lead at Tier 1–2, by the AI Governance Body at Tier 3–4.",
                         "15. Use-case intake: S0, S1 and Gate G1", ("Use-Case Intake", "intake:list")),
    "G2": Implementation("a countersigned (confirmed) tier is the G2 evidence the platform records.",
                         "3. Risk classification: stage S2", ("Risk Classification", "classification:register")),
    "G3": Implementation("Deployment Authorizations, signed by tier as IRGF's decision rights require.",
                         "Deployment authorization (S7 · Gate G3)", ("Deployment Authorizations", "lifecycle:authorizations")),
    "G4": Implementation("Changes: material changes wait at G4, and a major change suspends the authorization.",
                         "Change (S9 · Gate G4)", ("Changes", "lifecycle:changes")),
    "M4": Implementation("the scoring form, with suggested starting scores from the registry.",
                         "3. Risk classification: stage S2", ("Risk Classification", "classification:register")),
    "M5": Implementation("the tier is computed live with the same matrix and safety floor.",
                         "3. Risk classification: stage S2", ("Risk Classification", "classification:register")),
    "M6": Implementation("countersignature, enforced independent of the scorer and the owners.",
                         "3. Risk classification: stage S2", ("Risk Classification", "classification:register")),
    "M10": Implementation("each agent page shows where it stands on all eight links.",
                          "7. The Agent Card and the governance chain", ("Agents Registry", "inventory:agents")),
    "M12": Implementation("the Control Plane, comparing what runs with what was authorized.",
                          "11. Phase 3: the Control Plane", ("Control Plane", "controlplane:overview")),
    "M13": Implementation("drift records, routed and timed by category and tier.",
                          "Drift records (M13)", ("Drift", "controlplane:drift")),
    "M14": Implementation("policies enforced in the AI, MCP and A2A gateways, and runtime guardrails that block or "
                          "redact prompt injection, personal data, secrets and restricted topics.",
                          "6.12 Runtime guardrails", ("Policies", "policies:list")),
    "M15": Implementation("decision rights by tier on every G3, G4 and M17 decision.",
                          "Decision rights (M15, M16)", ("Deployment Authorizations", "lifecycle:authorizations")),
    "M16": Implementation("decision rights by tier on every G3, G4 and M17 decision.",
                          "Decision rights (M15, M16)", ("Deployment Authorizations", "lifecycle:authorizations")),
    "M17": Implementation("time-boxed exceptions, with no renewal at Tier 4.",
                          "Exceptions (M17)", ("Exceptions", "lifecycle:exceptions")),
    "L02": Implementation("service accounts bound to the agents they run.",
                          "5. Agent identity: chain link 02", ("Agents Registry", "inventory:agents")),
    "L04": Implementation("tool allow-lists and MCP grants enforced by the MCP Gateway.",
                          "7. The Agent Card and the governance chain", ("MCP Gateway", "mcp_gateway:overview")),
    "L06": Implementation("action limits checked before every tool call, and a kill-switch on every agent.",
                          "6. The kill-switch: chain link 06", ("Agents Registry", "inventory:agents")),
    "L07": Implementation("every action verified after it happens, within a ceiling set per tier.",
                          "Verification (link 07)", ("Control Plane", "controlplane:overview")),
    "L08": Implementation("an immutable audit trail of every change and decision.",
                          "6.8 Audit trail", ("Audit Log", "audit:list")),
}
for _dimension in ("D1", "D2", "D3", "D4", "D5"):
    IMPLEMENTED[_dimension] = Implementation(
        "scored 1–4 on the classification form.", "3. Risk classification: stage S2",
        ("Risk Classification", "classification:register"),
    )

#: Where each code is defined in the framework reference, and which column is its name.
FRAMEWORK_TABLES = {
    "S": "Lifecycle stages S0 to S10",
    "G": "The five gates",
    "D": "Risk dimensions D1 to D5 and the four tiers",
    "M": "The twenty modules M1 to M20",
    "L": "The agent governance chain",
}
#: A gate's own section in the reference, for "what does G3 require?".
GATE_SECTIONS = {
    "G1": "Gate G1: qualification and provisional risk",
    "G2": "Gate G2: architecture and data readiness",
    "G3": "Gate G3: deployment authorization",
    "G4": "Gate G4: material change",
    "G5": "Gate G5: retirement",
}

#: The guided questions the agent offers, by topic. Every one is answered by the
#: logic below; tests ask each of them.
GUIDED = [
    ("The framework", [
        "What is the IRGF?",
        "What are the eleven lifecycle stages?",
        "What are the five gates?",
        "What are the twenty modules?",
    ]),
    ("Gates and decisions", [
        "What does Gate G2 require?",
        "What evidence does Gate G3 need?",
        "Who must sign Gate G3 for a Tier 4 agent?",
        "What happens to a major change at Gate G4?",
        "What does Gate G5 check at retirement?",
    ]),
    ("Risk and tiers", [
        "How is a risk tier calculated?",
        "What are the dimensions D1 to D5?",
        "What is the safety-override floor?",
        "What changes between Tier 1 and Tier 4?",
    ]),
    ("Agents", [
        "What are the eight links of the governance chain?",
        "What goes in an Agent Card?",
        "How does the kill switch work?",
    ]),
    ("In the Control Tower", [
        "How do I propose a new AI use case?",
        "How do I get an evidence pack for an auditor?",
        "How do I get a technical blueprint for an AI idea?",
        "What are the IRGF phases in the Control Tower?",
        "Where do my governance gates stand?",
        "What does the Control Plane compare?",
        "How do budget forecast alerts work?",
    ]),
]
SUGGESTIONS = [question for _, questions in GUIDED for question in questions[:1]] + [
    "Who must sign Gate G3 for a Tier 4 agent?", "Where do my governance gates stand?",
]

#: Questions people ask in their own words, and the section that answers them.
#: Search alone finds the words; this finds the intent.
TOPICS = [
    (r"evidence pack|auditor|compliance|eu ai act|iso.?42001|nist|attest", "6.11 Compliance and evidence"),
    (r"guardrail|prompt injection|redact|secret", "6.12 Runtime guardrails"),
    (r"blueprint|solution advisor|tech(nology)? stack|sandbox|playground|try a prompt", "6.13 Solution Studio and AI Sandbox"),
    (r"use.?cases?|propos|intake|new ai idea|g1.*(control tower|platform|here|work)",
     "15. Use-case intake: S0, S1 and Gate G1"),
    (r"what is (the )?irgf|irgf (is|stand)|about the (irgf|framework)|^(the )?irgf\??$", "What IRGF governs"),
    (r"\b(lifecycle )?stages\b|\bs0\b.*\bs10\b|eleven", "Lifecycle stages S0 to S10"),
    (r"\b(all|the|five) gates\b|\bgates\b\??$|gate outcomes?|outcomes?.*gate", "The five gates"),
    (r"\bmodules\b|twenty|m1\b.*m20", "The twenty modules M1 to M20"),
    (r"governance bod|\barb\b|architecture review board|federat|governance sponsor", "Governance bodies and roles"),
    (r"chain|\blinks?\b", "The agent governance chain"),
    (r"safety.?(override)?.?floor|\btier\s*1\b.*\btier\s*4\b|tier (posture|definition)|what.*tiers? mean",
     "Risk dimensions D1 to D5 and the four tiers"),
    (r"\bphases?\b", "1. The fit in one paragraph"),
    (r"control plane.*\b(compar|check|watch|look)", "What is compared (M12)"),
    (r"\b(tier|tiers)\b.*\b(calculat|comput|work|derive|decide|determin|matrix)|\bhow\b.*\btier",
     "Risk dimensions D1 to D5 and the four tiers"),
    (r"\b(dimension|scor)", "Risk dimensions D1 to D5 and the four tiers"),
    (r"kill.?switch|suspend", "6. The kill-switch: chain link 06"),
    (r"agent card", "The agent governance chain"),
    (r"entry criteri|request authori", "Deployment authorization (S7 · Gate G3)"),
    (r"\b(roles?|who does what|permissions?)\b", "8. Who does what"),
    (r"\bdrift\b.*\b(resolv|end|clos)", "Four ways a record ends"),
    (r"\b(composite|application authori)", "12. Phase 4: applications and composite authority"),
    (r"\bre-?score|trigger", "3. Risk classification: stage S2"),
    (r"\broadmap|what('s| is) next|not (yet )?implement", "14. Mapping and roadmap"),
]
CODE = re.compile(r"\b(?:([SGMD])\s?(\d{1,2})|(?:chain\s+)?link\s+0?([1-8]))\b", re.IGNORECASE)

#: Pages worth offering when a question is about them.
APP_LINKS = [
    (r"\bbudget", "Budgets", "costs:budgets"),
    (r"\b(cost|spend|finops)", "Cost Explorer", "costs:explorer"),
    (r"\b(copilot|github|seat)", "Developer Tools", "costs:developer_tools"),
    (r"\b(optimi[sz]|saving|recommendation)", "Cost Optimization", "costs:optimization"),
    (r"\b(polic)", "Policies", "policies:list"),
    (r"\b(approval|approve)", "Approvals", "approvals:centre"),
    (r"\brisk register|\brisks?\b", "Risks", "risks:list"),
    (r"\b(evaluat|assurance)", "Evaluation Centre", "evaluations:home"),
    (r"\b(gateway|routing|fallback)", "AI Gateway", "gateway:overview"),
    (r"\bmcp\b|\btool", "MCP Gateway", "mcp_gateway:overview"),
    (r"\ba2a\b|agent.to.agent|contract", "A2A Gateway", "a2a:overview"),
    (r"\b(audit)", "Audit Log", "audit:list"),
]



@dataclass
class Answer:
    html: str
    sources: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    mode: str = "documentation"
    #: The answer as plain text, sent back with later questions as conversation history.
    text: str = ""

    def as_dict(self) -> dict:
        text = self.text or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self.html)).strip()
        return {"html": self.html, "sources": self.sources, "links": self.links,
                "suggestions": self.suggestions, "mode": self.mode, "text": text[:1500]}


def _url(name: str) -> str:
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def _link(label: str, name: str) -> dict | None:
    url = _url(name)
    return {"label": label, "url": url} if url else None


def find_section(title: str):
    for section in knowledge().sections:
        if section.title == title:
            return section
    return None


# -- Intents ---------------------------------------------------------------------------


GATE_WORDS = {
    "G3": r"\bg3\b|gate 3|s7\b|deploy\w* authori[sz]",
    "G4": r"\bg4\b|gate 4|s9\b|\bchange",
    "M17": r"\bm17\b|exception",
}


def decision_rights_answer(question: str) -> Answer | None:
    text = question.lower()
    if not re.search(r"\b(who|sign|signs|signature|decide|decides|approve|approves|approver|decision rights)\b", text):
        return None
    gates = [gate for gate, pattern in GATE_WORDS.items() if re.search(pattern, text)]
    if not gates:
        return None
    from apps.lifecycle import irgf

    tiers = [int(t) for t in re.findall(r"tier\s*([1-4])", text)] or [1, 2, 3, 4]
    rows = []
    for gate in gates:
        for tier in tiers:
            rows.append(f"<tr><td>{irgf.GATE_LABELS[gate]}</td><td>Tier {tier}</td>"
                        f"<td>{irgf.describe_rights(gate, tier)}</td></tr>")
    notes = [
        "Tier 4 is dual authorization: two signatures from two different people." if 4 in tiers else "",
        f"From Tier {irgf.INDEPENDENT_FROM_TIER}, signers come from outside the delivery line: neither the "
        "agent's owner nor the requester may sign." if any(t >= irgf.INDEPENDENT_FROM_TIER for t in tiers) else "",
        "Nobody decides a change they made or an exception they requested, and service accounts never sign.",
    ]
    html = (
        "<p>These are the signatures the platform requires, read from the rules it enforces:</p>"
        "<div class=\"table-responsive\"><table class=\"table table-sm ct-assistant-table\"><thead><tr>"
        "<th>Decision</th><th>Tier</th><th>Who signs</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        "<ul>" + "".join(f"<li>{note}</li>" for note in notes if note) + "</ul>"
    )
    section = find_section("Decision rights (M15, M16)")
    bodies = find_section("Governance bodies and roles")
    links = [link for link in (
        _link("Deployment Authorizations", "lifecycle:authorizations") if "G3" in gates else None,
        _link("Changes", "lifecycle:changes") if "G4" in gates else None,
        _link("Exceptions", "lifecycle:exceptions") if "M17" in gates else None,
    ) if link]
    return Answer(
        html=html,
        sources=[{"title": s.breadcrumb, "url": s.url, "doc": s.doc_title} for s in (section, bodies) if s],
        links=links,
        suggestions=["What evidence does Gate G3 need?", "What happens to a major change at Gate G4?"],
        mode="rules",
    )


STATUS_PATTERN = re.compile(
    r"\b(my|our|we|current|currently|status|stand|how many|which|pending|in force|open|outstanding|overdue)\b"
)
STATUS_TOPICS = re.compile(r"(authori[sz]|gate|g3|g4|m17|exception|drift|classif|tier|change|irgf|governance)")


def governance_facts(organization) -> list[tuple[str, str, str]]:
    """The organization's IRGF position right now: (label, value, url name)."""
    from apps.classification.models import CURRENT_STATUSES, ClassificationStatus, RiskClassification
    from apps.controlplane.models import UNRESOLVED_STATUSES, DriftRecord
    from apps.lifecycle.models import (
        OPEN_CHANGE_STATUSES,
        AuthorizationStatus,
        ChangeRecord,
        ChangeStatus,
        DeploymentAuthorization,
        ExceptionStatus,
        GovernanceException,
    )

    authorizations = DeploymentAuthorization.objects.for_organization(organization)
    changes = ChangeRecord.objects.for_organization(organization)
    exceptions = GovernanceException.objects.for_organization(organization)
    soon = timezone.now() + timezone.timedelta(days=7)
    current = RiskClassification.objects.for_organization(organization).filter(status__in=CURRENT_STATUSES)
    tiers = {tier: current.filter(tier=tier).count() for tier in (1, 2, 3, 4)}
    facts = [
        ("S2 classifications", f"{current.count()} current: " + ", ".join(f"Tier {t} {n}" for t, n in tiers.items())
         + f"; {current.filter(status=ClassificationStatus.PROVISIONAL).count()} awaiting countersignature",
         "classification:register"),
        ("G3 authorizations", f"{authorizations.filter(status=AuthorizationStatus.AUTHORIZED).count()} in force, "
         f"{authorizations.filter(status=AuthorizationStatus.PENDING).count()} awaiting a decision, "
         f"{authorizations.filter(status=AuthorizationStatus.SUSPENDED).count()} suspended",
         "lifecycle:authorizations"),
        ("G4 changes", f"{changes.filter(status=ChangeStatus.AWAITING_G4).count()} awaiting Gate G4, "
         f"{changes.filter(status__in=OPEN_CHANGE_STATUSES).count()} still open in all", "lifecycle:changes"),
        ("M17 exceptions", f"{exceptions.filter(status=ExceptionStatus.ACTIVE).count()} active "
         f"({exceptions.filter(status=ExceptionStatus.ACTIVE, expires_at__lte=soon).count()} expiring within 7 days), "
         f"{exceptions.filter(status=ExceptionStatus.PENDING).count()} awaiting a decision", "lifecycle:exceptions"),
        ("M13 drift", f"{DriftRecord.objects.for_organization(organization).filter(status__in=UNRESOLVED_STATUSES).count()}"
         " unresolved records", "controlplane:drift"),
    ]
    return facts


def estate_answer(question: str, organization) -> Answer | None:
    text = question.lower()
    if organization is None or not (STATUS_PATTERN.search(text) and STATUS_TOPICS.search(text)):
        return None
    facts = governance_facts(organization)
    rows = "".join(
        f"<li><strong>{label}:</strong> {value}. <a href=\"{_url(name)}\">Open</a></li>" for label, value, name in facts
    )
    return Answer(
        html=f"<p>Where <strong>{organization.name}</strong> stands right now:</p><ul>{rows}</ul>",
        links=[_link("Control Plane", "controlplane:overview")] if _url("controlplane:overview") else [],
        suggestions=["Who must sign Gate G3 for a Tier 4 agent?", "What happens to a major change at Gate G4?"],
        mode="live",
    )


def framework_terms() -> dict[str, tuple[str, str]]:
    """Every IRGF code the framework reference defines: code -> (name, what it is).

    Read from the reference's own tables, so the definitions live in one place.
    """
    terms: dict[str, tuple[str, str]] = {}
    for prefix, title in FRAMEWORK_TABLES.items():
        section = find_section(title)
        if section is None:
            continue
        for line in section.body.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 2 or not line.lstrip().startswith("|"):
                continue
            code = cells[0]
            if prefix == "L" and re.fullmatch(r"0[1-8]", code):
                code = f"L{code}"
            if not re.fullmatch(rf"{prefix}\d{{1,2}}", code):
                continue
            # Stages, gates and links: name | what it settles | ...; modules: name | group | purpose.
            what = cells[3] if prefix == "M" and len(cells) > 3 else (cells[2] if len(cells) > 2 else "")
            if prefix == "G" and len(cells) > 3:
                what = f"{cells[3]} (after {cells[2]})"
            terms[code] = (cells[1], what)
    return terms


def glossary_notes(question: str) -> tuple[list[str], list[dict], list[str]]:
    """Definitions for the IRGF codes a question names, and the sections they point to.

    The framework's definition first, then -- where the platform implements the
    element -- what the Control Tower does with it.
    """
    terms = framework_terms()
    notes, links, sections = [], [], []
    # "D1 to D5" or "S0–S10" is the whole set, which the topic routing answers;
    # defining its two ends would be noise.
    question = re.sub(r"\b([SGMD])\d{1,2}\s*(?:to|-|–|—|through)\s*\1?\d{1,2}\b", " ", question, flags=re.I)
    for letter, number, link_number in CODE.findall(question):
        code = f"L0{link_number}" if link_number else f"{letter.upper()}{int(number)}"
        label = f"Chain link {code[1:]}" if code.startswith("L") else code
        term = terms.get(code)
        implemented = IMPLEMENTED.get(code)
        if term is None:
            continue
        name, what = term
        note = f"**{label} · {name}** (IRGF): {what[0].lower() + what[1:] if what else ''}".rstrip(": ")
        if not note.endswith("."):
            note += "."
        if implemented:
            note += f" *In the Control Tower:* {implemented.note}"
            sections.append(implemented.section)
            link = _link(*implemented.link) if implemented.link[1] else None
            if link and link not in links:
                links.append(link)
        else:
            note += " The Control Tower does not implement this element yet."
        notes.append(note)
        if code in GATE_SECTIONS:
            sections.insert(0, GATE_SECTIONS[code])
    return notes, links, sections


# -- The service -----------------------------------------------------------------------------


class AssistantService:
    @classmethod
    def answer(cls, question: str, *, organization=None, user=None, history=None) -> Answer:
        question = (question or "").strip()[:MAX_QUESTION]
        if not question:
            return Answer(html=f"<p>I'm the {AGENT_NAME}. Ask me about IRGF stages and gates, or how the "
                               "Control Tower works.</p>", suggestions=SUGGESTIONS[:4])

        # ChatGPT, with the agent's skills, unless a governed gateway agent is named instead.
        notice = ""
        from apps.assistant import chatgpt

        if not getattr(settings, "ASSISTANT_AGENT", "") and chatgpt.configured():
            try:
                reply = chatgpt.ask(question, history=history, organization=organization, agent_name=AGENT_NAME)
                return Answer(html=render(reply.text), sources=reply.sources, links=reply.links,
                              suggestions=follow_ups(question, reply.sources), mode="chatgpt", text=reply.text)
            except chatgpt.ChatGPTUnavailable as exc:
                logger.warning("IRGF Agent fell back to the documentation: %s", exc)
                notice = (f"<p class=\"ct-assistant-notice\">{escape(str(exc))} "
                          "Answering from the documentation instead.</p>")

        answer = cls._without_model(question, organization)
        if notice:
            answer.html = notice + answer.html
        return answer

    @classmethod
    def _without_model(cls, question: str, organization) -> Answer:
        for intent in (decision_rights_answer,):
            answer = intent(question)
            if answer:
                return answer
        live = estate_answer(question, organization)
        if live:
            return live
        return cls.from_documentation(question, organization=organization)

    @classmethod
    def from_documentation(cls, question: str, *, organization=None) -> Answer:
        notes, links, preferred = glossary_notes(question)
        preferred += [title for pattern, title in TOPICS if re.search(pattern, question, re.I)][:1]
        governance = bool(re.search(r"irgf|gate|stage|tier|phase|drift|authori|classif|exception|kill|module|"
                                    r"dimension|chain|framework", question, re.I))
        platform = bool(re.search(r"control tower|platform|\bmy\b|\bour\b|in the app|phases?", question, re.I))
        prefer = ("irgf" if platform else FRAMEWORK_DOC) if governance or notes else ""
        results = knowledge().search(question, limit=4, prefer=prefer)

        # A section a definition points to outranks a lucky keyword match.
        # It goes to the top whether or not the search found it too.
        for title in reversed(preferred):
            section = find_section(title)
            if section:
                results = [(score, s) for score, s in results if s is not section]
                results.insert(0, (MIN_SCORE * 10, section))

        if not results or results[0][0] < MIN_SCORE:
            if notes:
                return Answer(html=render("\n\n".join(notes)), links=links, suggestions=SUGGESTIONS[:3])
            return Answer(
                html="<p>I couldn't find that in the IRGF framework or the Control Tower's documentation. I "
                     "answer questions about IRGF stages, gates, tiers and modules, and about how the Control "
                     "Tower applies them. Try rephrasing, pick a guided question, or see "
                     f"<a href=\"{FRAMEWORK_URL}\" target=\"_blank\" rel=\"noopener\">irgframework.org</a>.</p>",
                suggestions=SUGGESTIONS[:4],
            )

        best = results[0][1]
        passage = extract(best, question)
        sources, seen = [], set()
        for _, section in results:
            if section.id in seen:
                continue
            seen.add(section.id)
            sources.append({"title": section.breadcrumb, "url": section.url, "doc": section.doc_title})
            if len(sources) == 3:
                break
        for pattern, label, name in APP_LINKS:
            if re.search(pattern, question, re.I):
                link = _link(label, name)
                if link and link not in links:
                    links.append(link)

        generated = cls.via_gateway(question, results, organization)
        if generated:
            return Answer(html=generated, sources=sources, links=links[:3], mode="gateway",
                          suggestions=related_questions(best))

        markdown = "\n\n".join([*notes, f"From **{best.breadcrumb}**:", passage])
        return Answer(html=render(markdown), sources=sources, links=links[:3],
                      suggestions=related_questions(best))

    @staticmethod
    def via_gateway(question: str, results, organization) -> str:
        """Phrase the answer with a governed agent's model, if one is configured."""
        agent_ref = getattr(settings, "ASSISTANT_AGENT", "")
        if not agent_ref or organization is None:
            return ""
        from apps.agents.models import AIAgent
        from apps.gateway.services import GatewayError, GatewayRequest, GatewayService

        agent = AIAgent.objects.for_organization(organization).filter(agent_id=agent_ref).first()
        if agent is None:
            return ""
        context = "\n\n".join(f"## {section.breadcrumb}\n{extract(section, question, 2500)}" for _, section in results[:3])
        system = (
            f"You are the {AGENT_NAME}. You answer questions about the IRGF framework ({FRAMEWORK_URL}) and how "
            "the AI Control Tower applies it. Use only the "
            "documentation below. If it does not answer the question, say so. Be concise; use short paragraphs "
            "or bullet points in Markdown.\n\n" + context
        )
        try:
            response = GatewayService.complete(
                organization,
                GatewayRequest(agent=agent, messages=[{"role": "system", "content": system},
                                                      {"role": "user", "content": question}],
                               max_tokens=600, metadata={"source": "assistant"}),
            )
        except GatewayError as exc:
            logger.info("Assistant fell back to documentation: %s", exc)
            return ""
        except Exception:
            logger.exception("Assistant gateway call failed")
            return ""
        return render(response.output_text or "")


def follow_ups(question: str, sources: list[dict]) -> list[str]:
    """Suggestions after a ChatGPT answer: near the first section it looked up."""
    for source in sources:
        for section in knowledge().sections:
            if section.url == source["url"]:
                return [q for q in related_questions(section) if q.lower() != question.lower()][:2]
    return SUGGESTIONS[:2]


def related_questions(section) -> list[str]:
    """A couple of follow-ups that stay near what was just answered."""
    title = section.breadcrumb.lower()
    if section.doc == FRAMEWORK_DOC:
        if "gate" in title:
            return ["Who must sign Gate G3 for a Tier 4 agent?", "Where do my governance gates stand?"]
        if "stage" in title:
            return ["What are the five gates?", "What happens to a major change at Gate G4?"]
        if "dimension" in title or "tier" in title:
            return ["What is the safety-override floor?", "What changes between Tier 1 and Tier 4?"]
        if "module" in title:
            return ["What does M12 do?", "What is M17?"]
        if "chain" in title:
            return ["What goes in an Agent Card?", "How does the kill switch work?"]
        return ["What are the eleven lifecycle stages?", "What are the five gates?"]
    if "g3" in title or "authori" in title:
        return ["Who must sign Gate G3 for a Tier 4 agent?", "Where do my governance gates stand?"]
    if "g4" in title or "change" in title:
        return ["Who decides a change at Gate G4?", "What does the Control Plane compare?"]
    if "classification" in title or "s2" in title:
        return ["What does Gate G2 require?", "What triggers a re-score?"]
    if "control plane" in title or "drift" in title:
        return ["How are drift records resolved?", "Where do my governance gates stand?"]
    return SUGGESTIONS[:2]
