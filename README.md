

---

AI Control Tower is a Django platform that gives an organization a single control plane for its AI
estate. It keeps a registry of every provider, model, agent, application and MCP server; watches
every run; and — when you put it in the request path — enforces policy, budgets, routing and tool
access on live model and tool calls, while holding the upstream credentials so applications never do.

![Dashboard](docs/screenshots/02-dashboard.png)

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

---

## What's inside

| Module | What it gives you | Where |
|---|---|---|
| **Dashboard & Control Tower** | Executive KPIs for a chosen period, and an end-to-end map of the estate with everything that needs attention | `/`, `/control-tower/` |
| **AI Landscape** | Registries for applications, agents, models and platform providers — owners, risk, environments, pricing, health | `/inventory/` |
| **Observability** | Every run with its timeline, tokens, cost and latency; a live monitor pushed over WebSockets | `/executions/` |
| **AI Gateway** | In-path model calls: policy and budget enforcement, cost-aware routing, fallback chains, circuit breakers, streaming, agent-to-agent messaging | `/gateway/`, `/routing/` |
| **MCP Gateway** | One registry for every MCP server, tool review and re-review, per-agent access grants, and a single governed MCP endpoint for all of them | `/mcp-gateway/` |
| **AI FinOps** | Spend by period, provider, model and team; budgets with threshold alerts and optional hard stops | `/costs/` |
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
        telemetry["Telemetry API<br/>/api/v1/executions/"]
        governance["Policies, budgets,<br/>approvals, audit"]
        registry["Registries"]
        ui["Web UI"]
    end

    app --> aigw
    app --> mcpgw
    ide --> mcpgw
    engine --> telemetry
    aigw --> providers["Model providers"]
    mcpgw --> servers["MCP servers"]
    aigw -. checks .-> governance
    mcpgw -. checks .-> governance
    registry --- db[("PostgreSQL")]
    governance --- db
    ui --- db
    tower --- redis[("Redis: Celery, Channels, cache")]
```

The platform runs in two modes, side by side:

- **Reporting mode** — your engines keep calling their providers directly and report what they did
  to the telemetry API. The Tower observes and advises; it is never in the request path.
- **Gateway mode** — applications send model calls to the AI Gateway and tool calls to the MCP
  Gateway. Policy verdicts become real refusals, budgets become real stops, and every call is
  recorded.

Everything vendor-specific sits behind an adapter registry in [`integrations/`](integrations/):
providers (`openai`, `generic_rest`, `mock`), agent engines, webhook normalizers and MCP transports
(`streamable_http`, `mock`). Adding a vendor means registering one class, not editing call sites.

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
providers, models, agents, applications, 500 executions, budgets, risks, policies, approvals and a
governed MCP estate with real gateway traffic. It also puts the IRGF lifecycle to work: risk
classifications, deployment authorizations for agents and one application, a change waiting at
Gate G4, an exception, and the drift the Control Plane finds. It is idempotent; run it again
whenever you like.

On its first run it also prints a **service-account API key** — the only time that key is shown.
Copy it: it is how external code talks to the API and both gateways. (Lost it? Open the profile
menu → *Integrations*, select the `langgraph-runtime` service account and run
*Issue a new API key*.)

### 4. Run

```bash
python manage.py runserver 8000
```

Open <http://localhost:8000> and sign in:

| Email | Password | Role |
|---|---|---|
| `ava.mitchell@northwind.example` | `ControlTower!2026` | Organization Administrator (sees everything) |
| `admin@northwind.example` | `ControlTower!2026` | Django superuser (for `/admin/`) |

![Sign in](docs/screenshots/01-login.png)

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

### Content display

| Variable | Default | Purpose |
|---|---|---|
| `PROMPT_REDACT_CLASSIFICATIONS` | `confidential,restricted` | Prompt and completion text for agents with these classifications is redacted on screen. |

### Provider and MCP server credentials

The database **never** stores a credential. Each provider and MCP server record holds a
`credential_ref` — the *name* of an environment variable — and the platform resolves the secret at
call time. Set those variables in the platform's environment, for example:

```dotenv
OPENAI_API_KEY=sk-...
AZURE_OPENAI_API_KEY=...
GITHUB_MCP_TOKEN=...
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
| `admin@northwind.example` | Platform Admin | — | Django superuser |

