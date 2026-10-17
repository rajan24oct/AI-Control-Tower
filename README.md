

---

AI Control Tower is a Django platform that gives an organization a single control plane for its AI
estate. It keeps a registry of every provider, model, agent, application and MCP server; watches
every run; and — when you put it in the request path — enforces policy, budgets, routing and tool
access on live model and tool calls, while holding the upstream credentials so applications never do.

![Dashboard](screens/01-dashboard-home.png)

## Contents

- [What's inside](#whats-inside)
- [Architecture](#architecture)
- [Quick start (local)](#quick-start-local)
- [Running with Docker Compose](#running-with-docker-compose)
- [Configuration](#configuration)
- [Demo data, accounts and roles](#demo-data-accounts-and-roles)
- [Product tour](#product-tour)
- [Onboarding your AI estate](#onboarding-your-ai-estate)
- [Using the gateways from your applications](#using-the-gateways-from-your-applications)
- [API at a glance](#api-at-a-glance)
- [Background jobs](#background-jobs)
- [Management commands](#management-commands)
- [Running the tests](#running-the-tests)
- [Project structure](#project-structure)
- [Security model](#security-model)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What's inside

| Module | What it gives you | Where |
|---|---|---|
| **Dashboard & Control Tower** | Executive KPIs for a chosen period, and an end-to-end map of the estate with everything that needs attention | `/`, `/control-tower/` |
| **AI Landscape** | Registries for applications, agents, models and platform providers — owners, risk, environments, pricing, health | `/inventory/` |
| **Onboarding** | A guided hub and in-app create and edit pages for providers, models, agents, applications, routing, MCP servers, control policies, approval rules, risks and budgets — permission-checked and audited, with no trip to the admin | `/onboarding/` |
| **Observability** | Every run with its timeline, tokens, cost and latency; a live monitor pushed over WebSockets | `/executions/` |
| **AI Gateway** | In-path model calls: policy and budget enforcement, cost-aware routing, fallback chains, circuit breakers, streaming | `/gateway/`, `/routing/` |
| **MCP Gateway** | One registry for every MCP server, tool review and re-review, per-agent access grants, and a single governed MCP endpoint for all of them | `/mcp-gateway/` |
| **A2A Gateway** | Agent-to-agent communication, governed: a registry of the agents that take work from others, inside the organization or out, skills reviewed before any agent may ask for them, contracts saying which agent may hand work to which and with how sensitive a payload, hop limits, and one Agent2Agent (A2A) endpoint per agent | `/a2a-gateway/` |
| **AI FinOps** | A drill-down cost explorer (department → team → agent → model → provider, or by individual); GitHub Copilot licence and premium-request spend by team and by individual; budgets by department, team, individual or asset with threshold and forecast alerts, chosen recipients and optional hard stops; cost alert rules for spikes and runaway spend; costed optimization recommendations | `/costs/` |
| **Use-Case Intake · G1** | AI ideas recorded as use cases (IRGF S0–S1) with provisional D1–D5, decided at Gate G1 by tier with IRGF's five outcomes, then registered as draft applications | `/intake/` |
| **Compliance & Evidence** | EU AI Act, ISO/IEC 42001 and NIST AI RMF requirements per system, answered from live platform evidence, with scope decisions, attestations and exportable evidence packs (print to PDF, or a ZIP with JSON and the audit trail) | `/compliance/` |
| **Runtime Guardrails** | Prompt-injection, personal-data, secrets and restricted-topic checks on prompts and completions in the AI and A2A gateways: block, redact or warn by tier, with every hit recorded and masked | admin |
| **Solution Advisor** | Describe an AI use case and get a blueprint: approach, technology stack, build plan mapped to IRGF stages and gates, team, risks, cost estimate and tier controls, written by ChatGPT or the built-in advisor, and proposed at Gate G1 in one click | `/studio/` |
| **AI Sandbox** | Try prompts against up to three registered models side by side, through the AI Gateway in the sandbox environment, so policies, guardrails and budgets apply | `/studio/sandbox/` |
| **IRGF Agent** | A chat icon on every page. Guided questions by topic, or your own: the IRGF framework ([irgframework.org](https://irgframework.org/)) — stages, gates, tiers, modules, the agent chain — and how the Control Tower applies it, answered from the framework reference, this documentation and the rules the platform enforces, with links to the sources and to the right page | every page |
| **AI Governance** | IRGF risk classification (Tier 1–4, independently countersigned), deployment authorization of agents and applications with their composite authority (Gate G3), change control (Gate G4) and time-boxed exceptions (M17), the Control Plane comparing what runs with what was authorized (M12), drift records with response windows by tier (M13), action limits enforced before each tool call and every action verified after it (links 06–07), risk register and heat map, human approvals, typed control policies, evaluation suites and red-team runs | `/classification/`, `/lifecycle/`, `/control-plane/`, `/risks/`, `/approvals/`, `/policies/`, `/evaluations/` |
| **Control & Settings** | Tamper-evident audit trail, users, teams, service accounts and API keys | Profile menu |
| **Integration Handbook** | The developer guide: onboarding, API reference, both gateways, sample code | `/docs/integration/` |

## Architecture

```mermaid
flowchart LR
    subgraph callers["Callers"]
        app["Applications and agents"]
        ide["IDEs and MCP clients"]
        engine["External AI engines"]
    end

    subgraph tower["AI Control Tower"]
        aigw["AI Gateway<br/>/api/v1/gateway/chat/"]
        mcpgw["MCP Gateway<br/>/api/v1/mcp/"]
        a2agw["A2A Gateway<br/>/api/v1/a2a/"]
        telemetry["Telemetry API<br/>/api/v1/executions/"]
        governance["Policies, budgets,<br/>approvals, audit"]
        registry["Registries"]
        ui["Web UI"]
    end

    app --> aigw
    app --> mcpgw
    app --> a2agw
    ide --> mcpgw
    engine --> telemetry
    aigw --> providers["Model providers"]
    mcpgw --> servers["MCP servers"]
    a2agw --> peers["Other agents:<br/>mailboxes and A2A endpoints"]
    aigw -. checks .-> governance
    mcpgw -. checks .-> governance
    a2agw -. checks .-> governance
    registry --- db[("PostgreSQL")]
    governance --- db
    ui --- db
    tower --- redis[("Redis: Celery, Channels, cache")]
```

The platform runs in two modes, side by side:

- **Reporting mode** — your engines keep calling their providers directly and report what they did
  to the telemetry API. The Tower observes and advises; it is never in the request path.
- **Gateway mode** — applications send model calls to the AI Gateway, tool calls to the MCP
  Gateway and work for other agents to the A2A Gateway. Policy verdicts become real refusals, budgets
  become real stops, and every call is recorded.

Everything vendor-specific sits behind an adapter registry in [`integrations/`](integrations/):
providers (`openai`, `generic_rest`, `mock`), agent engines, webhook normalizers, MCP transports
(`streamable_http`, `mock`) and A2A transports (`jsonrpc`, `mock`). Adding a vendor means registering
one class, not editing call sites.

**Stack:** Python 3.12 · Django 6.1 · Django REST Framework 3.18 · Channels 4.3 + Daphne (ASGI and
WebSockets) · Celery 5.6 + Redis · PostgreSQL 16 (SQLite for local use) · Bootstrap 5, jQuery and
Chart.js · WhiteNoise.

---

## Quick start (local)

This runs the whole platform on your machine with SQLite, no Docker and no Redis. For every other
setup — PostgreSQL and Redis without Docker, a clean install without demo data, connecting real
providers and MCP servers, and production deployment — see **[SETUP.md](SETUP.md)**. For the full
technical reference — every setting, the integration contracts of the API and both gateways,
operations, troubleshooting and FAQs — see the **[Technical Guide](docs/technical-guide.md)**. How the
platform applies the IRGF governance framework — risk tiers, the deployment and change gates, the
Control Plane and applications as systems of agents — is in **[IRGF alignment](docs/irgf-alignment.md)**.

**Prerequisites:** Python 3.12 and Git.

### 1. Get the code and install dependencies

```bash
git clone <your-repository-url> aig-tower
cd aig-tower
python -m venv .venv
```

Activate the virtual environment:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

### 2. Configure

Create a file named `.env` in the project root:

```dotenv
DJANGO_DEBUG=1
USE_SQLITE=1
# No Redis locally: run background tasks in-process instead of queueing them.
CELERY_TASK_ALWAYS_EAGER=1
```

> `.env.example` is written for Docker Compose (PostgreSQL and Redis on their service hostnames).
> For a plain local run, the three lines above are all you need; every other setting has a safe
> development default. See [Configuration](#configuration) for the full list.

### 3. Create the database and load the demo estate

```bash
python manage.py migrate
python manage.py seed_demo
```

`seed_demo` builds a realistic organization — *Northwind Financial Group* — with users, teams,
providers, models, agents, applications, 500 executions, budgets, risks, policies, approvals, a
governed MCP estate with real gateway traffic, and an A2A estate: published agents, an external
reinsurer's agent, contracts between them and real hand-offs, refusals included. It also puts the
IRGF lifecycle to work: risk
classifications, deployment authorizations for agents and one application, changes waiting at
Gate G4, an exception, and the drift the Control Plane finds. It is idempotent; run it again
whenever you like.

On its first run it also prints a **service-account API key** — the only time that key is shown.
Copy it: it is how external code talks to the API and the gateways. (Lost it? Open the profile
menu → *Integrations*, open the `langgraph-runtime` service account and issue a new key there.)

### 4. Run

```bash
python manage.py runserver 8000
```

Open <http://localhost:8000> and sign in:

| Email | Password | Role |
|---|---|---|
| `ava.mitchell@northwind.example` | `ControlTower!2026` | Organization Administrator (sees everything) |
| `admin@northwind.example` | `ControlTower!2026` | Superuser: the site administrator, who creates organizations |

![Sign in](screens/00-login.png)

> `runserver` already serves ASGI through Daphne, so the live monitor's WebSockets work in
> development without a separate process.

---

## Running with Docker Compose

The Compose file runs the production-shaped stack: PostgreSQL, Redis, the ASGI web server, a Celery
worker and Celery beat.

```bash
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY, and any provider keys you want to use.
docker compose up --build
```

In a second terminal, load the demo estate and create your own superuser if you want one:

```bash
docker compose exec web python manage.py seed_demo
docker compose exec web python manage.py createsuperuser
```

Then open <http://localhost:8000>.

| Service | What it runs | Port |
|---|---|---|
| `postgres` | PostgreSQL 16 | 5432 |
| `redis` | Redis 7 — Celery broker and results, cache, Channels layer | 6379 |
| `web` | `migrate`, `collectstatic`, then Daphne serving `aig_tower.asgi` | 8000 |
| `celery_worker` | Background tasks | — |
| `celery_beat` | The periodic schedule (see [Background jobs](#background-jobs)) | — |

The web container sets `USE_MANIFEST_STATIC=1`, so static files are served compressed and
fingerprinted by WhiteNoise.

---

## Configuration

All configuration is read from environment variables (a `.env` file in the project root is loaded
automatically).

### Core

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | development-only value | **Set a long random value in production.** |
| `DJANGO_DEBUG` | `1` | `0` in production. Turning it off also enables secure cookies, HSTS and `X-Frame-Options: DENY`. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Comma-separated host names. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Origins allowed to post forms. |
| `SITE_URL` | empty | Absolute base URL used in notification emails. |
| `USE_MANIFEST_STATIC` | `0` | `1` to serve fingerprinted static files (requires `collectstatic`). |

### Database, cache, queues and WebSockets

| Variable | Default | Purpose |
|---|---|---|
| `USE_SQLITE` | `1` | `1` uses `db.sqlite3`, an in-memory channel layer and a local-memory cache. `0` uses PostgreSQL and Redis. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `aig_tower` | PostgreSQL credentials. |
| `POSTGRES_HOST`, `POSTGRES_PORT` | `localhost`, `5432` | PostgreSQL location. |
| `REDIS_URL` | `redis://localhost:6379/0` | Cache. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker. |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery results. |
| `CELERY_TASK_ALWAYS_EAGER` | `0` | `1` runs tasks inline — use it when there is no Redis. |
| `CHANNEL_LAYER_URL` | `REDIS_URL` | Redis database for live-monitor broadcasts. |

### Email

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_EMAIL_BACKEND` | console backend | Notification emails are printed, never sent, until you point this at a real backend. |
| `DJANGO_FROM_EMAIL` | `ai-control-tower@localhost` | Sender address. |

### AI Gateway

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_ATTEMPT_TIMEOUT` | `30` | Seconds before one provider call is abandoned. |
| `GATEWAY_DEADLINE` | `60` | Seconds for a whole request, fallbacks included. |
| `GATEWAY_CIRCUIT_THRESHOLD` | `5` | Consecutive failures before a provider is taken out of rotation. |
| `GATEWAY_CIRCUIT_COOL_OFF` | `30` | Seconds before an open breaker lets a probe through. |
| `GATEWAY_ENFORCE_BUDGETS` | `1` | `0` makes every hard-stop budget advisory again. |

### MCP Gateway

| Variable | Default | Purpose |
|---|---|---|
| `MCP_GATEWAY_CALL_TIMEOUT` | `30` | Seconds before one upstream tool call is abandoned. |
| `MCP_GATEWAY_CIRCUIT_THRESHOLD` | `5` | Consecutive failures before a server is taken out of rotation. |
| `MCP_GATEWAY_CIRCUIT_COOL_OFF` | `30` | Seconds before an open breaker lets a probe through. |
| `MCP_GATEWAY_REVIEW_NEW_TOOLS` | `1` | New tools, and tools whose definition changes, wait for review. `0` trusts every upstream. |
| `MCP_GATEWAY_RECORD_ARGUMENTS` | `1` | Keep tool arguments and result previews in the call log (shown only to `prompt.view` holders). |
| `MCP_GATEWAY_ALLOWED_ORIGINS` | empty | Extra browser origins allowed to call the MCP endpoint. |

### A2A Gateway

| Variable | Default | Purpose |
|---|---|---|
| `A2A_GATEWAY_CALL_TIMEOUT` | `30` | Seconds before one delivery to an agent reached over HTTP is abandoned. |
| `A2A_GATEWAY_CIRCUIT_THRESHOLD` | `5` | Consecutive failures before an agent is taken out of rotation. |
| `A2A_GATEWAY_CIRCUIT_COOL_OFF` | `30` | Seconds before an open breaker lets a probe through. |
| `A2A_GATEWAY_REVIEW_NEW_SKILLS` | `1` | New skills, and skills whose definition changes, wait for review. `0` trusts every agent's card. |
| `A2A_GATEWAY_RECORD_CONTENT` | `1` | Keep message and answer text in the log of exchanges with agents reached over HTTP (shown only to `prompt.view` holders). A mailbox message is the delivery itself, so it is kept either way. |
| `A2A_GATEWAY_MAX_HOPS` | `4` | How many times work may be handed on: A asks B, B asks C, and so on. |
| `A2A_GATEWAY_MAX_MESSAGE_KB` | `256` | The largest message, or answer, the gateway takes. |
| `A2A_GATEWAY_ALLOWED_ORIGINS` | empty | Extra browser origins allowed to call the A2A endpoint. |

### Content display

| Variable | Default | Purpose |
|---|---|---|
| `PROMPT_REDACT_CLASSIFICATIONS` | `confidential,restricted` | Prompt and completion text for agents with these classifications is redacted on screen. |

### IRGF Agent

| Variable | Default | Purpose |
|---|---|---|
| `ASSISTANT_PROVIDER` | `openai` | `openai`: the IRGF Agent answers with ChatGPT and its skills. `documentation`: passages quoted from the documentation, no model. |
| `OPENAI_API_KEY` | *(empty)* | The key ChatGPT uses. Without it, the agent answers from the documentation. |
| `ASSISTANT_OPENAI_MODEL` | `gpt-4o-mini` | The ChatGPT model. |
| `ASSISTANT_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint. |
| `ASSISTANT_AGENT` | *(empty)* | Instead of calling ChatGPT directly, the `agent_id` of an agent whose model phrases answers through the AI Gateway, under its policy, budget and audit. |

### Provider and MCP server credentials

The database **never** stores a credential. Each provider and MCP server record holds a
`credential_ref` — the *name* of an environment variable — and the platform resolves the secret at
call time. Set those variables in the platform's environment, for example:

```dotenv
OPENAI_API_KEY=sk-...
AZURE_OPENAI_API_KEY=...
GITHUB_MCP_TOKEN=...
GITHUB_COPILOT_TOKEN=...   # reads Copilot seats and billing usage for Developer Tools
```

The registries show each credential as **Resolved** or **Missing**, so you can see at a glance
which ones the running platform can actually find.

---

## Demo data, accounts and roles

`seed_demo` creates these users in *Northwind Financial Group*, all with the password
`ControlTower!2026` (change it with `--password`):

| Email | Name | Title | Role |
|---|---|---|---|
| `ava.mitchell@northwind.example` | Ava Mitchell | Chief AI Officer | Organization Administrator |
| `daniel.okafor@northwind.example` | Daniel Okafor | AI Platform Lead | AI Platform Administrator |
| `priya.raman@northwind.example` | Priya Raman | Senior AI Engineer | AI Developer |
| `marcus.lee@northwind.example` | Marcus Lee | AI Engineer | AI Developer |
| `sofia.hernandez@northwind.example` | Sofia Hernandez | AI Governance Manager | AI Governance Manager |
| `tom.becker@northwind.example` | Tom Becker | Operational Risk Manager | Risk Manager |
| `chen.wei@northwind.example` | Chen Wei | Head of Customer Operations | Approver |
| `nadia.faris@northwind.example` | Nadia Faris | Internal Auditor | Auditor |
| `admin@northwind.example` | Platform Admin | — | Superuser (site administrator) |

Signing in as different people is the quickest way to see role-based access at work.

### What each role may do

Permissions are organization-scoped: the same person can be an Approver in one organization and a
Viewer in another.

| Role | Beyond read access, may… |
|---|---|
| Superuser (site administrator) | Not a role but an account flag, set by `createsuperuser`. Everything in every organization, plus creating organizations and managing anyone's membership in any of them. |
| Organization Administrator / Super Administrator | Everything: people, teams and service accounts, and the second signature on Tier 4 gate decisions, as the Enterprise Governance Sponsor. |
| AI Platform Administrator | Manage the inventory, providers, models, agents and applications; score their risk classification; request deployment authorizations and exceptions; ingest telemetry; manage costs; read prompts; call the AI Gateway; manage and call the MCP Gateway; publish agents for A2A and send through the A2A Gateway; use the agent kill-switch. |
| AI Developer | Manage agents and applications; score their risk classification; request deployment authorizations and exceptions; ingest telemetry; read prompts; call the AI and MCP Gateways; publish agents for A2A and send through the A2A Gateway; use the agent kill-switch. |
| AI Governance Manager | Decide IRGF gates and exceptions as the AI Governance Body; approve models; approve MCP servers, review tools and mark their action-limit arguments; approve A2A agents, review their skills and grant contracts between agents; manage policies and risks; score and countersign risk classifications; resolve drift and run the Control Plane; decide approvals; read the audit trail and prompts; use the agent kill-switch. |
| Risk Manager | Decide Tier 1–2 changes and exceptions as the Risk and Compliance Lead; manage risks; score and countersign risk classifications; resolve drift and run the Control Plane; read the audit trail and prompts; use the agent kill-switch. |
| Approver | Decide approvals; read prompts. |
| Auditor | Read the audit trail and prompts. |
| Viewer | Read-only: inventory, executions, costs, risks, approvals, policies. No prompt text. |

Ownership counts as well as role. A system's owner signs its Tier 1–2 deployment authorization,
may request authorizations and exceptions for it, and may revert or ratify its drift. From Tier 3,
owners and requesters are kept out of the decision. Tier 4 takes two different people: in the demo,
Sofia Hernandez signs first for the AI Governance Body and Ava Mitchell second as the Sponsor. The
[IRGF alignment](docs/irgf-alignment.md#8-who-does-what) lists who does what.

Service accounts carry a role exactly like people do; the default, **AI Developer**, is everything an
application needs. Service accounts never sign a gate decision.

---

## Product tour

*Screenshots are taken from the seeded demo organization; all of them are in [`screens/`](screens/).*

### Dashboard and Control Tower

The **Dashboard** is the executive view for any period — applications, agents, runs, spend,
latency, policy denials, pending approvals, open risks and provider incidents. The **Control Tower**
maps the estate end to end and leads with what needs a person.

![Control Tower](screens/02-dashboard-control-tower.png)

### Onboarding

**Onboarding** in the header opens a hub that walks the estate in dependency order:
- providers, models, agents and applications
- routing and MCP servers
- policies and rules, budgets and risks
- IRGF classification and authorization

Each step shows what it still needs, with a button for whatever your role may add.

![Onboarding hub](screens/03-onboarding-hub.png)

Every registry and governance record has its own create and edit page, and people, teams and service
accounts are managed from the profile menu. The menu offers only what the signed-in role may
create. Each save is checked and written to the audit trail, and editing a system authorized at
Gate G3 goes through change control.

![Onboarding an agent](screens/07-inventory-agent-create.png)

### AI Landscape — the registries

Every AI asset, with its owner, environment, criticality, risk and status.

![Agents registry](screens/05-inventory-agents.png)

![Applications registry](screens/08-inventory-applications.png)

An agent's page shows its execution chain, the boundaries it runs within (allowed and restricted
tools, knowledge sources) and the applications that use it. It also carries the agent's IRGF record:
its risk tier, deployment authorization, Control Plane findings, the eight-link governance chain, its
Agent Card and the kill-switch. An application's page adds its composition: member agents, the
delegations between them and the authority they reach together.

![Agent detail](screens/06-inventory-agent-detail.png)

Providers show their adapter, region, health and whether the platform can resolve their credential;
models show pricing, context window, approval and risk.

![Providers registry](screens/10-inventory-providers.png)

![Models registry](screens/09-inventory-models.png)

### Observability

Every run reported by an engine or served by a gateway, filterable by status, agent, application,
provider, environment and period.

![Executions](screens/11-executions-list.png)

Each execution has a normalized event timeline — prompts, model calls, tool calls, approvals,
policy violations — with tokens, duration and cost. The **Live Monitor** (`/executions/live/`)
streams runs in flight over WebSockets.

![Live Monitor](screens/12-executions-live.png)

### AI Gateway

What the gateway is doing to live traffic: requests, fallbacks, latency, spend, provider circuit
breakers and routing outcomes — plus a warning for any provider whose credential cannot be resolved.

![AI Gateway overview](screens/13-ai-gateway-overview.png)

The **Routing Simulator** shows which models the platform would choose right now for a given type
and environment, in what order, and why the others were excluded.

![Routing simulator](screens/15-routing-simulator.png)

![Routing decisions](screens/16-routing-decisions.png)

### MCP Gateway

Every MCP server in one registry, reached through one governed endpoint. The overview leads with
what needs a decision: servers out of rotation, tools awaiting review (including tools whose
definition changed after approval), unapproved servers and missing credentials.

![MCP Gateway overview](screens/17-mcp-overview.png)

A server's page holds its connection, governance, health and breaker state, its tools with review
actions, and the agents granted access.

![MCP server registry](screens/18-mcp-servers.png)

The **Tool Catalog** lists every tool from every server and what governance decided about it; the
*Awaiting review* tab is the review queue.

![MCP tool catalog](screens/19-mcp-tools.png)

**Tool Calls** is the log of everything brokered — and everything refused, with the reason.

![MCP tool calls](screens/20-mcp-calls.png)

### A2A Gateway

Agent-to-agent communication, under the same rules the other gateways apply to model and tool
calls. The gateway speaks the [Agent2Agent (A2A) protocol](https://a2a-protocol.org/): every agent
that takes work from others is published at its own address, `/api/v1/a2a/<address>/`, with its Agent
Card at the usual well-known path. Behind the address:

- **The registry.** An agent of the organization collects its work from the gateway's mailbox, or is
  reached at its own A2A endpoint. An external agent — a reinsurer's, a vendor's — is registered with
  its endpoint and a `credential_ref`, so no agent ever holds another's credential.
- **Review.** Governance approves each agent and reviews each skill its card advertises. A skill
  whose description changes after review goes back to review, and no agent can ask for it meanwhile.
- **Contracts.** On a restricted agent, only a sender holding a contract gets through: for the skills
  the contract names, up to its data ceiling, at most at its rate, until it expires.
- **Content.** Every message is held to the contract's ceiling and the recipient's clearance. Personal
  data found in a message makes it at least confidential, whatever the sender declared.
- **Chains.** Work handed on is followed hop by hop. A chain longer than the hop limit, or one that
  would come back to an agent already in it, is refused.

The **A2A Overview** leads with what needs a decision and maps who may hand work to whom. The
**Agent Registry**, **Skill Catalog**, **Contracts** and **Communications** pages hold the rest,
and every refusal is kept with its reason. Contracts are part of each agent's authorized
configuration, so a new one for an agent authorized at Gate G3 goes to Gate G4. The Control Plane
verifies every hand-off, against the sender's authorization and the applications' delegation graphs.

![A2A Overview](screens/21-a2a-overview.png)

![A2A Agent Registry](screens/22-a2a-agents.png)

![Skill Catalog](screens/23-a2a-skills.png)

![Contracts](screens/24-a2a-contracts.png)

![Communications](screens/25-a2a-tasks.png)

### AI FinOps

Spend today, this week and this month, a straight-line projection, budget alerts, and spend over
time and by provider, model and team. From there:

- **Cost Explorer** (`/costs/explorer/`) groups spend by department, team, application, agent,
  model, provider, individual or environment. Click a bar to drill down — department, then team,
  agent, model and provider by default, or any dimension from a row's menu. Every slice shows the
  previous period alongside, a daily trend of its top five, and a CSV export.
- **Developer Tools** (`/costs/developer-tools/`) connects GitHub Copilot: seat licences and
  premium-request overage per team, then per individual, with idle seats and allowance use.
- **Cost Governance** (`/costs/governance/`) shows every budget's health (on track, watch, forecast
  to exceed, over), alert rules and open alerts. Budgets can be held against a department, team,
  individual, application, agent, provider or model, count AI runtime, developer tools or both, warn
  when spend is *forecast* to pass the limit, and notify the owner, the team lead and named people as
  well as cost managers. Alert rules watch a rolling hour, day or week for a level, a spike against the
  usual, an expensive average run or money spent on failures.
- **Optimization** (`/costs/optimization/`) lists costed recommendations — right-size a model,
  cache a long prompt prefix, cap verbose output, fix failures you are paying for, reclaim idle Copilot
  seats, move seats between plans, put a budget on unwatched spend — each with its evidence, a
  confidence and an effort, to accept, mark done or dismiss.

![AI FinOps dashboard](screens/26-finops-dashboard.png)

![Cost Explorer](screens/27-finops-explorer.png)

![Developer Tools](screens/28-finops-developer-tools.png)

![Cost Governance](screens/29-finops-governance.png)

![Budgets](screens/30-finops-budgets.png)

![Optimization](screens/31-finops-optimization.png)

### AI Governance

The risk register with a 5×5 likelihood-by-impact heat map, inherent and residual.

![Risk register](screens/42-governance-risks.png)

![Risk matrix](screens/43-governance-risk-matrix.png)

The **Approval Centre** holds actions waiting on a human decision, and the rules that send them there.

![Approval Centre](screens/44-governance-approvals.png)

**Policies** are typed controls — model approval, provider, data classification, agent autonomy,
cost limit, tool access, human approval — that can deny, require approval or warn. Both gateways
enforce them.

![Policies](screens/45-governance-policies.png)

The **Evaluation Centre** runs scored suites and red-team cases against models and agents; policies
can require passing evidence before a model may serve.

![Evaluation Centre](screens/46-governance-evaluations.png)

![Evaluation runs](screens/47-governance-evaluation-runs.png)

### Use-Case Intake and Gate G1

**Use-Case Intake** records an AI idea before anything is built (IRGF S0–S1): the need, the non-AI
alternative, the decision it supports, who it affects, its autonomy and data path, and provisional
D1–D5 scores that give a tier. Gate G1 is decided by the people that tier calls for, with IRGF's five
outcomes, and an approved use case is registered as a draft application.

![Use-Case Intake](screens/32-governance-intake.png)

![A use case at Gate G1](screens/33-governance-intake-detail.png)

### IRGF lifecycle

The platform applies the [Intent-to-Runtime Governance Framework](docs/irgf-alignment.md) (IRGF).
**Risk Classification** (stage S2) scores every agent and application on five dimensions and derives
a tier from 1 to 4, which someone outside the delivery line countersigns. The tier then decides which
policies apply, who signs each gate and how fast drift must be answered.

![Risk classification register](screens/34-governance-classification.png)

**Deployment Authorizations** (Gate G3) authorize an agent or an application as one configuration,
at one tier, for one owner. An application is authorized with its composite authority: everything
its agents can reach between them. Tier 4 takes two signatures from two different people.
**Changes** (Gate G4) then catch every edit to what was authorized and route it by IRGF's change
table. **Exceptions** (M17) set one policy aside for at most 90 days.

![Deployment authorizations](screens/35-governance-authorizations.png)

![Changes at Gate G4](screens/36-governance-changes.png)

![Exceptions (M17)](screens/37-governance-exceptions.png)

The **Control Plane** (IRGF M12) compares what every agent and application actually does with what
Gate G3 authorized. Each divergence becomes a drift record with a response window set by its category
and tier (M13). Action limits are enforced before each tool call, and every action is verified after
it. See [IRGF alignment](docs/irgf-alignment.md#11-phase-3-the-control-plane).

![Control Plane](screens/38-governance-control-plane.png)

![Drift register](screens/41-governance-drift.png)

### Compliance & Evidence

Each system's obligations under the **EU AI Act**, **ISO/IEC 42001** and the **NIST AI RMF**, answered
from what the platform already records, with scope decisions, attestations for what it cannot see, and
an evidence pack to print or download for an auditor.

![Compliance & Evidence](screens/39-governance-compliance.png)

![A system's requirements and evidence](screens/40-governance-compliance-system.png)

### AI Studio — Solution Advisor and AI Sandbox

The **Solution Advisor** takes an AI use case in plain terms and returns a blueprint: the approach and
the alternatives considered, a technology stack layer by layer, a build plan mapped to IRGF stages and
gates, the team, risks, an evaluation plan, a cost estimate, the controls its tier calls for, and
starter code that calls the AI Gateway. ChatGPT writes it when `OPENAI_API_KEY` is set; otherwise the
built-in advisor does. One click proposes it at Gate G1.

![Solution Advisor](screens/56-studio-solution-advisor.png)

![Describing a use case](screens/57-studio-describe-use-case.png)

![A solution blueprint](screens/58-studio-blueprint.png)

The **AI Sandbox** tries prompts against up to three registered models side by side, through the AI
Gateway in the sandbox environment, so policies, guardrails and budgets apply and every call is recorded.

![Sandbox sessions](screens/59-studio-sandbox-sessions.png)

![The AI Sandbox](screens/60-studio-sandbox.png)

### IRGF Agent

A chat icon on every page opens the **IRGF Agent**: guided questions by topic, or your own, about the
IRGF framework and how the Control Tower applies it.

![IRGF Agent](screens/55-irgf-agent-chat.png)

### Control & Settings

The profile menu holds the audit trail, the organization's people and machine identities, and the
Integration Handbook:
- **Users** lists every member with their role, teams and last sign-in. An organization
  administrator adds someone by email, changes their role, or removes them. Someone new gets a
  single-use link to set their own password.
- **Teams** group people, and own agents, applications and budgets.
- **Integrations** holds the service accounts that applications call the platform as. Bind each one
  to the agents it runs, and issue or revoke its API keys. Each key is shown once.
- **Organizations**, for superusers only, lists every organization on the installation. A
  superuser creates an organization together with its first administrator, and adds, changes or
  removes anyone in any organization from its page.

![Users](screens/48-access-members.png)

![Teams](screens/49-access-teams.png)

![Service accounts](screens/50-access-service-accounts.png)

The **Audit Trail** is append-only; every entry carries a content hash so a change made directly in
the database is still detectable.

![Audit trail](screens/52-audit-log.png)

The notification centre lists every alert sent to you, and each person chooses what reaches them by
email or in the bell.

![Notifications](screens/53-notifications.png)

![Notification preferences](screens/54-notification-preferences.png)

### Integration Handbook

The developer guide at `/docs/integration/`: onboarding, the API reference, both gateways, sample
code for Python, TypeScript and common agent frameworks, security model and troubleshooting.

![Integration Handbook](screens/04-integration-handbook.png)

---

## Onboarding your AI estate

Register in dependency order — each record points at the ones before it. Everything below is done in
the web UI, from **Onboarding** in the header and the profile menu, or over the REST API. A
superuser creates the organization itself, under the profile menu → *Organizations*. The
[Integration Handbook](http://localhost:8000/docs/integration/#estate) walks through each with
sample payloads.

1. **Platform provider** — name, adapter (`openai`, `generic_rest`, `mock`; see
   `GET /api/v1/adapters/`), endpoint, region and `credential_ref`. Set the variable, then press
   *Health check* and *Refresh capabilities* on its page. Governance presses *Approve for
   production*.
2. **Models** — `model_id`, type, context window and per-1K pricing (pricing drives cost and
   routing). Governance approves each on its page (*Approve for use*); if a policy requires
   evaluation evidence, run a suite in the Evaluation Centre first.
3. **Agent** — a stable `agent_id` (the identifier your code sends), framework, provider, primary
   and fallback models, environment, risk, autonomy level and data classification.
4. **Application** — owners, criticality, data classification, review date, and the agents, models
   and providers it uses.
5. **Identity** — create a service account for the application (profile menu → *Integrations* →
   *New service account*), ticking the agents it runs, and issue a key on its page. The key is shown
   once; store it as a secret. From Tier 3, the gateways refuse an agent any key that is not bound to
   it.
6. **MCP servers** — *MCP Gateway → Server Registry → Register server*. Choose Streamable HTTP, set
   the endpoint and `credential_ref`, then **Health check → Approve → Sync tools → review each
   tool → grant agents**. For a tool that moves value, mark which argument carries the amount and
   which the counterparty (*Action-limit arguments*), so agents' action limits can hold it.
7. **A2A agents** — *A2A Gateway → Agent Registry → Publish agent*, or *Publish for A2A* on an
   agent's page. An agent with no A2A endpoint of its own takes its work from the gateway's mailbox;
   declare its skills, one per line. Register an external agent with its endpoint and
   `credential_ref`, then **Sync card**. Then **Approve → review each skill → grant a contract** to
   each agent that may send it work, with its skills, data ceiling and rate.
8. **Guardrails** — routing policies (optional: without one, the gateway ranks every active model of
   the requested type and offers a three-model fallback chain), control policies and budgets.
9. **Classify (IRGF S2)** — score each agent and application under *AI Governance → Risk
   Classification*, and have someone outside its delivery line countersign. Fill in each acting
   agent's Agent Card: what it is not for, action limits, escalation thresholds and kill-switch
   conditions.
10. **Authorize (IRGF Gate G3)** — *Request authorization* on each agent's page, then on the
    application's. The request page lists the entry criteria for the tier; the decision rights say
    who signs. To make the gateways refuse anything that skipped S2 or G3, add a `risk_classification`
    and a `deployment_authorization` policy with effect `deny`. The defaults include neither.
11. **Prove it** — a gateway call naming the agent returns `200`, the MCP catalog lists the tools
    you enabled, the A2A directory lists the agents it may reach, all of it shows up under Executions,
    MCP Tool Calls and A2A Communications, and the Control Plane lists it as verified against the
    authorization.

---

## Using the gateways from your applications

Your application holds exactly one secret: its service-account key. Call the gateways from your
backend — never from a browser. The examples use the key `seed_demo` printed.

```bash
export CONTROL_TOWER_KEY="act_..."
```

### AI Gateway — a governed model call

```bash
curl -X POST http://localhost:8000/api/v1/gateway/chat/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "agent": "customer-support-agent",
        "messages": [{"role": "user", "content": "Summarise the open incidents."}],
        "max_tokens": 400
      }'
```

The response carries the answer, the model and provider that served it, token usage, cost, every
attempt made, and the execution it was recorded as. Add `"stream": true` for server-sent events;
add `"model": "gpt-4o-mini"` to pin a model (the platform's ranking becomes your fallback). The
demo providers use the mock adapter, so calls succeed without any real provider credentials.

The status code tells you what to do: **202** a person must approve first · **402** budget
exhausted · **403** refused by policy (do not retry) · **503** no eligible model · **502** every
attempt failed (retry with backoff).

### MCP Gateway — every governed tool through one endpoint

Point any MCP client that speaks Streamable HTTP at `/api/v1/mcp/<agent-id>/`. It sees every tool the
agent is permitted, from every approved server, named `<server>__<tool>`.

```bash
# Claude Code
claude mcp add --transport http control-tower \
  http://localhost:8000/api/v1/mcp/customer-support-agent/ \
  --header "Authorization: Bearer $CONTROL_TOWER_KEY"
```

```json
{
  "mcpServers": {
    "control-tower": {
      "type": "http",
      "url": "http://localhost:8000/api/v1/mcp/customer-support-agent/",
      "headers": { "Authorization": "Bearer act_..." }
    }
  }
}
```

Or speak the protocol directly — the endpoint is stateless, so one request is enough:

```bash
curl http://localhost:8000/api/v1/mcp/customer-support-agent/ \
  -H "Authorization: Bearer $CONTROL_TOWER_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
       "params": {"name": "servicenow__search_incidents", "arguments": {"query": "card declined"}}}'
```

Code that would rather not speak JSON-RPC gets the same catalog and the same enforced calls over REST:

```bash
curl "http://localhost:8000/api/v1/mcp-gateway/tools/?agent=customer-support-agent" \
  -H "X-API-Key: $CONTROL_TOWER_KEY"

curl -X POST http://localhost:8000/api/v1/mcp-gateway/tools/call/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" -H "Content-Type: application/json" \
  -d '{"agent": "customer-support-agent", "tool": "servicenow__search_incidents",
       "arguments": {"query": "card declined"}}'
```

A refused MCP call comes back as a tool result with `isError: true` and the reason, so a model can
adapt. Every call — served or refused — lands in *MCP Gateway → Tool Calls*.

### A2A Gateway — one agent hands another work

Each published agent is an A2A server at its own address. The sender is named in `X-Agent-ID`, or is
the one agent the key is bound to; the skill rides in the message's metadata:

```bash
curl http://localhost:8000/api/v1/a2a/claims-triage-agent/ \
  -H "Authorization: Bearer $CONTROL_TOWER_KEY" -H "X-Agent-ID: customer-support-agent" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "m-1",
        "parts": [{"kind": "text", "text": "Where does claim CLM-4471 stand?"}],
        "metadata": {"io.aicontroltower/skill": "claim-status"}}}}'
```

The answer is an A2A task. A mailbox agent collects it from `/api/v1/a2a-gateway/inbox/<agent>/` and
answers at `/api/v1/a2a-gateway/tasks/<id>/status/`; the sender follows it with `tasks/get`. An agent
reached over HTTP answers at once, or when `tasks/get` asks it again. A refused message comes back as
a task in the `rejected` state with the gateway's reason, so the sending agent can adapt. The same
sends work over REST:

```bash
curl -X POST http://localhost:8000/api/v1/a2a-gateway/messages/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" -H "Content-Type: application/json" \
  -d '{"agent": "customer-support-agent", "recipient": "claims-triage-agent",
       "skill": "claim-status", "text": "Where does claim CLM-4471 stand?"}'
```

`GET /api/v1/a2a/?agent=customer-support-agent` lists every agent the sender may reach and the
skills it may ask each for. Every hand-off — delivered or refused — lands in *A2A Gateway →
Communications*.

### Reporting mode — telemetry from your own engine

```bash
curl -X POST http://localhost:8000/api/v1/executions/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" -H "Content-Type: application/json" \
  -d '{"execution_id": "run-0001", "agent": "customer-support-agent", "status": "running",
       "events": [{"event_type": "EXECUTION_STARTED", "sequence": 0}]}'

curl -X POST http://localhost:8000/api/v1/executions/run-0001/complete/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" -H "Content-Type: application/json" -d '{}'
```

The [Integration Handbook](http://localhost:8000/docs/integration/#build) has complete samples for
Python (including streaming), TypeScript, the Python MCP SDK, LangChain/LangGraph, the OpenAI
Agents SDK and VS Code, plus an end-to-end example that uses both gateways under one traced run.

---

## API at a glance

All endpoints live under `/api/v1/`. Authenticate with `X-API-Key: act_...` (or
`Authorization: Api-Key act_...`; the MCP endpoint also accepts `Authorization: Bearer act_...`).
Signed-in browser sessions work too, and the browsable API at `/api/v1/` is enabled.

| Endpoint | Method | Purpose | Permission |
|---|---|---|---|
| `/health/` | GET | Liveness | none |
| `/adapters/` | GET | Adapters this installation can reach | authenticated |
| `/providers/` `/models/` `/agents/` `/applications/` | GET, POST, PUT, PATCH | The registries | `inventory.view` / the matching `*.manage` |
| `/agents/{id}/suspend/` `resume/` | POST | Pull or release an agent's kill-switch (people only may resume) | `agent.suspend` |
| `/deployment-authorizations/` | GET | IRGF Gate G3 authorizations, for deployment pipelines to check before shipping | `inventory.view` |
| `/drift-records/` | GET | IRGF M13 drift found by the Control Plane, for incident tooling and dashboards | `inventory.view` |
| `/mcp-servers/` | GET | The MCP registry with tool counts | `inventory.view` |
| `/executions/` | GET, POST | Read runs; report a run | `execution.view` / `telemetry.ingest` |
| `/executions/{id}/events/` | POST | Append events to a run | `telemetry.ingest` |
| `/executions/{id}/complete/` `fail/` `cancel/` | POST | Close a run | `telemetry.ingest` |
| `/telemetry/webhook/` | POST | A whole engine payload, normalized by a named adapter | `telemetry.ingest` |
| `/policy/check/` | POST | May this action proceed? (advisory) | authenticated |
| `/approval-requests/` | GET, POST | Raise and track human approvals | `approval.view` / `telemetry.ingest` |
| `/gateway/chat/` | POST | A governed model call (JSON or SSE) | `gateway.invoke` |
| `/gateway/messages/` and `/gateway/messages/{agent}/` | POST, GET | The original agent mailbox, kept; it now goes through the A2A Gateway | `gateway.invoke` |
| `/mcp/` and `/mcp/{agent}/` | POST | The MCP Gateway as an MCP server | `mcp.invoke` |
| `/mcp-gateway/tools/` | GET | An agent's governed tool catalog | `mcp.invoke` |
| `/mcp-gateway/tools/call/` | POST | One governed tool call over REST | `mcp.invoke` |
| `/a2a/` | GET | The agents a sender may reach, and the skills it may ask each for | `a2a.invoke` |
| `/a2a/{address}/` | POST | A published agent as an A2A server: `message/send`, `tasks/get`, `tasks/cancel` | `a2a.invoke` |
| `/a2a/{address}/.well-known/agent-card.json` | GET | Its Agent Card, as governance reviewed it | `a2a.invoke` |
| `/a2a-gateway/messages/`, `/a2a-gateway/tasks/{id}/`, `.../cancel/` | POST, GET | The same hand-offs over REST | `a2a.invoke` |
| `/a2a-gateway/inbox/{agent}/`, `/a2a-gateway/tasks/{id}/status/` | GET, POST | A mailbox agent collects its work and answers it | `a2a.invoke` |
| `/a2a-cards/` | GET | The A2A registry, with skill and contract counts | `inventory.view` |

The live monitor also pushes updates over WebSocket at `ws://<host>/ws/executions/<organization-id>/`
(session authentication and organization membership).

---

## Background jobs

Celery beat runs these on a schedule (defined in [`aig_tower/celery.py`](aig_tower/celery.py)):

| Job | Every | What it does |
|---|---|---|
| Provider health checks | 5 minutes | Probes every provider through its adapter and records transitions. |
| Budget threshold checks | 15 minutes | Raises alerts as budgets cross their thresholds. |
| Daily usage rollups | hour | Rebuilds the cost rollups behind the FinOps charts. |
| Expire stale approvals | 10 minutes | Expires approval requests past their deadline. |
| Notify expiring approvals | 30 minutes | Warns approvers before a request expires. |
| Notify overdue risk reviews | day | Reminds owners of risks past their review date. |
| IRGF lifecycle sweep | hour | Expires M17 exceptions, warning seven days ahead, and escalates overdue Gate G3 conditions. |
| IRGF Control Plane sweep | minute | Verifies runs (link 07), looks for evaluation, policy, budget and tool-definition drift, escalates drift past its response window and sends the daily drift digest. |

Tool calls and agent-to-agent hand-offs are verified as they are recorded, with or without beat.
Without beat, runs wait to be verified until someone presses *Run the Control Plane now* on the
Control Plane page. The same applies to the drift checks.

To run them outside Docker you need Redis, then:

```bash
celery -A aig_tower worker --loglevel=info
celery -A aig_tower beat --loglevel=info
```

On Windows, add `--pool=solo` to the worker. For local development without Redis, leave them off
and set `CELERY_TASK_ALWAYS_EAGER=1`.

---

## Management commands

| Command | What it does |
|---|---|
| `python manage.py seed_demo [--password P] [--superuser EMAIL] [--executions N]` | Builds (or refreshes) the Northwind demo organization, including MCP servers, reviewed tools and gateway traffic. `--executions 0` skips the mock telemetry. |
| `python manage.py generate_executions [--count 500] [--days 14] [--organization NAME] [--purge] [--seed N]` | Generates realistic mock executions and events. |
| `python manage.py seed_evaluations [--organization NAME]` | Creates a starter evaluation suite. |
| `python manage.py verify_audit_log [--organization NAME] [--fail-on-tamper]` | Recomputes every audit entry's hash and reports any that no longer match. |

---

## Running the tests

```bash
python manage.py test
```

Run one module while you work on it:

```bash
python manage.py test apps.mcp_gateway
python manage.py test apps.gateway
```

The suites use their own throwaway database, so they never touch your data.

---

## Project structure

```text
aig-tower/
├── aig_tower/                  Django project: settings, URLs, ASGI, Celery
├── apps/
│   ├── accounts/               Users, roles and the RBAC permission matrix
│   ├── organizations/          Organizations, teams, memberships, active-organization middleware
│   ├── providers/              Platform provider registry and health checks
│   ├── models_registry/        Model registry and approvals
│   ├── agents/                 Agent registry
│   ├── applications_registry/  Application registry
│   ├── inventory/              Registry pages (AI Landscape) and their create and edit pages
│   ├── onboarding/             The Onboarding hub
│   ├── access/                 People, teams, service accounts and API keys
│   ├── executions/             Runs, events, live monitor (Channels)
│   ├── telemetry/              Telemetry ingestion
│   ├── gateway/                AI Gateway: pipeline, circuit breakers, agent messaging
│   ├── routing/                Routing policies, simulator and decisions
│   ├── mcp_gateway/            MCP registry, discovery, tool review, unified MCP endpoint
│   ├── a2a/                    A2A Gateway: agent cards, skill review, contracts, the A2A endpoint
│   ├── costs/                  Pricing, spend, budgets and alerts
│   ├── risks/                  Risk register and matrix
│   ├── approvals/              Approval rules and the Approval Centre
│   ├── policies/               Control policies and the policy engine
│   ├── classification/         IRGF S2 risk classification: D1–D5, tiers, countersignature
│   ├── lifecycle/              IRGF Gates G3 and G4, M17 exceptions, decision rights
│   ├── controlplane/           IRGF Control Plane: drift, action limits, verification
│   ├── evaluations/            Evaluation suites and runs
│   ├── notifications/          In-app and email notifications
│   ├── audit/                  Append-only, hash-verified audit trail
│   ├── api/                    REST API, including both gateways' endpoints
│   ├── dashboard/              Dashboard, Control Tower, Integration Handbook
│   ├── integrations/           Loads the adapter registry at startup
│   └── core/                   Base models, tenancy managers, seed_demo
├── integrations/               Vendor-neutral adapter layer
│   ├── base/                   Adapter contracts and normalized types
│   ├── providers/              openai, generic_rest, mock
│   ├── agents/                 Agent-engine adapters
│   ├── mcp/                    MCP transports: streamable_http, mock
│   ├── a2a/                    A2A transports: jsonrpc, mock
│   └── webhooks/               Webhook payload normalizers
├── templates/                  Server-rendered pages
├── static/                     CSS, JavaScript and images (logo)
├── docs/                       Technical Guide and IRGF alignment
│   └── screenshots/            The screenshots in these documents
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## Security model

- **No credentials in the database.** Providers and MCP servers store only `credential_ref`, the
  name of an environment variable. In gateway mode the platform injects the secret itself, so
  applications and agents never hold upstream credentials.
- **API keys** have the form `act_<prefix>_<secret>`. Only a SHA-256 hash is stored; the raw key is
  shown once. Keys can expire and be revoked, and `last_used_at` shows which are still in use.
- **People**: an organization always keeps an administrator. Invited people set their own password
  through a single-use link. An organization's administrators never change the profile or password
  of someone who also belongs to another organization. Service accounts never hold an administrator
  role. Only superusers create organizations or grant Super Administrator.
- **Tenant isolation** is enforced in the data layer: organization-owned records are always queried
  through an organization-scoped manager.
- **Role-based access** is checked on every page, action and API call, per organization.
- **Audit trail** entries are append-only through the ORM and hashed at write time; run
  `verify_audit_log` to detect tampering made directly against the database.
- **MCP tool governance**: new tools and changed tool definitions wait for review; servers are
  restricted to granted agents by default; the MCP endpoint rejects browser origins nobody
  configured.
- **Agent-to-agent (A2A) governance**: an agent can be sent work only once it is published and
  approved, and a restricted one only by a sender holding a contract. Skills wait for review, and a
  changed skill goes back to it. Every message is held to its contract's data ceiling, with personal
  data raising what it carries. Chains stop at a hop limit and never return to an agent already in
  them. External agents' credentials stay with the platform, and the gateway sends to the endpoint
  governance registered, never to one an agent's card names.
- **Prompt content** is a separate privilege (`prompt.view`), and content from confidential or
  restricted agents is redacted on shared screens.
- **Agent runtime controls (IRGF)**: a Tier 3–4 agent answers only to service accounts bound to it.
  Anyone on call can pull an agent's kill-switch, and only a person can release it. Action limits
  are checked before each tool call. Every action is verified against the authorized configuration
  within a ceiling set per tier.
- **Separation of duties**: from Tier 3, nobody signs a gate decision for a system they own or a
  request they made. Tier 4 takes two different people, and service accounts never sign.
- **Production**: set `DJANGO_DEBUG=0`, a strong `DJANGO_SECRET_KEY`, real `DJANGO_ALLOWED_HOSTS`
  and `DJANGO_CSRF_TRUSTED_ORIGINS`, serve over HTTPS, and use PostgreSQL and Redis.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Error 10061` / connection refused when an action queues a task | No Redis is running. Set `CELERY_TASK_ALWAYS_EAGER=1` for local work, or start Redis. |
| `database is locked` | SQLite under concurrent writes. It is configured for WAL, but use PostgreSQL for anything shared. |
| A provider or MCP server shows **Missing** credential | The variable named by `credential_ref` is not set in the platform's environment. Set it and restart. |
| AI Gateway returns `403 policy_denied` — "No permitted model" | Every candidate model was refused by policy — typically unapproved models, or a model-approval policy with `require_evaluation` and no passing run. Approve or evaluate the model, or adjust the policy. |
| AI Gateway returns `503 no_route` | No active model of the requested type is eligible: check model status, provider health and your routing policy's filters. |
| AI Gateway refuses an agent with "requires human approval" | With the default policies, agents at autonomy 3+ in production need approval, which a gateway call cannot wait for. Lower the autonomy or scope the policy. |
| A gateway returns `403 identity_not_bound` | The key may not act as that agent: it is bound to other agents, or the agent is Tier 3–4 and the key is not bound to it. Use the agent's own service account, or bind this one to it. See [IRGF alignment](docs/irgf-alignment.md#5-agent-identity-chain-link-02). |
| A gateway returns `403 agent_suspended` | The agent's kill-switch was pulled, by a person or by one of its own pre-approved triggers. A person must resume it from the agent's page. |
| A gateway refuses an agent: *has no deployment authorization (IRGF S7 · Gate G3)* | A `deployment_authorization` policy is active, and the agent (or the application the call names) holds no authorization in force for that environment. Request one from the agent's page. *Suspended* means a major change is waiting at Gate G4: decide it under *AI Governance → Changes · G4*. |
| An MCP call comes back `blocked` — *Action limit (link 06)* | The call's value, the day's total, the hourly rate or the counterparty breaks the agent's action limits. If the agent pre-approved the `action_limit_breach` trigger, it is now suspended too. |
| Runs are never verified; the Control Plane looks stale | Celery beat is not running, so neither is the Control Plane sweep. Start beat, or press *Run the Control Plane now*. |
| Every production agent shows structural drift | They run in production without a Gate G3 authorization. Authorize them, or record an M17 exception for the divergence. |
| A hand-off comes back `rejected` — *holds no contract* | The recipient is restricted and the sender has no contract for it. Grant one on the recipient's A2A page (`a2a.approve`), or open the agent to every agent. |
| A hand-off comes back `rejected` — *… data may not be sent to …* | The message is more sensitive than the contract's ceiling or the recipient's clearance. A sender that declares nothing is read as sending its own classification, and personal data in the text makes it at least confidential. Declare what is sent, or send less. |
| A hand-off comes back `rejected` — *changed … after it was reviewed* | The agent's card changed that skill's definition. Review it again in the A2A Skill Catalog. |
| An A2A client gets `404` / `-32050` | Nothing is published at that address in the key's organization, or the agent is not approved. |
| A new A2A contract shows as a change awaiting Gate G4 | Contracts are part of the sender's authorized configuration. Decide it under *AI Governance → Changes · G4*; until then the Control Plane reports traffic along it as outside the agent's boundary. |
| MCP client gets `401` | Send the key as `Authorization: Bearer act_...` or `X-API-Key: act_...`. |
| MCP client gets `403` | The key's role lacks `mcp.invoke`, or the request came from a browser origin that is not allowed. |
| A tool is missing from `tools/list` | The server is not approved, the tool is awaiting review or disabled, the agent has no (unexpired) grant on a restricted server, the tool is on the agent's restricted list, or a deny policy blocks it. |
| Live Monitor stays on "Connecting…" | WebSockets need the ASGI server (Daphne). With `USE_SQLITE=0`, the Redis channel layer must be reachable. |
| Styles missing in production | Run `python manage.py collectstatic` and set `USE_MANIFEST_STATIC=1`. |

For the full developer guide — every endpoint, the event vocabulary, identifier resolution, writing
an adapter and more — open the **Integration Handbook** at `/docs/integration/` in a running
instance.

## License

AI Control Tower is distributed under the [Apache License, Version 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution.
