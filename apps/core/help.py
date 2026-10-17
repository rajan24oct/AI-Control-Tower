"""In-page help: what a module is for, how to run it well, and worked examples.

Each page looks up its help by `"<app namespace>:<url name>"` first, then by
the app namespace, so a module gets one panel and its busiest pages can say
something more specific. Examples use the platform's own API; `{api}` in an
example is replaced with this deployment's API root when the panel renders.

Content is data, not templates: keeping it here keeps the panel's markup in
one place and makes the wording reviewable like any other change.
"""

from __future__ import annotations

# -- Shared snippets ----------------------------------------------------------

_KEY = '-H "X-API-Key: $CONTROL_TOWER_KEY"'

GATEWAY_CALL = f"""curl -X POST {{api}}/gateway/chat/ \\
  {_KEY} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "agent": "customer-support-agent",
    "messages": [
      {{"role": "system", "content": "You draft replies for review."}},
      {{"role": "user", "content": "Why was my card declined abroad?"}}
    ],
    "max_tokens": 400,
    "trace_id": "ticket-48213"
  }}'"""

MCP_CALL = f"""curl -X POST {{api}}/mcp-gateway/tools/call/ \\
  {_KEY} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "agent": "claims-triage-agent",
    "tool": "servicenow__search_incidents",
    "arguments": {{"query": "claims portal outage"}},
    "execution_id": "run-7f3a"
  }}'"""

MCP_CLIENT = """{
  "mcpServers": {
    "control-tower": {
      "url": "{api}/mcp/claims-triage-agent/",
      "headers": {"X-API-Key": "${CONTROL_TOWER_KEY}"}
    }
  }
}"""

A2A_SEND = f"""curl -X POST {{api}}/a2a-gateway/messages/ \\
  {_KEY} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "agent": "customer-support-agent",
    "recipient": "claims-triage-agent",
    "skill": "claim-status",
    "text": "Where does claim CLM-20931 stand?",
    "data_classification": "confidential"
  }}'"""

POLICY_CHECK = f"""curl -X POST {{api}}/policy/check/ \\
  {_KEY} \\
  -H "Content-Type: application/json" \\
  -d '{{
    "agent": "fraud-detection-agent",
    "action": "freeze_account",
    "tool": "freeze_account",
    "data_classification": "restricted",
    "environment": "production",
    "estimated_cost": "0.40"
  }}'"""


def _ex(title: str, text: str = "", code: str = "", lang: str = "bash") -> dict:
    return {"title": title, "text": text, "code": code, "lang": lang}


# -- Help by page ---------------------------------------------------------------