Signing in as different people is the quickest way to see role-based access at work.

### What each role may do

Permissions are organization-scoped: the same person can be an Approver in one organization and a
Viewer in another.

| Role | Beyond read access, may… |
|---|---|
| Organization Administrator / Super Administrator | Everything, including the second signature on Tier 4 gate decisions, as the Enterprise Governance Sponsor. |
| AI Platform Administrator | Manage the inventory, providers, models, agents and applications; score their risk classification; request deployment authorizations and exceptions; ingest telemetry; manage costs; read prompts; call the AI Gateway; manage and call the MCP Gateway; use the agent kill-switch. |
| AI Developer | Manage agents and applications; score their risk classification; request deployment authorizations and exceptions; ingest telemetry; read prompts; call both gateways; use the agent kill-switch. |
| AI Governance Manager | Decide IRGF gates and exceptions as the AI Governance Body; approve models; approve MCP servers, review tools and mark their action-limit arguments; manage policies and risks; score and countersign risk classifications; resolve drift and run the Control Plane; decide approvals; read the audit trail and prompts; use the agent kill-switch. |
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

*Screenshots are taken from the seeded demo organization.*

### Dashboard and Control Tower

The **Dashboard** is the executive view for any period — applications, agents, runs, spend,
latency, policy denials, pending approvals, open risks and provider incidents. The **Control Tower**
maps the estate end to end and leads with what needs a person.

![Control Tower](docs/screenshots/03-control-tower.png)

### AI Landscape — the registries

Every AI asset, with its owner, environment, criticality, risk and status.

![Applications registry](docs/screenshots/04-applications.png)

An agent's page shows its execution chain, the boundaries it runs within (allowed and restricted
tools, knowledge sources) and the applications that use it. It also carries the agent's IRGF record:
its risk tier, deployment authorization, Control Plane findings, the eight-link governance chain, its
Agent Card and the kill-switch. An application's page adds its composition: member agents, the
delegations between them and the authority they reach together.

![Agent detail](docs/screenshots/05-agent-detail.png)

Providers show their adapter, region, health and whether the platform can resolve their credential;
models show pricing, context window, approval and risk.

![Providers registry](docs/screenshots/06-providers.png)

![Models registry](docs/screenshots/07-models.png)

### Observability

Every run reported by an engine or served by a gateway, filterable by status, agent, application,
provider, environment and period.

![Executions](docs/screenshots/08-executions.png)

Each execution has a normalized event timeline — prompts, model calls, tool calls, approvals,
policy violations — with tokens, duration and cost. The **Live Monitor** (`/executions/live/`)
streams runs in flight over WebSockets.

![Execution detail](docs/screenshots/09-execution-detail.png)

### AI Gateway

What the gateway is doing to live traffic: requests, fallbacks, latency, spend, provider circuit
breakers and routing outcomes — plus a warning for any provider whose credential cannot be resolved.

![AI Gateway overview](docs/screenshots/11-ai-gateway.png)

The **Routing Simulator** shows which models the platform would choose right now for a given type
and environment, in what order, and why the others were excluded.

![Routing simulator](docs/screenshots/12-routing-simulator.png)

### MCP Gateway

Every MCP server in one registry, reached through one governed endpoint. The overview leads with
what needs a decision: servers out of rotation, tools awaiting review (including tools whose
definition changed after approval), unapproved servers and missing credentials.

![MCP Gateway overview](docs/screenshots/13-mcp-overview.png)

A server's page holds its connection, governance, health and breaker state, its tools with review
actions, and the agents granted access.

![MCP server detail](docs/screenshots/14-mcp-server.png)

The **Tool Catalog** lists every tool from every server and what governance decided about it; the
*Awaiting review* tab is the review queue.

![MCP tool catalog](docs/screenshots/15-mcp-tool-catalog.png)

