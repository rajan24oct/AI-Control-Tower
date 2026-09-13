# Setup guide

This guide covers every way to install and run AI Control Tower, from a five-minute local demo to a
production deployment. For an overview of the product and a tour of every screen, see the
[README](README.md). For the developer guide — APIs, both gateways, sample code — open the
**Integration Handbook** at `/docs/integration/` in a running instance.

## Contents

1. [Choose a setup](#1-choose-a-setup)
2. [Prerequisites](#2-prerequisites)
3. [Option A: local with SQLite](#3-option-a-local-with-sqlite)
4. [Option B: local with PostgreSQL and Redis](#4-option-b-local-with-postgresql-and-redis)
5. [Option C: Docker Compose](#5-option-c-docker-compose)
6. [First sign-in and the demo estate](#6-first-sign-in-and-the-demo-estate)
7. [A clean install for your own organization](#7-a-clean-install-for-your-own-organization)
8. [Connect real model providers](#8-connect-real-model-providers)
9. [Connect MCP servers](#9-connect-mcp-servers)
10. [Give applications an identity](#10-give-applications-an-identity)
11. [Background workers](#11-background-workers)
12. [Production deployment](#12-production-deployment)
13. [Verify the installation](#13-verify-the-installation)
14. [Upgrading](#14-upgrading)
15. [Resetting a local install](#15-resetting-a-local-install)
16. [Troubleshooting setup](#16-troubleshooting-setup)

---

## 1. Choose a setup

| Setup | Use it for | You need |
|---|---|---|
| **A — Local, SQLite** | Exploring, demos, UI work, running the tests | Python 3.12 |
| **B — Local, PostgreSQL + Redis** | Developing against production-shaped services, running Celery workers | Python 3.12, PostgreSQL 16, Redis 7 |
| **C — Docker Compose** | The full stack with one command; closest to production | Docker with Compose v2 |

All three end at the same application on <http://localhost:8000>. Sections 6–11 apply to every
setup.

## 2. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.12 | Options A and B (the Docker image uses `python:3.12-slim`) |
| Git | any recent | Getting the code |
| PostgreSQL | 16 | Option B and production |
| Redis | 7 | Option B and production: Celery broker and results, cache, WebSocket channel layer |
| Docker + Compose v2 | recent | Option C |

Check what you have:

```bash
python --version
git --version
docker compose version
```

**Browser access to CDNs.** Pages load Bootstrap, Bootstrap Icons, jQuery, Chart.js and Google Fonts
from public CDNs (`cdn.jsdelivr.net`, `fonts.googleapis.com`). The browser — not the server — needs
to reach them. For an air-gapped network, copy those assets into `static/` and point
`templates/base.html` at the local copies.

**Windows.** The commands below are given for PowerShell where they differ. Redis has no native
Windows build: run it in Docker or WSL.

---

## 3. Option A: local with SQLite

The quickest path: no database server, no Redis, no Docker.

### Get the code and create a virtual environment

```bash
git clone <your-repository-url> aig-tower
cd aig-tower
python -m venv .venv
```

Activate it — you will need to do this in every new terminal:

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

> If PowerShell refuses to run the activation script, use `cmd` and `.venv\Scripts\activate.bat`,
> or call the interpreter directly: `.venv\Scripts\python.exe manage.py ...`.

### Install the dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Configure

Create `.env` in the project root:

```dotenv
DJANGO_DEBUG=1
USE_SQLITE=1
# No Redis: run background tasks in-process instead of queueing them.
CELERY_TASK_ALWAYS_EAGER=1
```

With `USE_SQLITE=1` the platform uses `db.sqlite3` in the project root (in WAL mode, so readers are
not blocked by a long write), an in-memory WebSocket channel layer and a local-memory cache.
Variables already set in your shell take precedence over `.env`.

### Create the database and run

```bash
python manage.py migrate
python manage.py seed_demo          # optional: the demo estate (section 6)
python manage.py runserver 8000
```

Open <http://localhost:8000>. If you skipped `seed_demo`, follow
[section 7](#7-a-clean-install-for-your-own-organization) to create your organization.

> `runserver` serves the ASGI application through Daphne, so the live monitor's WebSockets work in
> development without a separate process.

---

## 4. Option B: local with PostgreSQL and Redis

Everything from Option A, but against the services production uses — and with real Celery workers.

### PostgreSQL

Create a role and a database (as the `postgres` superuser):

```sql
CREATE ROLE aig_tower WITH LOGIN PASSWORD 'aig_tower';
CREATE DATABASE aig_tower OWNER aig_tower;
```

```bash
psql -U postgres -c "CREATE ROLE aig_tower WITH LOGIN PASSWORD 'aig_tower';"
psql -U postgres -c "CREATE DATABASE aig_tower OWNER aig_tower;"
```

The Python driver ships as `psycopg[binary]` in `requirements.txt`, so no compiler or local libpq is
needed.

### Redis

Any Redis 7 on `localhost:6379` works. The simplest way on any OS:

```bash
docker run -d --name aig-redis -p 6379:6379 redis:7-alpine
```

Or natively: `sudo apt install redis-server` on Debian/Ubuntu, `brew install redis && brew services
start redis` on macOS.

### Configure

```dotenv
DJANGO_DEBUG=1
USE_SQLITE=0

POSTGRES_DB=aig_tower
POSTGRES_USER=aig_tower
POSTGRES_PASSWORD=aig_tower
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
CHANNEL_LAYER_URL=redis://localhost:6379/3
```

Each concern gets its own Redis database, so monitor broadcasts never collide with the cache or the
Celery queues.

### Create the database and run

```bash
python manage.py migrate
python manage.py seed_demo          # optional
python manage.py runserver 8000
```

Then start the workers in two more terminals — see [section 11](#11-background-workers).

---

## 5. Option C: Docker Compose

The Compose file runs PostgreSQL, Redis, the ASGI web server, a Celery worker and Celery beat.

### Configure

```bash
cp .env.example .env              # PowerShell: Copy-Item .env.example .env
```

Edit `.env`:

- set `DJANGO_SECRET_KEY` to a long random value;
- keep `USE_SQLITE=0` and `DJANGO_ALLOWED_HOSTS` including `web`;
- fill in any provider keys you want to use (see [section 8](#8-connect-real-model-providers)).

### Start the stack

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f web
```

On every start, the `web` service runs `migrate` and `collectstatic`, then serves
`aig_tower.asgi:application` with Daphne on port 8000.

### Load data

```bash
docker compose exec web python manage.py seed_demo
docker compose exec web python manage.py createsuperuser
```

Open <http://localhost:8000>.

| Service | Runs | Host port |
|---|---|---|
| `postgres` | PostgreSQL 16; data in the `postgres_data` volume | 5432 |
| `redis` | Redis 7 | 6379 |
| `web` | `migrate`, `collectstatic`, Daphne | 8000 |
| `celery_worker` | `celery -A aig_tower worker` | — |
| `celery_beat` | `celery -A aig_tower beat` | — |

Good to know:

- The project directory is mounted into the containers at `/app`, so code edits are visible
  immediately — but Daphne does not auto-reload. Run `docker compose restart web celery_worker`
  after changing Python code.
- If ports 5432, 6379 or 8000 are already taken on your machine, change the left-hand side of the
  `ports:` mappings in `docker-compose.yml`.
- The Compose file is a development stack: it mounts the source and publishes the database and
  Redis ports. Harden it before using it anywhere shared (see [section 12](#12-production-deployment)).

---

## 6. First sign-in and the demo estate

```bash
python manage.py seed_demo
```

This builds *Northwind Financial Group*: 8 users across every role, 4 teams, providers, models,
agents, applications, 500 mock executions, pricing, budgets and alerts, risks, approval rules and
requests, the seven default control policies with findings, and a governed MCP estate (servers,
reviewed tools, access grants and real gateway traffic).

| Option | Default | Effect |
|---|---|---|
| `--password` | `ControlTower!2026` | Password for every demo user |
| `--superuser` | `admin@northwind.example` | Creates this Django superuser if it does not exist |
| `--executions` | `500` | Mock executions to generate; `0` skips them |

`seed_demo` is safe to re-run: records are updated in place. It does **replace** the mock
execution history each time (unless `--executions 0`).

Sign in as `ava.mitchell@northwind.example` (Organization Administrator) or `admin@northwind.example`
(superuser, for `/admin/`), with the password above. The full list of demo users and what each role
may do is in the [README](README.md#demo-data-accounts-and-roles).

![Sign in](docs/screenshots/01-login.png)

On its **first** run, `seed_demo` also prints an API key for the `langgraph-runtime` service account.
It is shown only once; if you missed it, issue a new one ([section 10](#10-give-applications-an-identity)).

A user who belongs to several organizations switches between them with the selector in the header.
Superusers can see every organization.

---

## 7. A clean install for your own organization

Skip the demo and set up your real organization.

### 1. Create the database and an administrator

```bash
python manage.py migrate
python manage.py createsuperuser        # prompts for an email address and a password
```

### 2. Create the organization

Sign in at `/admin/`, open **Organizations → Add organization**, and fill in the name (the slug
fills itself in), industry and status *Active*. In the **Organization memberships** section at the
bottom, add yourself with the role **Organization Administrator** and tick **Is default**. Save.

![Django admin](docs/screenshots/26-admin-home.png)

A superuser can see every organization even without a membership; everyone else sees only the
organizations they belong to. Give yourself a membership anyway, so your role is explicit and the
organization opens by default.

### 3. Invite your people

Create users under **Users → Add user**, then give each one a membership — either in the
organization's membership section or under **Organization memberships** — with the role that fits.
Roles are per organization. Create **Teams** if you want spend, agents and applications grouped by
team.

### 4. Load the default governance (recommended)

A new organization starts with no control policies and no approval rules. Load the platform's
defaults, then tailor them under *AI Governance → Policies* and in the admin:

```bash
python manage.py shell -c "from apps.organizations.models import Organization; from apps.core.demo_governance import seed_policies, seed_approval_rules; org = Organization.objects.get(name='Your Organization'); print(len(seed_policies(org)), 'policies,', len(seed_approval_rules(org)), 'approval rules')"
```

This is idempotent (it matches by name) and gives you:

- **Policies** — unapproved models may not run in production; restricted and confidential data stays
  with approved providers; providers must be approved and in-region; autonomy is capped at level 3
  in production; a single-run cost ceiling; prohibited tools (`delete_account`, `transfer_funds`,
  `modify_permissions`); and human approval delegated to the approval rules.
- **Approval rules** — high-autonomy agents, critical-risk assets, restricted tools, sensitive
  actions, restricted data in production, unapproved providers or models, and high-cost operations.

> These defaults are enforced by both gateways. Review them before production traffic depends on
> the platform — for example, an agent at autonomy level 3 or above in production will be refused
> gateway calls until the rule is adjusted, because a gateway call cannot wait for a human.

### 5. Onboard the estate

Register, in order: providers ([section 8](#8-connect-real-model-providers)) → models → agents →
applications → MCP servers ([section 9](#9-connect-mcp-servers)) → service accounts
([section 10](#10-give-applications-an-identity)). The Integration Handbook's *Onboarding the
estate* section walks through each with sample API payloads.

---

## 8. Connect real model providers

The database never stores a credential. A provider record names an **environment variable** in
`credential_ref`; the platform reads the secret from its own environment when it calls the provider.

### 1. Put the secret in the platform's environment

```dotenv
OPENAI_API_KEY=sk-...
```

Set it wherever the platform runs — `.env`, your container environment or your secret manager — for
the **web process and the Celery workers** alike (workers run the provider health checks). Restart
them afterwards.

### 2. Choose the adapter

| `adapter_key` | Reaches | Notes |
|---|---|---|
| `openai` | The OpenAI API, or any OpenAI-compatible endpoint you put in `endpoint` | Requires the `openai` package: `pip install openai` (it is not in `requirements.txt`). Requires `credential_ref`. |
| `generic_rest` | Any JSON-over-HTTP inference service | Driven entirely by the provider's `options`; the credential is optional. |
| `mock` | Nothing — deterministic answers | For demos and tests. |

`GET /api/v1/adapters/` lists every adapter this installation has loaded.

### 3. Register the provider

In the admin: **AI providers → Add AI provider**. Choose the organization, a name, the provider type
(a label), the adapter key, endpoint, region, authentication method and `credential_ref`.

![Add a provider](docs/screenshots/29-admin-add-provider.png)

For `generic_rest`, the `options` field describes the service. Every key is optional; these are the
defaults for an OpenAI-shaped API:

```json
{
  "completion_path": "v1/chat/completions",
  "response_text_path": "choices.0.message.content",
  "input_tokens_path": "usage.prompt_tokens",
  "output_tokens_path": "usage.completion_tokens",
  "health_path": "health",
  "auth_header": "Authorization",
  "auth_scheme": "Bearer",
  "timeout_seconds": 30,
  "headers": {"X-Team": "ai-platform"}
}
```

### 4. Check it

Select the provider in the admin list and run **Run health check**, then **Refresh capabilities from
adapter**. The registry shows its health, and whether the credential is **Resolved** or **Missing**.

![Providers registry](docs/screenshots/06-providers.png)

Governance then ticks **Is approved** — the default provider policy requires it for production.

### 5. Register its models

**AI models → Add AI model**: the provider, a display name, the `model_id` exactly as the provider
expects it, the type, context window and prices per 1,000 tokens (they drive cost reporting and
routing). Approve each with the admin action **Approve for use**. If a model-approval policy
requires evaluation evidence, run a suite in the Evaluation Centre first.

### 6. Smoke-test through the AI Gateway

```bash
curl -X POST http://localhost:8000/api/v1/gateway/chat/ \
  -H "X-API-Key: $CONTROL_TOWER_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "Reply with the word ready.", "model": "<model_id>"}'
```

A `200` names the model and provider that served. A `403`, `503` or `502` explains itself in the
body — see the troubleshooting table below.

---

## 9. Connect MCP servers

The MCP Gateway reaches servers over **Streamable HTTP**. Servers using stdio or the legacy HTTP+SSE
transport can be registered for inventory and governance, but the gateway will not call them.

### 1. Put the server's credential in the platform's environment

```dotenv
GITHUB_MCP_TOKEN=...
```

### 2. Register the server

**MCP Gateway → Server Registry → Register server** (or **MCP servers → Add** in the admin):

- **Name** — its slug becomes the prefix of every tool it contributes (`github__create_issue`).
- **Transport** — *Streamable HTTP*, and the server's **endpoint** URL.
- **Auth method** — *None*, *Bearer token* or *API key header* (then set **Auth header**, default
  `X-API-Key`), and **Credential ref** naming the variable from step 1.
- **Access mode** — *Only agents with a grant* (the default) or *Any agent in the organization*.
- **Data classification**, **risk level**, **owner** and **team** for governance.
- **Options** — static, non-secret headers if the server needs them: `{"headers": {"X-Tenant": "acme"}}`.

![Register an MCP server](docs/screenshots/28-admin-register-mcp-server.png)

### 3. Bring it into service

On the server's page, in order: **Health check** → **Approve** (needs `mcp.approve`) → **Sync
tools** → review each tool (**Enable** or **Disable**) → **Grant an agent** access if the server is
restricted.

![MCP server](docs/screenshots/14-mcp-server.png)

New tools arrive *awaiting review*, and a tool whose definition changes upstream goes back to review
automatically. Set `MCP_GATEWAY_REVIEW_NEW_TOOLS=0` only if you really mean to trust every upstream.

### 4. Check what an agent will see

```bash
curl -H "X-API-Key: $CONTROL_TOWER_KEY" \
  "http://localhost:8000/api/v1/mcp-gateway/tools/?agent=<agent-id>"
```

---

## 10. Give applications an identity

Every application or engine gets its own service account — never a shared one — so every call is
attributable and a key can be rotated without touching anything else.

1. In the admin, open **Service accounts → Add service account**. Choose the organization, a name,
   the external system it represents, and a role. The default, **AI Developer**, grants
   `telemetry.ingest`, `gateway.invoke` and `mcp.invoke`: everything an application needs.
2. Back in the list, tick the account, choose **Issue a new API key** and press **Run**. The key
   (`act_<prefix>_<secret>`) appears once, in the message at the top of the page. Only a hash is
   stored — copy it straight into your secret manager.

![Issue an API key](docs/screenshots/27-admin-service-accounts.png)

Send it as `X-API-Key: act_...`, `Authorization: Api-Key act_...`, or — on the MCP endpoint —
`Authorization: Bearer act_...`.

**Rotating a key:** issue a new one, deploy it, then revoke the old one under **API keys** with
**Revoke selected keys**. Keys can also be given an **Expires at** date, and **Last used at** shows
which ones are still in use.

---

## 11. Background workers

Celery runs scheduled housekeeping and work the UI queues. It needs Redis (Option B, C or
production).

```bash
celery -A aig_tower worker --loglevel=info
celery -A aig_tower beat --loglevel=info
```

- **Windows:** add `--pool=solo` to the worker.
- Run **exactly one** `beat` process per deployment; run as many workers as you need.
- Without Redis, don't start them — set `CELERY_TASK_ALWAYS_EAGER=1` so queued work runs in-process.

The schedule lives in [`aig_tower/celery.py`](aig_tower/celery.py): provider health checks every
5 minutes, budget threshold checks every 15, cost rollups hourly, expiry of stale approvals every
10, reminders for expiring approvals every 30, and overdue risk-review reminders daily.

---

## 12. Production deployment

### Environment

```dotenv
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<long random value>
DJANGO_ALLOWED_HOSTS=tower.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://tower.example.com
SITE_URL=https://tower.example.com
USE_MANIFEST_STATIC=1

USE_SQLITE=0
POSTGRES_DB=aig_tower
POSTGRES_USER=aig_tower
POSTGRES_PASSWORD=<strong password>
POSTGRES_HOST=<database host>
POSTGRES_PORT=5432

REDIS_URL=redis://<redis host>:6379/0
CELERY_BROKER_URL=redis://<redis host>:6379/1
CELERY_RESULT_BACKEND=redis://<redis host>:6379/2
CHANNEL_LAYER_URL=redis://<redis host>:6379/3
```

Generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

With `DJANGO_DEBUG=0` the platform turns on secure cookies, HSTS, `nosniff` and
`X-Frame-Options: DENY` by itself.

### Release steps

```bash
pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check --deploy
```

### Processes

| Process | Command | How many |
|---|---|---|
| Web (ASGI) | `daphne -b 0.0.0.0 -p 8000 aig_tower.asgi:application` | One or more |
| Worker | `celery -A aig_tower worker --loglevel=info` | One or more |
| Scheduler | `celery -A aig_tower beat --loglevel=info` | Exactly one |

Serve the web tier with Daphne (as the Dockerfile does), not a plain WSGI server: the live monitor
needs WebSockets. Several web instances can run side by side — circuit-breaker state is in
PostgreSQL and WebSocket broadcasts go through Redis.

### Reverse proxy

Terminate TLS at a proxy and forward both HTTP and WebSocket traffic. An nginx example:

```nginx
server {
    listen 443 ssl;
    server_name tower.example.com;
    # ssl_certificate     /etc/ssl/tower.crt;
    # ssl_certificate_key /etc/ssl/tower.key;

    # Live monitor WebSockets: /ws/executions/<organization-id>/
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 1h;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # The AI Gateway streams server-sent events; do not buffer them.
        proxy_buffering off;
        # Longer than the gateway's own 60-second request deadline.
        proxy_read_timeout 120s;
    }
}
```

- WebSocket connections are origin-checked against `DJANGO_ALLOWED_HOSTS`, so the public host name
  must be in it.
- Form posts arriving through an HTTPS proxy are accepted because the `https://` origin is listed in
  `DJANGO_CSRF_TRUSTED_ORIGINS`. If you also want Django itself to treat forwarded requests as
  HTTPS (for absolute URLs it generates), add
  `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` to `aig_tower/settings.py`.

### Email

Notification emails go to the console by default. `DJANGO_EMAIL_BACKEND` switches the backend, but
SMTP connection details (`EMAIL_HOST`, `EMAIL_PORT`, credentials, TLS) are not read from the
environment today — add them to `aig_tower/settings.py` when you move to SMTP.

### Checklist

- [ ] `DJANGO_DEBUG=0` and a unique `DJANGO_SECRET_KEY`
- [ ] Real host names in `DJANGO_ALLOWED_HOSTS` and `https://` origins in `DJANGO_CSRF_TRUSTED_ORIGINS`
- [ ] PostgreSQL and Redis, not SQLite and in-memory layers
- [ ] HTTPS in front of everything, with WebSocket upgrade forwarded for `/ws/`
- [ ] Provider and MCP credentials in a secret store, exposed as the variables named by `credential_ref`
- [ ] One Celery beat, at least one worker
- [ ] `python manage.py check --deploy` passes
- [ ] Default policies reviewed for your organization
- [ ] Database backups (`pg_dump`) scheduled — all durable state lives in PostgreSQL; Redis holds
      only queues, cache and broadcasts
- [ ] `python manage.py verify_audit_log --fail-on-tamper` scheduled, if you want tamper checks alerted

---

## 13. Verify the installation

```bash
python manage.py check
curl http://localhost:8000/api/v1/health/
```

The health check answers `{"status": "ok", "service": "ai-control-tower"}` without authentication.
With a service-account key:

```bash
# Adapters this installation can reach, MCP transports included
curl -H "X-API-Key: $CONTROL_TOWER_KEY" http://localhost:8000/api/v1/adapters/

# The MCP Gateway answers the protocol handshake
curl http://localhost:8000/api/v1/mcp/ \
  -H "Authorization: Bearer $CONTROL_TOWER_KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "ping"}'
```

In the UI, the **AI Gateway** overview flags any active provider whose credential the platform
cannot resolve, and the **MCP Gateway** overview does the same for MCP servers.

![AI Gateway overview](docs/screenshots/11-ai-gateway.png)

Finally, run the test suite — it uses its own throwaway database:

```bash
python manage.py test
```

---

## 14. Upgrading

```bash
git pull
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput      # production
```

Then restart the web processes, the workers and beat. With Docker Compose:

```bash
git pull
docker compose up --build -d
```

— the `web` service applies migrations and collects static files on start.

---

## 15. Resetting a local install

**SQLite:** stop the server, delete the database files, and rebuild:

```bash
# macOS / Linux
rm -f db.sqlite3 db.sqlite3-wal db.sqlite3-shm
```

```powershell
# Windows (PowerShell)
Remove-Item db.sqlite3, db.sqlite3-wal, db.sqlite3-shm -ErrorAction SilentlyContinue
```

```bash
python manage.py migrate
python manage.py seed_demo
```

**Docker Compose:** `docker compose down -v` removes the containers *and* the PostgreSQL volume;
`docker compose up --build -d` starts again from empty.

To refresh only the demo telemetry, without touching anything else:

```bash
python manage.py generate_executions --purge
```

---

## 16. Troubleshooting setup

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'django'` | The virtual environment is not active in this terminal. Activate it (section 3). |
| `psycopg.OperationalError: connection ... refused` | `USE_SQLITE=0` but PostgreSQL is not reachable. Locally the host is `localhost`; inside Compose it is `postgres`. |
| Connection refused / `Error 10061` when an action queues work | Redis is not running. Start it, or set `CELERY_TASK_ALWAYS_EAGER=1`. |
| `DisallowedHost` / *Bad Request (400)* | Add the host name to `DJANGO_ALLOWED_HOSTS`. |
| *CSRF verification failed* behind a proxy | Add the public `https://` origin to `DJANGO_CSRF_TRUSTED_ORIGINS`. |
| `ValueError: Missing staticfiles manifest entry` | `USE_MANIFEST_STATIC=1` needs `python manage.py collectstatic` to have run. |
| Pages load without styles or icons | Production: run `collectstatic`. Anywhere: the browser cannot reach the CDNs (section 2). |
| Live Monitor stays on *Connecting…* | Serve with Daphne; forward WebSocket upgrades for `/ws/`; include the host in `DJANGO_ALLOWED_HOSTS`; with `USE_SQLITE=0`, Redis must be reachable. |
| A provider or MCP server shows **Missing** credential | The variable named by `credential_ref` is not in the platform's environment. Set it for web and workers, then restart. |
| A provider health check says *The 'openai' package is required* | `pip install openai` in the platform's environment, then restart. |
| `database is locked` | SQLite under concurrent writes. Use PostgreSQL (Option B or C) for anything shared. |
| `seed_demo` changed my execution history | It regenerates mock executions on every run. Use `--executions 0` to leave them alone. |
| No emails arrive | The default backend prints them to the console. See *Email* in section 12. |
| Celery worker exits immediately on Windows | Add `--pool=solo`. |

Still stuck? The Integration Handbook's *Troubleshooting* and *Security model* sections cover the
gateways and API in more depth.
