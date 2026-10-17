"""Seed a realistic demo organization (section 37).

Idempotent: re-running updates the existing demo records rather than creating
duplicates. Everything is deterministic so screenshots and tests stay stable.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.roles import Role
from apps.agents.models import AgentFramework, AIAgent, AutonomyLevel
from apps.api.models import APIKey, ServiceAccount
from apps.applications_registry.models import AIApplication
from apps.core.constants import (
    Criticality,
    DataClassification,
    Environment,
    HealthStatus,
    LifecycleStatus,
    RiskLevel,
)
from apps.models_registry.models import AIModel, ModelType
from apps.organizations.models import Organization, OrganizationMembership, Team, TeamMembership
from apps.providers.models import AIProvider, AuthenticationMethod, ProviderType

User = get_user_model()

DEFAULT_PASSWORD = "ControlTower!2026"
ORGANIZATION_NAME = "Northwind Financial Group"

USERS = [
    ("ava.mitchell@northwind.example", "Ava", "Mitchell", "Chief AI Officer", Role.ORG_ADMIN),
    ("daniel.okafor@northwind.example", "Daniel", "Okafor", "AI Platform Lead", Role.PLATFORM_ADMIN),
    ("priya.raman@northwind.example", "Priya", "Raman", "Senior AI Engineer", Role.AI_DEVELOPER),
    ("marcus.lee@northwind.example", "Marcus", "Lee", "AI Engineer", Role.AI_DEVELOPER),
    ("sofia.hernandez@northwind.example", "Sofia", "Hernandez", "AI Governance Manager", Role.GOVERNANCE_MANAGER),
    ("tom.becker@northwind.example", "Tom", "Becker", "Operational Risk Manager", Role.RISK_MANAGER),
    ("chen.wei@northwind.example", "Chen", "Wei", "Head of Customer Operations", Role.APPROVER),
    ("nadia.faris@northwind.example", "Nadia", "Faris", "Internal Auditor", Role.AUDITOR),
]

TEAMS = [
    ("AI Platform", "Technology", "daniel.okafor@northwind.example"),
    ("Customer Experience", "Operations", "chen.wei@northwind.example"),
    ("Risk & Compliance", "Governance", "sofia.hernandez@northwind.example"),
    ("Data Science", "Technology", "priya.raman@northwind.example"),
]

PROVIDERS = [
    {
        "name": "Azure OpenAI (West Europe)",
        "provider_type": ProviderType.AZURE_OPENAI,
        "adapter_key": "mock",
        "region": "westeurope",
        "endpoint": "https://northwind-weu.openai.azure.example",
        "authentication_method": AuthenticationMethod.AZURE_AD,
        "credential_ref": "AZURE_OPENAI_API_KEY",
        "health_status": HealthStatus.HEALTHY,
        "is_approved": True,
    },
    {
        "name": "OpenAI Platform",
        "provider_type": ProviderType.OPENAI,
        "adapter_key": "mock",
        "region": "us-east",
        "endpoint": "https://api.openai.example/v1",
        "authentication_method": AuthenticationMethod.API_KEY,
        "credential_ref": "OPENAI_API_KEY",
        "health_status": HealthStatus.HEALTHY,
        "is_approved": True,
    },
    {
        "name": "AWS Bedrock (Frankfurt)",
        "provider_type": ProviderType.AWS_BEDROCK,
        "adapter_key": "mock",
        "region": "eu-central-1",
        "authentication_method": AuthenticationMethod.IAM_ROLE,
        "credential_ref": "AWS_BEDROCK_ACCESS_KEY",
        "health_status": HealthStatus.DEGRADED,
        "is_approved": True,
    },
    {
        "name": "Internal Inference Cluster",
        "provider_type": ProviderType.INTERNAL_ML,
        "adapter_key": "generic_rest",
        "region": "on-prem-dc1",
        "endpoint": "https://inference.internal.northwind.example",
        "authentication_method": AuthenticationMethod.MTLS,
        "credential_ref": "",
        "health_status": HealthStatus.HEALTHY,
        "is_approved": True,
    },
    {
        "name": "Gemini Evaluation Sandbox",
        "provider_type": ProviderType.GOOGLE_GEMINI,
        "adapter_key": "mock",
        "region": "us-central1",
        "authentication_method": AuthenticationMethod.SERVICE_ACCOUNT,
        "credential_ref": "GOOGLE_API_KEY",
        "health_status": HealthStatus.UNHEALTHY,
        "is_approved": False,
        "status": LifecycleStatus.DRAFT,
    },
]

CHAT = ["supports_chat", "supports_streaming", "supports_tool_calling"]

MODELS = [
    ("Azure OpenAI (West Europe)", "GPT-4o", "gpt-4o", "gpt", ModelType.CHAT, 128000, "0.005", "0.015", True, RiskLevel.MODERATE),
    ("Azure OpenAI (West Europe)", "GPT-4o mini", "gpt-4o-mini", "gpt", ModelType.CHAT, 128000, "0.00015", "0.0006", True, RiskLevel.LOW),
    ("Azure OpenAI (West Europe)", "Text Embedding 3 Large", "text-embedding-3-large", "embedding", ModelType.EMBEDDING, 8191, "0.00013", "0", True, RiskLevel.LOW),
    ("OpenAI Platform", "GPT-4.1", "gpt-4.1", "gpt", ModelType.CHAT, 1000000, "0.002", "0.008", True, RiskLevel.MODERATE),
    ("OpenAI Platform", "o4-mini (reasoning)", "o4-mini", "o-series", ModelType.CHAT, 200000, "0.0011", "0.0044", False, RiskLevel.HIGH),
    ("AWS Bedrock (Frankfurt)", "Claude Sonnet", "anthropic.claude-sonnet", "claude", ModelType.CHAT, 200000, "0.003", "0.015", True, RiskLevel.MODERATE),
    ("AWS Bedrock (Frankfurt)", "Titan Embeddings", "amazon.titan-embed-text", "titan", ModelType.EMBEDDING, 8192, "0.0001", "0", True, RiskLevel.LOW),
    ("Internal Inference Cluster", "Northwind Llama 3 70B", "nw-llama3-70b", "llama", ModelType.CHAT, 32000, "0.0004", "0.0004", True, RiskLevel.LOW),
    ("Internal Inference Cluster", "Fraud Classifier v3", "nw-fraud-clf-v3", "internal", ModelType.CLASSIFICATION, 4096, "0.00002", "0", True, RiskLevel.HIGH),
    ("Gemini Evaluation Sandbox", "Gemini 2.5 Pro", "gemini-2.5-pro", "gemini", ModelType.CHAT, 1000000, "0.00125", "0.005", False, RiskLevel.MODERATE),
]

AGENTS = [
    {
        "name": "Customer Support Agent", "framework": AgentFramework.LANGGRAPH,
        "provider": "Azure OpenAI (West Europe)", "model": "GPT-4o", "team": "Customer Experience",
        "owner": "chen.wei@northwind.example", "environment": Environment.PRODUCTION,
        "risk": RiskLevel.MODERATE, "autonomy": AutonomyLevel.EXECUTE_WITH_APPROVAL,
        "approval": True, "allowed": ["crm_lookup", "knowledge_base", "web_search"],
        "restricted": ["send_email", "issue_refund"], "sources": ["Support Knowledge Base", "CRM"],
        "classification": DataClassification.CONFIDENTIAL,
        "purpose": "Resolves tier-1 customer queries and drafts responses for agent review.",
    },
    {
        "name": "Claims Triage Agent", "framework": AgentFramework.CREWAI,
        "provider": "AWS Bedrock (Frankfurt)", "model": "Claude Sonnet", "team": "Customer Experience",
        "owner": "marcus.lee@northwind.example", "environment": Environment.PRODUCTION,
        "risk": RiskLevel.HIGH, "autonomy": AutonomyLevel.EXECUTE_WITHIN_BOUNDARIES,
        "approval": True, "allowed": ["claims_db", "policy_lookup"],
        "restricted": ["approve_payout"], "sources": ["Claims Database", "Policy Documents"],
        "classification": DataClassification.RESTRICTED,
        "purpose": "Classifies inbound claims by severity and routes them to the right adjuster.",
    },
    {
        "name": "Fraud Detection Agent", "framework": AgentFramework.CUSTOM_PYTHON,
        "provider": "Internal Inference Cluster", "model": "Fraud Classifier v3", "team": "Risk & Compliance",
        "owner": "tom.becker@northwind.example", "environment": Environment.PRODUCTION,
        "risk": RiskLevel.CRITICAL, "autonomy": AutonomyLevel.AUTONOMOUS,
        # Deliberately ungated: surfaces in the Needs Attention panel.
        "approval": False, "allowed": ["transaction_db", "sanctions_list", "freeze_account"],
        "restricted": [], "sources": ["Transaction Store", "Sanctions Register"],
        "classification": DataClassification.RESTRICTED,
        "purpose": "Scores transactions in real time and freezes accounts above the risk threshold.",
    },
    {
        "name": "Market Research Agent", "framework": AgentFramework.OPENAI_AGENTS,
        "provider": "OpenAI Platform", "model": "GPT-4.1", "team": "Data Science",
        "owner": "priya.raman@northwind.example", "environment": Environment.STAGING,
        "risk": RiskLevel.LOW, "autonomy": AutonomyLevel.RECOMMEND,
        "approval": False, "allowed": ["web_search", "internal_documents"],
        "restricted": ["send_email"], "sources": ["Research Repository"],
        "classification": DataClassification.INTERNAL,
        "purpose": "Compiles competitor and market briefings for the strategy team.",
    },
    {
        "name": "Regulatory Change Monitor", "framework": AgentFramework.LANGGRAPH,
        "provider": "AWS Bedrock (Frankfurt)", "model": "Claude Sonnet", "team": "Risk & Compliance",
        "owner": "sofia.hernandez@northwind.example", "environment": Environment.PRODUCTION,
        "risk": RiskLevel.MODERATE, "autonomy": AutonomyLevel.ADVISORY,
        "approval": False, "allowed": ["regulatory_feed", "internal_documents"],
        "restricted": [], "sources": ["Regulatory Feeds", "Policy Library"],
        "classification": DataClassification.INTERNAL,
        "purpose": "Tracks regulatory publications and flags changes affecting existing controls.",
    },
    {
        "name": "Internal Knowledge Assistant", "framework": AgentFramework.AUTOGEN,
        "provider": "Internal Inference Cluster", "model": "Northwind Llama 3 70B", "team": "AI Platform",
        "owner": "daniel.okafor@northwind.example", "environment": Environment.PRODUCTION,
        "risk": RiskLevel.LOW, "autonomy": AutonomyLevel.ADVISORY,
        "approval": False, "allowed": ["internal_documents", "confluence"],
        "restricted": ["crm_lookup"], "sources": ["Intranet", "Engineering Wiki"],
        "classification": DataClassification.INTERNAL,
        "purpose": "Answers employee questions from internal documentation.",
    },
    {
        "name": "Underwriting Copilot", "framework": AgentFramework.SEMANTIC_KERNEL,
        "provider": "Azure OpenAI (West Europe)", "model": "GPT-4o", "team": "Data Science",
        "owner": "priya.raman@northwind.example", "environment": Environment.TEST,
        "risk": RiskLevel.HIGH, "autonomy": AutonomyLevel.EXECUTE_WITH_APPROVAL,
        "approval": True, "allowed": ["actuarial_tables", "policy_lookup"],
        "restricted": ["bind_policy"], "sources": ["Actuarial Models"],
        "classification": DataClassification.CONFIDENTIAL,
        "purpose": "Assists underwriters with risk pricing recommendations.",
        "status": LifecycleStatus.PENDING_REVIEW,
    },
    {
        "name": "Onboarding Document Extractor", "framework": AgentFramework.CUSTOM_PYTHON,
        "provider": "Azure OpenAI (West Europe)", "model": "GPT-4o mini", "team": "Customer Experience",
        "owner": "marcus.lee@northwind.example", "environment": Environment.DEVELOPMENT,
        "risk": RiskLevel.MODERATE, "autonomy": AutonomyLevel.RECOMMEND,
        "approval": False, "allowed": ["document_store", "ocr"],
        "restricted": [], "sources": ["Onboarding Documents"],
        "classification": DataClassification.CONFIDENTIAL,
        "purpose": "Extracts structured data from customer onboarding paperwork.",
    },
]

APPLICATIONS = [
    {
        "name": "Customer Support Portal", "team": "Customer Experience",
        "business_owner": "chen.wei@northwind.example", "technical_owner": "marcus.lee@northwind.example",
        "department": "Operations", "environment": Environment.PRODUCTION,
        "criticality": Criticality.HIGH, "risk": RiskLevel.MODERATE,
        "classification": DataClassification.CONFIDENTIAL,
        "agents": ["Customer Support Agent", "Onboarding Document Extractor"],
        "purpose": "Customer-facing support experience with AI-assisted response drafting.",
        "production_days_ago": 240, "review_in_days": -20,
    },
    {
        "name": "Claims Processing Platform", "team": "Customer Experience",
        "business_owner": "chen.wei@northwind.example", "technical_owner": "daniel.okafor@northwind.example",
        "department": "Claims", "environment": Environment.PRODUCTION,
        "criticality": Criticality.CRITICAL, "risk": RiskLevel.HIGH,
        "classification": DataClassification.RESTRICTED,
        "agents": ["Claims Triage Agent"],
        "purpose": "End-to-end claims intake, triage and adjuster assignment.",
        "production_days_ago": 420, "review_in_days": 45,
    },
    {
        "name": "Fraud Operations Console", "team": "Risk & Compliance",
        "business_owner": "tom.becker@northwind.example", "technical_owner": "daniel.okafor@northwind.example",
        "department": "Financial Crime", "environment": Environment.PRODUCTION,
        "criticality": Criticality.CRITICAL, "risk": RiskLevel.CRITICAL,
        "classification": DataClassification.RESTRICTED,
        "agents": ["Fraud Detection Agent"],
        "purpose": "Real-time transaction monitoring and account intervention.",
        "production_days_ago": 150, "review_in_days": -5,
    },
    {
        "name": "Strategy Research Workspace", "team": "Data Science",
        "business_owner": "ava.mitchell@northwind.example", "technical_owner": "priya.raman@northwind.example",
        "department": "Strategy", "environment": Environment.STAGING,
        "criticality": Criticality.MEDIUM, "risk": RiskLevel.LOW,
        "classification": DataClassification.INTERNAL,
        "agents": ["Market Research Agent", "Underwriting Copilot"],
        "purpose": "Competitive intelligence and market analysis for the strategy function.",
        "production_days_ago": None, "review_in_days": 90,
    },
    {
        "name": "Employee Knowledge Hub", "team": "AI Platform",
        "business_owner": "daniel.okafor@northwind.example", "technical_owner": "daniel.okafor@northwind.example",
        "department": "Technology", "environment": Environment.PRODUCTION,
        "criticality": Criticality.MEDIUM, "risk": RiskLevel.LOW,
        "classification": DataClassification.INTERNAL,
        "agents": ["Internal Knowledge Assistant", "Regulatory Change Monitor"],
        "purpose": "Self-service answers over internal documentation for all employees.",
        "production_days_ago": 300, "review_in_days": 120,
    },
]


class Command(BaseCommand):
    help = "Seed a demo organization with realistic AI inventory."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", default=DEFAULT_PASSWORD, help="Password for the seeded demo users."
        )
        parser.add_argument(
            "--superuser",
            default="admin@northwind.example",
            help="Email of a superuser to create alongside the demo data.",
        )
        parser.add_argument(
            "--executions", type=int, default=500, help="Mock executions to generate (0 to skip)."
        )

    def _say(self, message: str = "", style=None) -> None:
        if self.verbosity:
            self.stdout.write(style(message) if style else message)

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(20260909)
        self.verbosity = options.get("verbosity", 1)
        password = options["password"]

        organization = self._organization()
        users = self._users(organization, password)
        teams = self._teams(organization, users)
        providers = self._providers(organization)
        models = self._models(organization, providers)
        agents = self._agents(organization, providers, models, teams, users)
        applications = self._applications(organization, agents, models, providers, teams, users)
        self._service_account(organization)
        superuser_created = self._superuser(options["superuser"], password)
        from apps.a2a.demo import seed_a2a_traffic
        from apps.controlplane.signals import deferred
        from apps.core.demo_irgf import seed_control_plane, seed_gates, seed_irgf, seed_systems

        # The Control Plane verifies each MCP call as it is recorded. The demo
        # backdates its traffic, so verification waits and runs at the end.
        with deferred():
            telemetry = self._executions(organization, options["executions"])
            # Model routing: policies per class of work, and the decisions the
            # router made for each agent, with the gateway runs they served.
            from apps.routing.demo import seed_routing

            routing = seed_routing(organization, agents, providers)
            governance = self._governance(
                organization, users, teams, applications, agents, models, providers
            )
            # FinOps: who caused the spend, GitHub Copilot, scoped budgets,
            # alert rules and the first optimization run.
            from apps.costs.demo import seed_finops

            finops = seed_finops(organization, teams, agents, users)
            # MCP traffic runs after governance so the gateway judges it against
            # the seeded policies: its refusals are the policies' own decisions.
            mcp = self._mcp(organization, agents, teams, users)
            # Agent-to-agent: the published agents, their reviewed skills and the
            # contracts between them -- before Gate G3 authorizes the agents that
            # hold them, so the contracts are part of what G3 signs.
            a2a = self._a2a(organization, agents, teams, users)

            # IRGF: S2 classifications, identities and Agent Cards (Phase 1), then
            # G3 authorizations, a G4 change and an M17 exception (Phase 2), then
            # the Control Plane over all of it (Phase 3).
            irgf = seed_irgf(organization, agents, applications, users)
            gates = seed_gates(organization, agents, users)
            systems = seed_systems(organization, agents, applications, users)
            a2a["tasks"] = seed_a2a_traffic(organization, agents, users)
            plane = seed_control_plane(organization, agents, users)

        self._say()
        self._say(f"Seeded '{organization.name}'", self.style.SUCCESS)
        self._say(
            f"  {len(users)} users, {len(teams)} teams, {len(providers)} providers, "
            f"{len(models)} models, {len(agents)} agents, {len(applications)} applications"
        )
        if telemetry:
            self._say(f"  {telemetry.executions} executions, {telemetry.events} execution events")
        self._say(
            f"  {routing['policies']} routing policies, {routing['decisions']} routing decisions "
            f"({routing['fallbacks']} served by a fallback, {routing['exhausted']} with no route)"
        )
        self._say(
            f"  {governance['risks']} risks, {governance['approvals']} approval requests, "
            f"{governance['rules']} approval rules"
        )
        self._say(
            f"  {governance['budgets']} budgets, {governance['alerts']} budget alerts, "
            f"1 pricing version"
        )
        self._say(
            f"  FinOps: {finops['seats']} GitHub Copilot seats, {finops['budgets']} scoped budgets, "
            f"{finops['rules']} cost alert rules, {finops['recommendations']} optimization recommendations"
        )
        self._say(
            f"  {governance['policies']} control policies, "
            f"{governance['violations']} policy findings"
        )
        self._say(
            f"  {mcp['servers']} MCP servers, {mcp['tools']} MCP tools, "
            f"{mcp['calls']} MCP tool calls"
        )
        self._say(
            f"  {a2a['agents']} A2A agents, {a2a['skills']} A2A skills, {a2a['contracts']} A2A contracts, "
            f"{a2a['tasks']} agent-to-agent hand-offs"
        )
        if irgf["classified"]:
            self._say(f"  {irgf['classified']} IRGF risk classifications (stage S2)")
        if gates["authorized"]:
            self._say(f"  {gates['authorized']} agent deployment authorizations in force (IRGF S7, Gate G3)")
        if systems["authorized"]:
            self._say(f"  {systems['authorized']} application authorization(s) in force, with composite authority (IRGF chapter 16)")
        if plane["verified"]:
            self._say(
                f"  {plane['verified']} actions verified (IRGF link 07), {plane['drift']} unresolved drift "
                f"records, {plane['incidents']} incidents (M13)"
            )
        self._say()
        self._say(f"  Sign in: {USERS[0][0]} / {password}")
        if superuser_created:
            self._say(f"  Superuser: {options['superuser']} / {password}")

    # -- Steps --------------------------------------------------------------

    def _organization(self) -> Organization:
        organization, _ = Organization.objects.update_or_create(
            name=ORGANIZATION_NAME,
            defaults={
                "description": "Demo organization for the AI Control Tower.",
                "industry": "Financial Services",
                "status": LifecycleStatus.ACTIVE,
            },
        )
        return organization

    def _users(self, organization: Organization, password: str) -> dict:
        users = {}
        for index, (email, first, last, title, role) in enumerate(USERS):
            user, created = User.objects.update_or_create(
                email=email,
                defaults={"first_name": first, "last_name": last, "job_title": title, "is_active": True},
            )
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])

            OrganizationMembership.objects.update_or_create(
                organization=organization, user=user, defaults={"role": role, "is_default": index == 0}
            )
            users[email] = user
        return users

    def _teams(self, organization: Organization, users: dict) -> dict:
        teams = {}
        for name, department, lead_email in TEAMS:
            team, _ = Team.objects.update_or_create(
                organization=organization,
                name=name,
                defaults={"department": department, "lead": users.get(lead_email), "status": LifecycleStatus.ACTIVE},
            )
            teams[name] = team

        # Spread everyone across teams so membership is not empty.
        emails = list(users)
        for index, email in enumerate(emails):
            team = teams[TEAMS[index % len(TEAMS)][0]]
            TeamMembership.objects.get_or_create(team=team, user=users[email])
        return teams

    def _providers(self, organization: Organization) -> dict:
        providers = {}
        now = timezone.now()
        for spec in PROVIDERS:
            provider, _ = AIProvider.objects.update_or_create(
                organization=organization,
                name=spec["name"],
                defaults={
                    "provider_type": spec["provider_type"],
                    "adapter_key": spec["adapter_key"],
                    "region": spec.get("region", ""),
                    "endpoint": spec.get("endpoint", ""),
                    "authentication_method": spec["authentication_method"],
                    "credential_ref": spec.get("credential_ref", ""),
                    "health_status": spec["health_status"],
                    "last_health_check": now - timedelta(minutes=random.randint(2, 55)),
                    "last_health_latency_ms": random.randint(40, 900),
                    "last_health_message": ""
                    if spec["health_status"] == HealthStatus.HEALTHY
                    else "Elevated error rate reported by the last probe.",
                    "is_approved": spec["is_approved"],
                    "status": spec.get("status", LifecycleStatus.ACTIVE),
                },
            )
            # Cache what the bound adapter advertises.
            try:
                provider.supported_capabilities = provider.get_adapter().capability_list()
                provider.save(update_fields=["supported_capabilities"])
            except Exception:
                pass
            providers[spec["name"]] = provider
        return providers

    def _models(self, organization: Organization, providers: dict) -> dict:
        models = {}
        for (
            provider_name, name, model_id, family, model_type,
            context, input_cost, output_cost, approved, risk,
        ) in MODELS:
            capabilities = CHAT if model_type == ModelType.CHAT else ["supports_embeddings"]
            model, _ = AIModel.objects.update_or_create(
                organization=organization,
                provider=providers[provider_name],
                model_id=model_id,
                version="1",
                defaults={
                    "model_name": name,
                    "model_family": family,
                    "model_type": model_type,
                    "context_window": context,
                    "input_cost_per_1k": Decimal(input_cost),
                    "output_cost_per_1k": Decimal(output_cost),
                    "capabilities": capabilities,
                    "region": providers[provider_name].region,
                    "approved_for_use": approved,
                    "risk_classification": risk,
                    "status": LifecycleStatus.ACTIVE,
                },
            )
            models[name] = model
        return models

    def _agents(self, organization, providers, models, teams, users) -> dict:
        agents = {}
        for spec in AGENTS:
            agent, _ = AIAgent.objects.update_or_create(
                organization=organization,
                agent_id=spec["name"].lower().replace(" ", "-"),
                defaults={
                    "name": spec["name"],
                    "business_purpose": spec["purpose"],
                    "description": spec["purpose"],
                    "framework": spec["framework"],
                    "adapter_key": "mock",
                    "provider": providers[spec["provider"]],
                    "primary_model": models[spec["model"]],
                    "team": teams[spec["team"]],
                    "owner": users[spec["owner"]],
                    "environment": spec["environment"],
                    "risk_level": spec["risk"],
                    "autonomy_level": spec["autonomy"],
                    "requires_human_approval": spec["approval"],
                    "allowed_tools": spec["allowed"],
                    "restricted_tools": spec["restricted"],
                    "knowledge_sources": spec["sources"],
                    "data_classification": spec["classification"],
                    "system_prompt_reference": f"git://northwind/prompts/{spec['name'].lower().replace(' ', '-')}.md",
                    "status": spec.get("status", LifecycleStatus.ACTIVE),
                },
            )
            agents[spec["name"]] = agent
        return agents

    def _applications(self, organization, agents, models, providers, teams, users) -> dict:
        applications = {}
        today = timezone.now().date()

        for spec in APPLICATIONS:
            production_date = (
                today - timedelta(days=spec["production_days_ago"])
                if spec["production_days_ago"]
                else None
            )
            application, _ = AIApplication.objects.update_or_create(
                organization=organization,
                name=spec["name"],
                defaults={
                    "description": spec["purpose"],
                    "business_purpose": spec["purpose"],
                    "business_owner": users[spec["business_owner"]],
                    "technical_owner": users[spec["technical_owner"]],
                    "team": teams[spec["team"]],
                    "department": spec["department"],
                    "environment": spec["environment"],
                    "criticality": spec["criticality"],
                    "data_classification": spec["classification"],
                    "risk_level": spec["risk"],
                    "production_date": production_date,
                    "review_date": today + timedelta(days=spec["review_in_days"]),
                    "status": LifecycleStatus.ACTIVE,
                },
            )

            linked_agents = [agents[name] for name in spec["agents"]]
            application.agents.set(linked_agents)
            # Derive the model and provider links from the agents that use them.
            application.ai_models.set({a.primary_model for a in linked_agents if a.primary_model})
            application.providers.set({a.provider for a in linked_agents if a.provider})
            applications[spec["name"]] = application

        return applications

    def _service_account(self, organization: Organization) -> None:
        account, created = ServiceAccount.objects.update_or_create(
            organization=organization,
            name="langgraph-runtime",
            defaults={
                "description": "External LangGraph runtime reporting execution telemetry.",
                "external_system": "LangGraph on Kubernetes",
                "role": Role.AI_DEVELOPER,
            },
        )
        if created or not account.api_keys.exists():
            _, raw_key = APIKey.generate(account, name="seed-demo")
            self._say()
            self._say(f"  Service account API key: {raw_key}", self.style.SUCCESS)
            self._say("  (shown once -- used by external engines to post telemetry)")

    def _executions(self, organization, count: int):
        """Generate mock telemetry, replacing any from a previous seed run."""
        if count <= 0:
            return None

        from apps.executions.generators import MockExecutionGenerator, purge

        purge(organization)
        return MockExecutionGenerator(organization).generate(count)

    def _governance(self, organization, users, teams, applications, agents, models, providers) -> dict:
        """Pricing, budgets, risks and approvals (sections 15-20)."""
        from apps.core import demo_governance as governance

        governance.seed_pricing(organization, models)
        rules = governance.seed_approval_rules(organization)
        policies = governance.seed_policies(organization)
        risks = governance.seed_risks(organization, agents, providers, users)
        approvals = governance.seed_approvals(organization, agents, users)
        # Policy activity runs last so it evaluates against the finished estate.
        violations = governance.seed_policy_activity(organization, agents, providers)
        # Budgets are evaluated last so alerts reflect the seeded spend.
        budgets, alerts = governance.seed_budgets(
            organization, teams, applications, agents, providers, users
        )

        return {
            "rules": len(rules),
            "policies": len(policies),
            "violations": violations,
            "risks": len(risks),
            "approvals": len(approvals),
            "budgets": len(budgets),
            "alerts": alerts,
        }

    def _mcp(self, organization, agents, teams, users) -> dict:
        """MCP servers, reviewed tools, grants and brokered traffic."""
        from apps.mcp_gateway.demo import seed_mcp

        return seed_mcp(organization, agents, teams, users)

    def _a2a(self, organization, agents, teams, users) -> dict:
        """Agents published for A2A, their reviewed skills and the contracts between them."""
        from apps.a2a.demo import seed_a2a

        return seed_a2a(organization, agents, teams, users)

    def _superuser(self, email: str, password: str) -> bool:
        if not email or User.objects.filter(email=email).exists():
            return False
        User.objects.create_superuser(email=email, password=password, first_name="Platform", last_name="Admin")
        return True