HELP: dict[str, dict] = {
    # ---- Gateways ------------------------------------------------------------
    "gateway": {
        "title": "AI Gateway",
        "summary": "One governed entry point for every model call: identity, policy, budget, "
        "guardrails and routing are applied before a provider is reached, and every call "
        "is recorded as an execution.",
        "practices": [
            "Send all model traffic through the gateway. A direct provider key in an agent "
            "is an ungoverned path: revoke it once the agent is migrated.",
            "Call as a named agent (bind the API key to it), so policy, budgets and the "
            "kill switch apply to the agent rather than to a shared key.",
            "Let routing choose the model. Pin a model only where you must, such as "
            "evaluations that measure one model.",
            "Pass a trace_id from your engine so a model call joins up with the run, tool "
            "calls and A2A hand-offs it belongs to.",
            "Treat 403 (policy), 402 (budget) and 202 (approval needed) as answers, not "
            "errors: do not retry them.",
            "Retry only 5xx and timeouts, with backoff. The gateway has already walked the "
            "fallback chain before it gives up.",
            "Never put secrets or raw identifiers in prompts. Guardrails catch common "
            "patterns, but redaction belongs upstream.",
        ],
        "examples": [
            _ex("Call a model as an agent",
                "The gateway resolves the agent, checks policy and budget, routes and records the run.",
                GATEWAY_CALL),
            _ex("Handle the gateway's answers",
                "Branch on the status code instead of retrying everything.",
                """r = post("/gateway/chat/", json=payload)
if r.status_code == 200:   use(r.json()["output_text"])
elif r.status_code == 202: queue_for_human(r.json())   # approval required
elif r.status_code in (402, 403):
    log_refusal(r.json()["error"], r.json()["detail"])  # budget / policy: do not retry
else:
    retry_with_backoff()                               # 5xx, timeouts""", "python"),
            _ex("Stream a long answer", "Add \"stream\": true to the same request. Usage is "
                "estimated if the provider does not report it, and the run is flagged as estimated."),
        ],
        "related": [("Model Routing", "routing:list"), ("Policies", "policies:list"),
                    ("Budgets", "costs:budgets"), ("Executions", "executions:list")],
    },
    "routing": {
        "title": "Model Routing",
        "summary": "Routing policies decide which approved models may serve a class of work, "
        "and in what order. The gateway walks the plan: a primary, then a ranked fallback chain "
        "with unhealthy and non-compliant models already removed.",
        "practices": [
            "Write a narrow policy per class of work (customer-facing, confidential, research), "
            "plus one estate-wide default. Agent-scoped policies beat general ones at the same priority.",
            "Pick the strategy for the risk: reliability for customer-facing work, cost for "
            "internal research, latency for real-time scoring.",
            "Keep at least one fallback on a different provider. Two models on one provider "
            "fail together.",
            "Use allowed and blocked providers for data residency, for example EU-only for "
            "confidential documents.",
            "Set a minimum context window and a cost ceiling instead of naming models, so new "
            "approved models join the plan automatically.",
            "Leave \"approved models only\" on. Routing to an unreviewed model bypasses the "
            "model registry.",
            "Review the fallback rate weekly. A rising rate on one primary is an early sign "
            "of provider trouble.",
        ],
        "examples": [
            _ex("Customer-facing chat",
                "Strategy: Highest reliability. Agents: Customer Support, Claims Triage. "
                "Environments: production. Capabilities: supports_tool_calling. "
                "Allowed providers: Azure OpenAI, Bedrock, OpenAI. Fallback depth: 2."),
            _ex("Confidential data stays in the EU",
                "Strategy: Balanced. Agents: Underwriting Copilot, Onboarding Extractor. "
                "Blocked providers: OpenAI Platform (US). Degraded providers kept as fallbacks."),
            _ex("Cheap long-document research",
                "Strategy: Lowest cost. Minimum context: 32,000 tokens. Cost ceiling: $0.003 per "
                "1k input tokens. Any approved model that fits is eligible, and the cheapest ranks first."),
        ],
        "related": [("Routing Simulator", "routing:simulator"), ("Routing Decisions", "routing:decisions"),
                    ("Models Registry", "inventory:models"), ("AI Gateway", "gateway:overview")],
    },
    "routing:simulator": {
        "title": "Routing Simulator",
        "summary": "Ask \"what would route now, and why?\" without sending a request. The plan uses "
        "live provider health and observed reliability, so it is the answer the gateway would give right now.",
        "practices": [
            "Simulate before you save a policy change, and again after. Compare the chains.",
            "Simulate as a specific agent: agent-scoped policies only apply when an agent is given.",
            "Read the exclusions. When a plan is empty, they say exactly which rule removed each model.",
            "Re-run when a provider changes health. Degraded providers move down or out of the chain.",
        ],
        "examples": [
            _ex("Why is my agent not on GPT-4o?",
                "Simulate as the agent. If GPT-4o appears under Excluded with \"cost above ceiling\", "
                "the policy's cost ceiling is doing its job. Raise it or accept the cheaper model."),
            _ex("No route",
                "Asking for a 2,000,000-token context gives an empty plan: every model is excluded on "
                "context window. The same request through the gateway returns no_route_available."),
        ],
        "related": [("Model Routing", "routing:list"), ("Routing Decisions", "routing:decisions")],
    },
    "routing:decisions": {
        "title": "Routing Decisions",
        "summary": "Every plan the router produced, frozen as sent, and what served in the end. "
        "This is the audit trail for \"why did this agent use that model?\"",
        "practices": [
            "Filter on fallback outcomes. A cluster on one primary means that provider is struggling.",
            "\"Chain exhausted\" means no model could serve. Check the exclusions before relaxing a policy.",
            "Open the run behind a decision (click its time) to see the prompt, attempts and cost together.",
            "Decisions are frozen at decision time. Re-simulating later answers a different question, "
            "because health moves.",
        ],
        "examples": [
            _ex("Reading a fallback",
                "Primary: Claude Sonnet, Served: GPT-4o (attempt 2). The run's attempts show Bedrock "
                "returned 429, and the next model in the frozen chain served."),
        ],
        "related": [("Model Routing", "routing:list"), ("Executions", "executions:list")],
    },
    "mcp_gateway": {
        "title": "MCP Gateway",
        "summary": "Agents reach tools on MCP servers through one governed endpoint. Servers are "
        "registered and approved, each tool is reviewed, and grants say which agent may call what.",
        "practices": [
            "Register every MCP server, internal or third party. An agent pointing straight at a "
            "server is an ungoverned tool path.",
            "Review tools individually. A server is trusted, its tools are not: approve read tools "
            "broadly, and gate write or payment tools behind approval.",
            "Grant the least an agent needs, per tool, with rate limits. Avoid server-wide grants "
            "for servers with write tools.",
            "Re-review when a tool's definition changes. The gateway flags changed tools and refuses "
            "them until reviewed, which guards against rug-pull updates.",
            "Treat tool descriptions and tool output as untrusted input. They can carry prompt injection.",
            "Watch refusals in Tool Calls. Repeated refusals for one agent usually mean a missing "
            "grant or a misbehaving agent.",
        ],
        "examples": [
            _ex("Point an MCP client at the gateway",
                "One endpoint per agent: the gateway lists only the tools that agent is granted.",
                MCP_CLIENT, "json"),
            _ex("Call a tool over REST",
                "For engines without an MCP client. The same grants, limits and policies apply.",
                MCP_CALL),
            _ex("A safe grant",
                "Claims Triage Agent → ServiceNow: search_incidents allowed, "
                "100 calls per hour. close_incident is not granted, and create_incident requires human approval."),
        ],
        "related": [("Server Registry", "mcp_gateway:servers"), ("Tool Catalog", "mcp_gateway:tools"),
                    ("Tool Calls", "mcp_gateway:calls"), ("Policies", "policies:list")],
    },
    "mcp_gateway:tools": {
        "title": "Tool Catalog",
        "summary": "Every tool every registered server offers, and the review decision on each. "
        "Only enabled tools are callable through the gateway.",
        "practices": [
            "Review pending tools first. Until reviewed, they are invisible to agents.",
            "Rate the risk by effect, not by name: anything that writes, sends, pays or deletes is high.",
            "Set per-tool limits on expensive or side-effecting tools.",
            "Tools marked \"changed since review\" need a fresh look: compare the new description and schema.",
        ],
        "examples": [
            _ex("Risk ratings", "Low: search, lookup, list. Moderate: create draft, open ticket. "
                "High: send email, issue refund, freeze account, run a shell command."),
        ],
        "related": [("Server Registry", "mcp_gateway:servers"), ("Tool Calls", "mcp_gateway:calls")],
    },
    "a2a": {
        "title": "A2A Gateway",
        "summary": "Agents hand work to other agents through the gateway. It checks that a contract "
        "allows the pair, the skill is enabled, and the message is no more sensitive than the "
        "recipient is cleared for.",
        "practices": [
            "Publish an agent for A2A only when another agent needs it. Every published agent is attack surface.",
            "Default to restricted access, with explicit contracts. Open access suits only low-risk, "
            "internal-data agents such as a knowledge assistant.",
            "Write contracts per pair, listing the allowed skills, a data-classification ceiling, "
            "a rate limit and an expiry date.",
            "Review skills, not just agents. A new or changed skill is refused until it is reviewed.",
            "Approve external agents only after a security review, and keep their clearance below "
            "your restricted data.",
            "Always declare data_classification on sends. The gateway scans for personal data and "
            "can only raise a classification, never lower it.",
            "Pass parent_task_id on onward hand-offs so multi-hop chains stay traceable and hop limits apply.",
        ],
        "examples": [
            _ex("Hand work to another agent", "Over REST. JSON-RPC message/send to the agent's address works too.",
                A2A_SEND),
            _ex("A tight contract",
                "Customer Support → Claims Triage: skills claim-status only, ceiling Confidential, "
                "120 messages/hour, expires in 90 days, purpose \"answer customer claim queries\"."),
            _ex("Discover agents",
                "GET {api}/a2a/ lists published agents. Each serves its card at "
                "{api}/a2a/<address>/.well-known/agent-card.json."),
        ],
        "related": [("Agent Registry", "a2a:agents"), ("Skill Catalog", "a2a:skills"),
                    ("Contracts", "a2a:contracts"), ("Communications", "a2a:tasks")],
    },
    "a2a:skills": {
        "title": "A2A Skill Catalog",
        "summary": "Every skill every published agent advertises, and governance's decision on it. "
        "Download any skill as a SKILL.md for developers of calling agents.",
        "practices": [
            "Clear \"Awaiting review\" first. Callers are refused until a skill is enabled.",
            "Rate skill risk by what the skill does with the data it is given, not by the agent's overall risk.",
            "Disable rather than delete skills you do not want called. The decision stays on record.",
            "Hand SKILL.md files to the teams building calling agents: they show the address, the "
            "skill id and an exact call.",
        ],
        "examples": [
            _ex("Share the catalog", "Filter to Enabled, then use \"Download SKILL.md files\" for a zip "
                "with one folder per skill: skills/<agent>/<skill>/SKILL.md."),
        ],
        "related": [("Contracts", "a2a:contracts"), ("Agent Registry", "a2a:agents")],
    },
    "a2a:contracts": {
        "title": "A2A Contracts",
        "summary": "The communication graph as governance granted it: which agent may send to which, "
        "which skills, how sensitive a payload and how often.",
        "practices": [
            "One contract per sender and recipient pair. Never grant all skills by default.",
            "Set the ceiling to the lowest classification the work needs.",
            "Always set an expiry, and renew deliberately.",
            "Revoke contracts for retired agents straight away. An unused contract is a standing permission.",
        ],
        "examples": [
            _ex("Partner agent", "Underwriting Copilot → external reinsurer agent: skill quote-capacity, "
                "ceiling Internal (no customer data), 30 messages/hour, expires at renewal."),
        ],
        "related": [("Communications", "a2a:tasks"), ("Skill Catalog", "a2a:skills")],
    },
    "a2a:tasks": {
        "title": "A2A Communications",
        "summary": "Every hand-off between agents: delivered, refused or failed, with the reason and the hop trail.",
        "practices": [
            "Read refusals as policy working, then decide: missing contract, unreviewed skill, "
            "or an agent doing something it should not.",
            "Watch hop counts. Long chains are hard to reason about and multiply cost.",
            "Personal data flagged on a message bound for an external agent deserves a look, even when delivered.",
        ],
        "examples": [],
        "related": [("Contracts", "a2a:contracts"), ("Control Plane", "controlplane:overview")],
    },

    # ---- Policy and governance ------------------------------------------------
    "policies": {
        "title": "Control Policies",
        "summary": "Typed rules the gateways enforce in the request path: model approval, providers, "
        "data classification, autonomy, cost, tools, human approval and agent communication. "
        "Effects are Deny, Require Approval or Warn.",
        "practices": [
            "Start new policies on Warn. Watch the findings for a week, then move to Deny or Require Approval.",
            "Prefer Require Approval over Deny for actions with business value, such as refunds or payouts.",
            "Express \"allowed\" as an allow-list inside the policy. There is no Allow effect, which "
            "avoids precedence puzzles.",
            "Use priority deliberately. Lower numbers are evaluated first, so keep a gap between priorities "
            "to insert rules later.",
            "Give every policy a reference to the regulation or standard it implements. Auditors will ask.",
            "Acknowledge findings with a reason. Unacknowledged findings are your open governance debt.",
            "Let engines ask before acting: call policy/check before a side-effecting step.",
        ],
        "examples": [
            _ex("Ask before acting", "An engine checks a high-risk action before taking it.", POLICY_CHECK),
            _ex("Restricted data stays on-prem",
                "Type: Data Classification. Effect: Deny. Restricted data may only go to approved "
                "providers on the internal cluster."),
            _ex("Refunds need a human",
                "Type: Human Approval. Effect: Require Approval. Tool issue_refund above 100 EUR, "
                "all environments."),
        ],
        "related": [("Approvals", "approvals:centre"), ("Risks", "risks:list"), ("AI Gateway", "gateway:overview")],
    },
    "approvals": {
        "title": "Approvals",
        "summary": "Human decisions the platform asked for: actions held by a Require Approval policy, "
        "and requests matched by approval rules.",
        "practices": [
            "Route approvals to the business owner of the action, not only to the platform team.",
            "Decide with the context shown: agent, data classification and estimated cost.",
            "Write a reason on every decision. It is part of the audit trail.",
            "Set expiries on approval rules so held work does not wait forever.",
        ],
        "examples": [],
        "related": [("Policies", "policies:list"), ("Audit Trail", "audit:list")],
    },
    "risks": {
        "title": "Risk Register",
        "summary": "AI risks against agents, models and providers, scored on likelihood and impact, "
        "with owners and mitigations.",
        "practices": [
            "Give every risk an owner and a review date.",
            "Link each risk to the control (policy, guardrail, approval) that mitigates it.",
            "Re-score after an incident or a model change, not just at review time.",
        ],
        "examples": [],
        "related": [("Risk Matrix", "risks:matrix"), ("Policies", "policies:list")],
    },
    "evaluations": {
        "title": "Evaluations",
        "summary": "Test suites run through the real gateway, so a result reflects what production would do, refusals included.",
        "practices": [
            "Evaluate before promoting an agent or switching its model.",
            "Pin the model in model-targeted suites. Agent suites should use routing, as production does.",
            "Include refusal cases: prompts that policy or guardrails must stop.",
        ],
        "examples": [],
        "related": [("Evaluation Runs", "evaluations:runs"), ("AI Sandbox", "studio:sandbox")],
    },
    "classification": {
        "title": "Risk Classification (S2)",
        "summary": "Each AI system is classified for risk before it is built further. "
        "The class sets the gates and controls it must pass.",
        "practices": [
            "Classify on intended use and data, not on the technology.",
            "Have a second person countersign high-risk classifications.",
            "Reclassify when the purpose, autonomy or data changes.",
        ],
        "examples": [],
        "related": [("Deployment Authorizations", "lifecycle:authorizations")],
    },
    "lifecycle": {
        "title": "Authorizations, Changes and Exceptions",
        "summary": "Gate G3 authorizes deployment, G4 governs changes to an authorized system, "
        "and M17 records time-boxed exceptions.",
        "practices": [
            "Attach conditions to authorizations, and mark them met with evidence.",
            "Route material changes (model, autonomy, data) through G4 before they ship.",
            "Keep exceptions short and owned. An expired exception is a finding.",
        ],
        "examples": [],
        "related": [("Control Plane", "controlplane:overview"), ("Compliance", "compliance:overview")],
    },
    "controlplane": {
        "title": "Control Plane",
        "summary": "Verifies that what agents actually do matches what was authorized, and records drift where it does not.",
        "practices": [
            "Resolve drift by fixing the system or authorizing the change. Never by silencing the check.",
            "Investigate communication outside contract boundaries first. It is the highest-signal drift.",
        ],
        "examples": [],
        "related": [("Drift", "controlplane:drift"), ("Changes", "lifecycle:changes")],
    },
    "intake": {
        "title": "Use-Case Intake (G1)",
        "summary": "New AI ideas enter here and pass Gate G1 before anyone builds.",
        "practices": [
            "Describe the business outcome and the data involved, not the model.",
            "Name a business owner at intake. Unowned use cases stall at every later gate.",
        ],
        "examples": [],
        "related": [("Solution Advisor", "studio:list"), ("Risk Classification", "classification:register")],
    },
    "compliance": {
        "title": "Compliance and Evidence",
        "summary": "Evidence packs per system: controls, decisions and the audit trail, ready for an auditor.",
        "practices": [
            "Generate a pack before every audit or major release, and keep it with the release.",
            "Attest scopes on a schedule, not just before audits.",
        ],
        "examples": [],
        "related": [("Audit Trail", "audit:list")],
    },

    # ---- Landscape, observability, FinOps --------------------------------------
    "inventory": {
        "title": "AI Landscape",
        "summary": "The registry of applications, agents, models and providers. Everything the gateways "
        "enforce is resolved against it.",
        "practices": [
            "Register an agent before it makes its first call, and bind its API key to it.",
            "Every agent needs an owner, a team, a data classification and an autonomy level.",
            "Approve models and providers explicitly. Routing only uses approved ones by default.",
            "Use the kill switch (suspend) on an agent before investigating an incident.",
        ],
        "examples": [],
        "related": [("Onboarding Hub", "onboarding:hub"), ("Executions", "executions:list")],
    },
    "executions": {
        "title": "Executions",
        "summary": "Every agent run, from the gateways or reported by external engines, with timeline, tokens and cost.",
        "practices": [
            "Report runs from engines that do not use the gateway, so the estate stays complete.",
            "Send a trace_id to join model calls, tool calls and hand-offs into one story.",
            "Sort by cost or duration to find outliers before the budget does.",
        ],
        "examples": [
            _ex("Report a run from your engine", "",
                f"""curl -X POST {{api}}/executions/ \\
  {_KEY} \\
  -H "Content-Type: application/json" \\
  -d '{{"execution_id": "run-7f3a", "agent": "claims-triage-agent",
       "status": "completed", "input_tokens": 1820, "output_tokens": 240}}'"""),
        ],
        "related": [("Live Monitor", "executions:live"), ("Routing Decisions", "routing:decisions")],
    },
    "costs": {
        "title": "AI FinOps",
        "summary": "Who spends what on AI, against budgets, with alerts and optimization recommendations.",
        "practices": [
            "Budget per team and per critical agent, not just per organization.",
            "Alert at 80% and act at 100%. A gateway budget can refuse calls when one is exhausted.",
            "Review optimization recommendations monthly: model downgrades and routing changes save the most.",
        ],
        "examples": [],
        "related": [("Budgets", "costs:budgets"), ("Model Routing", "routing:list")],
    },
    "dashboard": {
        "title": "Dashboard",
        "summary": "The estate at a glance: activity, spend, risk and what needs attention.",
        "practices": [
            "Start from Needs Attention: ungated high-risk agents, open drift and pending approvals.",
            "Use the Control Tower view to follow a request through gateways and controls.",
        ],
        "examples": [],
        "related": [("Control Tower", "dashboard:control_tower")],
    },
    "studio": {
        "title": "AI Studio",
        "summary": "Shape a use case into a governed solution, and try models safely in the sandbox.",
        "practices": [
            "Try prompts in the AI Sandbox. Calls go through the gateway with sandbox budgets and guardrails.",
            "Compare two or three models side by side before choosing one for a routing policy.",
            "Never paste production customer data into the sandbox.",
        ],
        "examples": [],
        "related": [("Use-Case Intake", "intake:list"), ("Model Routing", "routing:list")],
    },
    "onboarding": {
        "title": "Onboarding",
        "summary": "Get an estate under governance: register assets, connect gateways and set first policies.",
        "practices": [
            "Onboard in this order: providers, models, agents, then routing policies and control policies.",
            "Move one agent's traffic to the gateway end to end before migrating the rest.",
        ],
        "examples": [],
        "related": [("AI Landscape", "inventory:index")],
    },
    "audit": {
        "title": "Audit Trail",
        "summary": "A tamper-evident record of every change and decision on the platform.",
        "practices": [
            "Export the trail for an audit period rather than screenshotting entries.",
            "Verify the chain regularly with manage.py verify_audit_log.",
        ],
        "examples": [],
        "related": [("Compliance", "compliance:overview")],
    },
    "access": {
        "title": "Access and Integrations",
        "summary": "Members, teams, service accounts and API keys.",
        "practices": [
            "One service account per external system, and one key per environment.",
            "Bind keys to a single agent where possible, so the key cannot act as another.",
            "Rotate keys on a schedule, and revoke on staff or system changes.",
        ],
        "examples": [],
        "related": [("Audit Trail", "audit:list")],
    },
}
# The gateway's own message log shares its overview's help.
HELP["gateway:messages"] = HELP["gateway"]
HELP["a2a:skill_detail"] = HELP["a2a:skills"]
for _name in ("create", "edit"):
    HELP[f"routing:{_name}"] = HELP["routing"]


def help_for(app_name: str, url_name: str) -> dict | None:
    return HELP.get(f"{app_name}:{url_name}") or HELP.get(app_name)
