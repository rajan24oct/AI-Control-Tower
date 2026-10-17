"""A demo routing estate for `seed_demo`.

Six policies cover the Northwind agents: reliability first for the
customer-facing agents, EU and on-prem only for confidential underwriting and
onboarding work, lowest cost for research and knowledge, the fraud classifier
pinned to the internal cluster, and estate-wide defaults for chat and
embeddings. Every decision is made by the real RoutingService against the
seeded registry -- so Bedrock's degraded health, the unapproved models and the
cost ceilings shape the plans -- and each served call is recorded as a gateway
execution against its agent and application, the way the gateway records it.

Fallback is the part a fixture would fake. Here a primary on a degraded
provider sometimes fails and the next model in the frozen chain serves, and a
few research requests ask for more context than any approved model offers and
get no route at all.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.core.constants import Environment, HealthStatus
from apps.executions.generators import HOUR_WEIGHTS
from apps.executions.models import Execution, ExecutionStatusChoices
from apps.executions.services import ExecutionService
from apps.models_registry.models import ModelType
from apps.routing.models import RouteOutcome, RoutingDecision, RoutingPolicy, RoutingStrategy
from apps.routing.services import RoutingRequest, RoutingService

#: Seeded gateway runs carry this prefix, so a re-seed replaces them.
EXTERNAL_PREFIX = "gw-demo-"

POLICIES = [
    {
        "name": "Customer-facing chat - reliability first",
        "description": "Customer-facing agents route to the most reliable approved model. "
        "A failed answer in front of a customer costs more than a dearer token.",
        "strategy": RoutingStrategy.RELIABILITY, "priority": 10, "model_type": ModelType.CHAT,
        "environments": [Environment.PRODUCTION],
        "agents": ["Customer Support Agent", "Claims Triage Agent"],
        "required_capabilities": ["supports_tool_calling"],
        "allowed": ["Azure OpenAI (West Europe)", "AWS Bedrock (Frankfurt)", "OpenAI Platform"],
        "fallback_depth": 2,
    },
    {
        "name": "Confidential data - EU and on-prem only",
        "description": "Underwriting and onboarding documents are confidential: they never leave "
        "EU-hosted or internal inference.",
        "strategy": RoutingStrategy.BALANCED, "priority": 20, "model_type": ModelType.CHAT,
        "agents": ["Underwriting Copilot", "Onboarding Document Extractor"],
        "blocked": ["OpenAI Platform"],
        "fallback_depth": 2,
    },
    {
        "name": "Research and knowledge - lowest cost",
        "description": "Internal research and Q&A favour the cheapest model that holds a long "
        "document, under a ceiling on input price.",
        "strategy": RoutingStrategy.COST, "priority": 30, "model_type": ModelType.CHAT,
        "agents": ["Market Research Agent", "Internal Knowledge Assistant", "Regulatory Change Monitor"],
        "min_context_window": 32000, "max_input_cost_per_1k": Decimal("0.003"),
        "fallback_depth": 2,
    },
    {
        "name": "Fraud scoring - internal cluster only",
        "description": "Transaction scoring stays on the internal cluster. No degraded fallbacks: "
        "a slow score is a missed fraud.",
        "strategy": RoutingStrategy.LATENCY, "priority": 10, "model_type": ModelType.CLASSIFICATION,
        "agents": ["Fraud Detection Agent"],
        "allowed": ["Internal Inference Cluster"], "allow_degraded": False,
        "fallback_depth": 0,
    },
    {
        "name": "Embeddings - lowest cost",
        "description": "Retrieval embeddings for every agent: cheapest approved embedding model.",
        "strategy": RoutingStrategy.COST, "priority": 50, "model_type": ModelType.EMBEDDING,
        "fallback_depth": 1,
    },
    {
        "name": "Estate default - balanced",
        "description": "Anything no narrower policy covers: balanced across cost, latency and reliability.",
        "strategy": RoutingStrategy.BALANCED, "priority": 100, "model_type": ModelType.CHAT,
        "fallback_depth": 2,
    },
]

#: Requests per agent over the window: (agent, chat calls, embedding calls).
TRAFFIC = [
    ("Customer Support Agent", 70, 18),
    ("Claims Triage Agent", 46, 0),
    ("Fraud Detection Agent", 40, 0),
    ("Internal Knowledge Assistant", 38, 14),
    ("Regulatory Change Monitor", 20, 0),
    ("Market Research Agent", 18, 0),
    ("Onboarding Document Extractor", 16, 0),
    ("Underwriting Copilot", 12, 0),
]

PROMPTS = {
    "Customer Support Agent": [
        ("Customer asks why their card payment was declined abroad.", "Drafted a reply explaining travel notifications and how to enable them."),
        ("Summarise this complaint thread and suggest next steps.", "Summary: delayed refund, two contacts. Next step: escalate to payments ops."),
        ("Customer wants to change the address on their policy.", "Drafted the address-change steps with the identity check required."),
    ],
    "Claims Triage Agent": [
        ("New claim: water damage to kitchen, photos attached.", "Severity: moderate. Route to property adjuster team B."),
        ("Claim CLM-20931: rear-end collision, no injuries reported.", "Severity: low. Fast-track motor claim, assign desk adjuster."),
    ],
    "Fraud Detection Agent": [
        ("Score transaction TX-88412: 4,900 EUR, new device, new payee.", "Score 0.91. Signals: new device, first payee, velocity."),
        ("Score transaction TX-88533: 42 EUR grocery, usual merchant.", "Score 0.03. No anomalous signals."),
    ],
    "Internal Knowledge Assistant": [
        ("How do I request access to the data warehouse?", "Raise an access request in ServiceNow under 'Data Platform'; your manager approves."),
        ("What is the expense limit for client dinners?", "Up to 85 EUR per head; above that needs director sign-off."),
    ],
    "Regulatory Change Monitor": [
        ("What changed in the latest EBA guidelines on outsourcing?", "Two changes: register of arrangements, and exit-plan testing. Controls OUT-03 and OUT-07 affected."),
    ],
    "Market Research Agent": [
        ("Brief me on competitor pricing moves in SME insurance this quarter.", "Three competitors cut premiums 4-7%; one bundled cyber cover."),
    ],
    "Onboarding Document Extractor": [
        ("Extract fields from this passport scan.", "Name, nationality, document number and expiry extracted; MRZ check passed."),
    ],
    "Underwriting Copilot": [
        ("Price a commercial property risk: warehouse, sprinklered, flood zone 2.", "Indicative rate 0.18%; flood loading applied; refer for survey."),
    ],
}

PRIMARY_ERRORS = [
    "Upstream provider returned HTTP 429 (rate limited).",
    "Model request exceeded the 30s timeout.",
    "Provider returned HTTP 503 (service unavailable).",
]


def _policies(organization, agents, providers) -> list[RoutingPolicy]:
    policies = []
    for spec in POLICIES:
        policy, _ = RoutingPolicy.objects.update_or_create(
            organization=organization,
            name=spec["name"],
            defaults={
                "description": spec["description"],
                "strategy": spec["strategy"],
                "priority": spec["priority"],
                "model_type": spec["model_type"],
                "environments": spec.get("environments", []),
                "required_capabilities": spec.get("required_capabilities", []),
                "min_context_window": spec.get("min_context_window"),
                "max_input_cost_per_1k": spec.get("max_input_cost_per_1k"),
                "allow_degraded_providers": spec.get("allow_degraded", True),
                "fallback_depth": spec["fallback_depth"],
            },
        )
        policy.agents.set([agents[name] for name in spec.get("agents", []) if name in agents])
        policy.allowed_providers.set([providers[name] for name in spec.get("allowed", []) if name in providers])
        policy.blocked_providers.set([providers[name] for name in spec.get("blocked", []) if name in providers])
        policies.append(policy)
    return policies


def _moment(rng, now, days: int):
    """A time in the window, busier on recent days and in working hours."""
    day = int(abs(rng.gauss(0, days / 2.5))) % days
    hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
    stamp = (now - timedelta(days=day)).replace(
        hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0
    )
    return stamp if stamp <= now else now - timedelta(minutes=rng.randint(1, 90))


def _serve(organization, rng, decision, plan, agent, model_type, started_at, index) -> Execution:
    """Walk the frozen chain the way the gateway does, and record the run."""
    chain = list(plan.candidates)
    attempts, served_position = [], 1
    primary = chain[0].model
    # A degraded provider fails often enough to make fallback visible; a
    # healthy one fails rarely. Either way the next entry in the chain serves.
    failure_rate = 0.35 if primary.provider.health_status == HealthStatus.DEGRADED else 0.04
    if len(chain) > 1 and rng.random() < failure_rate:
        attempts.append({
            "position": 1, "model": primary.model_name, "provider": primary.provider.name,
            "ok": False, "latency_ms": rng.randint(800, 30_000), "error": rng.choice(PRIMARY_ERRORS),
            "skipped": "",
        })
        served_position = 2
    served = chain[served_position - 1].model
    latency = rng.randint(180, 900) if model_type == ModelType.EMBEDDING else rng.randint(600, 9_000)
    attempts.append({
        "position": served_position, "model": served.model_name, "provider": served.provider.name,
        "ok": True, "latency_ms": latency, "error": "", "skipped": "",
    })
    total_latency = sum(attempt["latency_ms"] for attempt in attempts)

    if model_type == ModelType.EMBEDDING:
        prompt, completion = "Embed retrieved knowledge-base passages for search.", ""
        input_tokens, output_tokens = rng.randint(400, 6_000), 0
    else:
        prompt, completion = rng.choice(PROMPTS.get(agent.name) or [("Request", "Response")])
        input_tokens, output_tokens = rng.randint(300, 4_500), rng.randint(60, 900)

    execution = ExecutionService.record(
        organization=organization,
        external_id=f"{EXTERNAL_PREFIX}{index:05d}",
        source_adapter="gateway",
        agent=str(agent.pk),
        model=str(served.pk),
        provider=str(served.provider_id),
        trace_id=f"route-{index:05d}",
        status=ExecutionStatusChoices.COMPLETED,
        environment=agent.environment,
        started_at=started_at,
        ended_at=started_at + timedelta(milliseconds=total_latency),
        latency_ms=total_latency,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        metadata={
            "gateway": True,
            "streamed": False,
            "used_fallback": served_position > 1,
            "usage_estimated": False,
            "attempts": attempts,
            "routing_decision": str(decision.pk),
        },
    )
    ExecutionService.append_events(
        execution,
        [
            {"event_type": "PROMPT_CREATED", "sequence": 0, "timestamp": started_at,
             "metadata": {"prompt": prompt}},
            {"event_type": "MODEL_REQUEST_COMPLETED", "sequence": 1,
             "timestamp": started_at + timedelta(milliseconds=total_latency),
             "duration_ms": total_latency, "input_tokens": input_tokens, "output_tokens": output_tokens,
             "model_id": served.model_id, "metadata": {"completion": completion}},
        ],
    )

    decision.execution = execution
    decision.save(update_fields=["execution", "updated_at"])
    detail = (
        f"Primary {primary.model_name} failed ({attempts[0]['error']}); fallback served."
        if served_position > 1 else ""
    )
    RoutingService.report_outcome(
        decision, model=served, attempt=served_position, outcome=RouteOutcome.SERVED, detail=detail
    )
    return execution


def seed_routing(organization, agents, providers, *, days: int = 14) -> dict:
    """Routing policies, and two weeks of decisions with the runs they served."""
    rng = random.Random(20261017)
    now = timezone.now()
    policies = _policies(organization, agents, providers)

    # Replace what a previous seed recorded, not what anyone else did.
    RoutingDecision.objects.filter(organization=organization, policy__in=policies).delete()
    Execution.objects.filter(organization=organization, external_id__startswith=EXTERNAL_PREFIX).delete()

    requests = []
    for name, chat, embeddings in TRAFFIC:
        agent = agents.get(name)
        if agent is None:
            continue
        chat_type = (
            agent.primary_model.model_type if agent.primary_model else ModelType.CHAT
        )
        requests += [(agent, chat_type, None)] * chat
        requests += [(agent, ModelType.EMBEDDING, None)] * embeddings
    # A few research briefs ask for a two-million-token context: nothing approved
    # holds that, so the plan is empty and the decision log says why.
    research = agents.get("Market Research Agent")
    if research is not None:
        requests += [(research, ModelType.CHAT, 2_000_000)] * 3

    stamped = sorted(((_moment(rng, now, days), request) for request in requests), key=lambda item: item[0])
    # The newest call lands minutes ago, so the monitors open on live traffic.
    if stamped:
        stamped[-1] = (now - timedelta(minutes=2), stamped[-1][1])

    served = fallbacks = exhausted = 0
    for index, (moment, (agent, model_type, min_context)) in enumerate(stamped):
        plan, decision = RoutingService.decide(
            organization,
            RoutingRequest(
                model_type=model_type,
                environment=agent.environment,
                agent=agent,
                min_context_window=min_context,
            ),
        )
        if plan.has_route:
            _serve(organization, rng, decision, plan, agent, model_type, moment, index)
            decision.refresh_from_db(fields=["attempt"])
            served += 1
            fallbacks += decision.attempt > 1
        else:
            exhausted += 1
        # Decisions are stamped when made; move each onto its point in history.
        RoutingDecision.objects.filter(pk=decision.pk).update(
            created_at=moment,
            updated_at=moment,
            outcome_reported_at=moment + timedelta(seconds=rng.randint(1, 30)) if plan.has_route else None,
        )

    return {
        "policies": len(policies),
        "decisions": served + exhausted,
        "fallbacks": fallbacks,
        "exhausted": exhausted,
    }