**Tool Calls** is the log of everything brokered — and everything refused, with the reason.

![MCP tool calls](docs/screenshots/16-mcp-tool-calls.png)

### AI FinOps

Spend today, this week and this month, a straight-line projection, budget alerts, and spend over
time and by provider, model and team.

![Costs](docs/screenshots/17-costs.png)

![Budgets](docs/screenshots/18-budgets.png)

### AI Governance

The risk register with a 5×5 likelihood-by-impact heat map, inherent and residual.

![Risk matrix](docs/screenshots/19-risk-matrix.png)

The **Approval Centre** holds actions waiting on a human decision, and the rules that send them there.

![Approval Centre](docs/screenshots/20-approvals.png)

**Policies** are typed controls — model approval, provider, data classification, agent autonomy,
cost limit, tool access, human approval — that can deny, require approval or warn. Both gateways
enforce them.

![Policies](docs/screenshots/21-policies.png)

The **Evaluation Centre** runs scored suites and red-team cases against models and agents; policies
can require passing evidence before a model may serve.

![Evaluation Centre](docs/screenshots/22-evaluations.png)

### IRGF lifecycle

The platform applies the [Intent-to-Runtime Governance Framework](docs/irgf-alignment.md) (IRGF).
**Risk Classification** (stage S2) scores every agent and application on five dimensions and derives
a tier from 1 to 4, which someone outside the delivery line countersigns. The tier then decides which
policies apply, who signs each gate and how fast drift must be answered.

![Risk classification register](docs/screenshots/30-irgf-register.png)

**Deployment Authorizations** (Gate G3) authorize an agent or an application as one configuration,
at one tier, for one owner. An application is authorized with its composite authority: everything
its agents can reach between them. Tier 4 takes two signatures from two different people.
**Changes** (Gate G4) then catch every edit to what was authorized and route it by IRGF's change
table. **Exceptions** (M17) set one policy aside for at most 90 days.

![Deployment authorizations](docs/screenshots/46-systems-register.png)

The **Control Plane** (IRGF M12) compares what every agent and application actually does with what
Gate G3 authorized. Each divergence becomes a drift record with a response window set by its category
and tier (M13). Action limits are enforced before each tool call, and every action is verified after
it. See [IRGF alignment](docs/irgf-alignment.md#11-phase-3-the-control-plane).

![Control Plane](docs/screenshots/40-control-plane.png)

### Control & Settings

Audit trail, users, teams, integrations (service accounts and API keys), Django settings and the
Integration Handbook live under the profile menu.

![Profile menu](docs/screenshots/24-profile-menu.png)

The **Audit Trail** is append-only; every entry carries a content hash so a change made directly in
the database is still detectable.

![Audit trail](docs/screenshots/23-audit-trail.png)

### Integration Handbook

The developer guide at `/docs/integration/`: onboarding, the API reference, both gateways, sample
code for Python, TypeScript and common agent frameworks, security model and troubleshooting.

![Integration Handbook](docs/screenshots/25-integration-handbook.png)

---

## Onboarding your AI estate

Register in dependency order — each record points at the ones before it. Steps 1–4 can be done in
the admin (`/admin/`) or over the REST API; the rest happen in the web UI and the admin. The
[Integration Handbook](http://localhost:8000/docs/integration/#estate) walks through each with
sample payloads.

1. **Platform provider** — name, `adapter_key` (`openai`, `generic_rest`, `mock`; see
   `GET /api/v1/adapters/`), endpoint, region and `credential_ref`. Set the variable, then run
   *Run health check* and *Refresh capabilities from adapter* from the admin. Governance marks it
   approved.
2. **Models** — `model_id`, type, context window and per-1K pricing (pricing drives cost and
   routing). Approve with the admin action *Approve for use*; if a policy requires evaluation
   evidence, run a suite in the Evaluation Centre first.
3. **Agent** — a stable `agent_id` (the identifier your code sends), framework, provider, primary
   and fallback models, environment, risk, autonomy level and data classification.
4. **Application** — owners, criticality, data classification, review date, and the agents, models
   and providers it uses.
5. **Identity** — create a service account for the application (profile menu → *Integrations*) and
   run *Issue a new API key*. The key is shown once; store it as a secret. Bind the account to the
   agents it runs (*Acts as* in the admin): from Tier 3, the gateways refuse an agent any key that is
   not bound to it.
6. **MCP servers** — *MCP Gateway → Server Registry → Register server*. Choose Streamable HTTP, set
   the endpoint and `credential_ref`, then **Health check → Approve → Sync tools → review each
   tool → grant agents**. For a tool that moves value, mark which argument carries the amount and
   which the counterparty (*Action-limit arguments*), so agents' action limits can hold it.
7. **Guardrails** — routing policies (optional: without one, the gateway ranks every active model of
   the requested type and offers a three-model fallback chain), control policies and budgets.
8. **Classify (IRGF S2)** — score each agent and application under *AI Governance → Risk
   Classification*, and have someone outside its delivery line countersign. Fill in each acting
   agent's Agent Card: what it is not for, action limits, escalation thresholds and kill-switch
   conditions.
9. **Authorize (IRGF Gate G3)** — *Request authorization* on each agent's page, then on the
   application's. The request page lists the entry criteria for the tier; the decision rights say
   who signs. To make the gateways refuse anything that skipped S2 or G3, add a `risk_classification`
   and a `deployment_authorization` policy with effect `deny`. The defaults include neither.
10. **Prove it** — a gateway call naming the agent returns `200`, the MCP catalog lists the tools
    you enabled, both show up under Executions and MCP Tool Calls, and the Control Plane lists them
    as verified against the authorization.

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
| `/gateway/messages/` and `/gateway/messages/{agent}/` | POST, GET | Agent-to-agent mailbox | `gateway.invoke` |
| `/mcp/` and `/mcp/{agent}/` | POST | The MCP Gateway as an MCP server | `mcp.invoke` |
| `/mcp-gateway/tools/` | GET | An agent's governed tool catalog | `mcp.invoke` |
| `/mcp-gateway/tools/call/` | POST | One governed tool call over REST | `mcp.invoke` |

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

Tool calls and agent-to-agent messages are verified as they are recorded, with or without beat.
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
│   ├── inventory/              Registry pages (AI Landscape)
│   ├── executions/             Runs, events, live monitor (Channels)
│   ├── telemetry/              Telemetry ingestion
│   ├── gateway/                AI Gateway: pipeline, circuit breakers, agent messaging
│   ├── routing/                Routing policies, simulator and decisions
│   ├── mcp_gateway/            MCP registry, discovery, tool review, unified MCP endpoint
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
- **Tenant isolation** is enforced in the data layer: organization-owned records are always queried
  through an organization-scoped manager.
- **Role-based access** is checked on every page, action and API call, per organization.
- **Audit trail** entries are append-only through the ORM and hashed at write time; run
  `verify_audit_log` to detect tampering made directly against the database.
- **MCP tool governance**: new tools and changed tool definitions wait for review; servers are
  restricted to granted agents by default; the MCP endpoint rejects browser origins nobody
  configured.
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
| MCP client gets `401` | Send the key as `Authorization: Bearer act_...` or `X-API-Key: act_...`. |
| MCP client gets `403` | The key's role lacks `mcp.invoke`, or the request came from a browser origin that is not allowed. |
| A tool is missing from `tools/list` | The server is not approved, the tool is awaiting review or disabled, the agent has no (unexpired) grant on a restricted server, the tool is on the agent's restricted list, or a deny policy blocks it. |
| Live Monitor stays on "Connecting…" | WebSockets need the ASGI server (Daphne). With `USE_SQLITE=0`, the Redis channel layer must be reachable. |
| Styles missing in production | Run `python manage.py collectstatic` and set `USE_MANIFEST_STATIC=1`. |

For the full developer guide — every endpoint, the event vocabulary, identifier resolution, writing
an adapter and more — open the **Integration Handbook** at `/docs/integration/` in a running
instance.
