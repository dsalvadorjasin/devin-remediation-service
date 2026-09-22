# Devin Remediation Service

An event-driven automation that scans a GitHub repository for issues labelled `devin-remediate` and spins up [Devin](https://devin.ai) sessions to fix them — opening pull requests with proposed solutions.

## How it works

0. **Discovery.** On a schedule (`SEMGREP_SCAN_INTERVAL_MINUTES`), a worker clones/updates the target repo, runs Semgrep, and files one `devin-remediate` issue per new finding (dedup by fingerprint). `POST /ingest/semgrep` accepts SARIF from the `semgrep.yml` GitHub workflow as a host-free alternative.
1. On startup, the API enqueues a scan of the GitHub repo for open issues with the `devin-remediate` label. Scans run on a Celery worker.
2. For each issue, a `remediate_issue_task` checks for an existing open PR before creating a Devin session, so restarting the service never triggers duplicate work.
3. If no open PR exists, a Devin session is created with a structured prompt to fix the issue and open a PR.
4. When a GitHub webhook is registered, `POST /webhooks/github` reacts immediately to `issues` (labelled `devin-remediate`) and `pull_request` events, enqueueing remediation or updating task state.
5. Celery Beat repeats the scan every `SCAN_INTERVAL_MINUTES` as a **reconciliation scan**: it re-lists labelled issues and reconciles the store against GitHub's live state, so missed webhook deliveries (or no webhook at all) never drop work.
6. Each running session is tracked by a `poll_session_task` that re-queues itself every 60 seconds (`apply_async(countdown=60)`) until the session finishes. A session is marked completed when Devin reports `status: exit`, `status: running` with `status_detail: finished`, or when a PR is found but the session is still finishing up; it is marked failed on `error` or `suspended`.
7. Poll ownership is durable: each chain holds a `poll_token` and a `poll_lease_until` lease on the task row, renewed on every hop. The reconciliation scan re-arms polling for any `running` session whose lease is missing or expired (worker restart, lost broker message, sessions created before Celery), claiming the lease atomically so concurrent scans never start duplicate chains; a poll carrying a superseded token stops itself.
8. A live dashboard at `http://localhost:8000` shows the status of all tasks.
9. Task state (issue, session, status, PR URL) is persisted in PostgreSQL, so it survives restarts.

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
GITHUB_TOKEN=your_github_personal_access_token
GITHUB_REPO=your_org/target_repo
DEVIN_API_KEY=your_devin_api_key
DEVIN_ORG_ID=your_devin_org_id
SCAN_INTERVAL_MINUTES=5
DATABASE_URL=postgresql+psycopg://remediation:remediation@postgres:5432/remediation
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
ORCHESTRATOR=celery
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here
INGEST_TOKEN=your_ingest_token_here
SEMGREP_SCAN_INTERVAL_MINUTES=60
```

- `GITHUB_TOKEN` — fine-grained personal access token scoped to the target repository. Full permission footprint:
  - **Issues: read and write** — list labelled issues, post comments, create issues from static-analysis findings.
  - **Pull requests: read** — detect existing PRs for an issue.
  - **Contents: read** — clone the (possibly private) target repository for Semgrep scans.
  - **Webhooks: read and write** — programmatic webhook registration.
  - **Metadata: read** — baseline permission required by every fine-grained token.
- `DATABASE_URL` — SQLAlchemy connection string. The default in `.env.example` points at the `postgres` service in `docker-compose.yml`. If unset, the app falls back to a local SQLite file (`sqlite:///./remediation.db`), which is what the unit tests use (in-memory).
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — Redis URLs used by Celery. Defaults point at the `redis` service in `docker-compose.yml`.
- `ORCHESTRATOR` — which `Orchestrator` implementation dispatches work. Only `celery` exists today.
- `GITHUB_WEBHOOK_SECRET` — self-generated shared secret for HMAC (`X-Hub-Signature-256`) verification of webhook deliveries. If unset, `POST /webhooks/github` returns 503 and the service runs on the reconciliation scan alone.
- `INGEST_TOKEN` — self-generated bearer token for `POST /ingest/semgrep` (`Authorization: Bearer ...` or `X-Ingest-Token`). Unset -> 503.
- `SEMGREP_SCAN_INTERVAL_MINUTES` — Beat interval for the Semgrep discovery task. Optional tuning: `SEMGREP_CONFIG` (default `p/default`), `SEMGREP_CHECKOUT_DIR`, `SEMGREP_MAX_FINDINGS` (cap issues filed per run; `0` = unlimited).
- `DEVIN_API_KEY` — API key for Devin (starts with `cog_`), which you can generate following the instructions [here](https://docs.devin.ai/api-reference/getting-started/teams-quickstart#step-2-generate-an-api-key).
- `DEVIN_ORG_ID` - Organization ID for Devin (starts with `org-`), which you can find under `Settings -> General` in [app.devin.ai](app.devin.ai).

`.gitignore` already excludes `.env`, so real secrets stay local; `.env.example` only ever contains placeholders.

> **Note:** Devin must be linked to a GitHub account with **write access** to the target repository so that it can open pull requests on your behalf. If the repository is private, that account also needs read access to clone it.

### 2. Run with Docker

Install Docker Desktop and run the following command in the project root:

```bash
docker compose up --build
```

This starts the following containers (all app containers are built from the same image and differ only by command):

| Service | Image | Role |
|---|---|---|
| `migrate` | this repo | one-shot `alembic upgrade head`; the app services wait for it |
| `api` | this repo | FastAPI ingest/API service + dashboard on `:8000`. Never calls Devin; only enqueues. |
| `ingest-worker` | this repo | `celery worker -Q ingest`: `scan_task` (GitHub listing) and `discovery_task` (clone + Semgrep) |
| `devin-worker` | this repo | `celery worker -Q devin`: `remediate_issue_task` (`devin.create_session`) and `poll_session_task` (status polling) |
| `beat` | this repo | `celery beat`: reconciliation scan + Semgrep discovery schedule (run exactly one) |
| `redis` | `redis:7-alpine` | Celery broker + result backend |
| `postgres` | `postgres:16-alpine` | Task state, persistent `pgdata` volume |

Scale a tier independently, e.g. `docker compose up --scale devin-worker=3`. `GET /healthz` checks database connectivity and backs the compose/k8s health probes.

The dashboard will be available at **http://localhost:8000**.
At startup, the app enqueues a scan for open issues labelled `devin-remediate` in the target repository.
Celery Beat enqueues a scan every `SCAN_INTERVAL_MINUTES` to pick up newly labelled issues.
You can also trigger a manual scan by using the **Scan Issues** button in the dashboard.
If a Devin session fails, you can retry it by clicking the **Retry Failed** button.

## Development with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run the following commands in the project root:

```bash
uv sync
uv run pytest
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
# in separate terminals, with a local Redis on localhost:6379:
uv run celery -A app.celery_app:celery_app worker -Q ingest --loglevel=info
uv run celery -A app.celery_app:celery_app worker -Q devin --loglevel=info
uv run celery -A app.celery_app:celery_app beat --loglevel=info
# or a single worker consuming both queues during development:
uv run celery -A app.celery_app:celery_app worker -Q ingest,devin --loglevel=info
```

`uv sync` installs the runtime dependencies and the `dev` dependency group (which includes the test tools). `uv run pytest` runs the test suite, and `uv run uvicorn app.main:app` starts the local dashboard at **http://localhost:8000**.

The tests set `CELERY_TASK_ALWAYS_EAGER=1`, so Celery tasks execute synchronously in-process and no broker is required.

### Database migrations

Schema changes are managed with Alembic (`alembic/`). Common commands:

```bash
uv run alembic upgrade head                        # apply migrations (uses DATABASE_URL)
uv run alembic revision --autogenerate -m "msg"    # generate a new migration from app/db.py models
```

The unit tests run against an in-memory SQLite database by default. To run them against an ephemeral Postgres instead:

```bash
docker run -d --rm --name pgtest -e POSTGRES_USER=u -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=t -p 55432:5432 postgres:16-alpine
DATABASE_URL=postgresql+psycopg://u:pw@localhost:55432/t uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+psycopg://u:pw@localhost:55432/t uv run pytest
```

### Kubernetes (optional)

`k8s/` contains a kustomize base with the same topology: `remediation-api` (Deployment + Service + Ingress), `remediation-ingest-worker`, `remediation-devin-worker`, `remediation-beat` (replicas=1, `Recreate`), a `remediation-migrate` Job, plus `redis` and a `postgres` StatefulSet. Non-secret config lives in `configmap.yaml`. The `remediation-secrets` Secret is **not** part of the kustomize base (`secret.example.yaml` only documents the expected keys) and must be created out of band:

```bash
kubectl create namespace devin-remediation
kubectl -n devin-remediation create secret generic remediation-secrets --from-env-file=.env
# set your image in `images:` and the Ingress host / TLS secret (or cert-manager issuer) in api.yaml, then:
kubectl apply -k k8s/
```

Migrations: every `kubectl apply -k` re-creates the `remediation-migrate` Job (it is garbage-collected after completion), and each workload has a `wait-for-migrations` init container that blocks until `alembic current` reports head, so new pods never start against an old schema. The Ingress forces HTTPS (`ssl-redirect`) and expects a certificate in `remediation-api-tls`; webhook and ingest secrets must never travel over plain HTTP.

## Architecture decisions

**Stack.** FastAPI + uvicorn for the API server, httpx for HTTP calls to the GitHub and Devin APIs, python-dotenv for configuration, SQLAlchemy + Alembic on PostgreSQL for persistence.

**Persistent storage.** `app/store.py` keeps the original function signatures (`upsert`, `get`, `get_all`, `get_status`, `clear`) but is backed by a `tasks` table (`app/db.py`). On startup the service still re-scans GitHub and treats the live PR state as the source of truth, so a stale database never blocks work.

**Pluggable discovery sources.** `app/discovery/base.py` defines `DiscoverySource.discover() -> list[Finding]` and the `Finding` dataclass (`rule_id`, `message`, `severity`, `file_path`, `start_line`, `snippet`, `fingerprint`). `SemgrepDiscoverySource` clones the target repo (`Contents: read`) with `git clone --depth 1`, runs `semgrep scan --sarif`, and `parse_sarif` normalizes the results. The fingerprint is Semgrep's SARIF `matchBasedId/v1` when present, otherwise `sha256(rule_id | file | line//10 | normalized snippet)`, so a finding that shifts a few lines does not become a new issue. `ingest_findings` looks up existing open issues by the hidden `<!-- semgrep-fingerprint: ... -->` marker and only calls `github.create_issue` for new ones; the ordinary scan/webhook path then remediates those issues unchanged.

Three ways to trigger discovery:

```bash
# 1. Celery Beat: every SEMGREP_SCAN_INTERVAL_MINUTES (automatic)
# 2. Ask a worker to run discovery now (empty body)
curl -X POST http://localhost:8000/ingest/semgrep -H "Authorization: Bearer $INGEST_TOKEN"
# 3. Push SARIF you produced elsewhere (what .github/workflows/semgrep.yml does)
semgrep scan --config p/default --sarif -o out.sarif /path/to/checkout
curl -X POST "http://localhost:8000/ingest/semgrep?dry_run=true" -H "Authorization: Bearer $INGEST_TOKEN" \
     -H "Content-Type: application/json" --data-binary @out.sarif
```

**Webhooks for immediacy, reconciliation scan for eventual consistency.** `app/webhooks.py` verifies the HMAC signature and normalizes `issues` / `pull_request` events into idempotent store updates and orchestrator calls:

| Event | Effect |
|---|---|
| `issues` `labeled`/`opened`/`reopened` with `devin-remediate` | `enqueue_remediation(issue)` (same guards as the scan) |
| `issues` `edited` on a tracked issue | update title / URL |
| `pull_request` `opened`/`reopened` referencing `#N` | tracked `#N` -> `completed` with `pr_url` |
| `pull_request` `closed` unmerged, matching the tracked `pr_url` | re-fetch issue; if still open + labelled -> `failed` and `enqueue_remediation(force_retry=True)` |

Webhook deliveries are best-effort, so the Phase 2 Beat scan is kept as a reconciliation pass. The whole discover -> issue -> session -> PR loop works with no webhook registered.

**Runtime webhook registration.** The public URL is only known once a tunnel is up, so hooks are registered at runtime (Webhooks: read/write scope):

```bash
# no account needed for a Cloudflare quick tunnel
cloudflared tunnel --url http://localhost:8000   # prints https://<random>.trycloudflare.com
uv run python scripts/webhook.py register https://<random>.trycloudflare.com
uv run python scripts/webhook.py list
uv run python scripts/webhook.py delete --all-service   # tunnel is ephemeral: always clean up
```

**Service split along the orchestrator seam.** `app/main.py` mounts two routers: `app/api/read.py` (`GET /`, `/status`, `/status/{n}`, `/healthz` — pure reads, where the Phase 6 SPA attaches) and `app/api/ingest.py` (`POST /scan`, `/webhooks/github`, `/ingest/semgrep` — authenticate, then hand off to the `Orchestrator`). Celery `task_routes` split the tasks across two queues so each deployable owns one concern: the **ingest worker** (`-Q ingest`) talks to GitHub and runs Semgrep; the **Devin worker** (`-Q devin`) is the only process holding a conversation with the Devin API (`create_session` + polling). They share nothing but Redis (broker) and Postgres (state). Because routes only see the `Orchestrator` interface, swapping Celery for Temporal changes neither the API service nor the worker entrypoints' business logic.

**Celery behind a swappable orchestrator.** All background work (scans, per-issue remediation, per-session polling) runs as Celery tasks (`app/tasks.py`) on Redis. The API never talks to Celery directly: it calls `app.orchestrator.get_orchestrator()`, which returns an implementation of the abstract `Orchestrator` interface (`enqueue_scan`, `enqueue_remediation`, `enqueue_discovery`, `schedule_poll`). `CeleryOrchestrator` is the only implementation today; a future `TemporalOrchestrator` can implement the same four methods (with a durable timer replacing the self-re-queuing poll task) without touching routes or the `app/devin.py` / `app/github.py` clients. Business logic lives in `app/remediation.py` and is scheduler-agnostic.

## Delivery: stacked PRs and roadmap

This service was built as five stacked PRs, each branched off the previous one (merge bottom-up; when a lower PR changes, rebase the ones above it):

| # | Branch | Base | Phase |
|---|---|---|---|
| 1 | `phase-1-postgres` | `main` | PostgreSQL persistence via SQLAlchemy + Alembic |
| 2 | `phase-2-celery` | `phase-1-postgres` | Celery worker/beat behind the `Orchestrator` interface |
| 3 | `phase-3-webhooks` | `phase-2-celery` | GitHub webhook ingestion (HMAC) + reconciliation scan |
| 4 | `phase-4-semgrep` | `phase-3-webhooks` | Semgrep discovery source, fingerprint-deduped issue creation, `/ingest/semgrep` |
| 5 | `phase-5-microservices` | `phase-4-semgrep` | api / ingest-worker / devin-worker split, compose + k8s |

**Temporal upgrade path.** Implement `TemporalOrchestrator(Orchestrator)` in `app/orchestrator/` (`enqueue_scan`, `enqueue_remediation`, `enqueue_discovery`, `schedule_poll` — the last one becomes a durable workflow timer instead of a self-re-queuing task), register it in `get_orchestrator()` under `ORCHESTRATOR=temporal`, and replace the Celery worker deployables with Temporal workers running the same `app/remediation.py` functions as activities. Routes, clients and the store are untouched.

**Follow-up sessions (not in this repo yet):** Phase 6 — SPA replacing `templates/index.html` on top of `app/api/read.py`, plus e2e tests; Phase 7 — hardening and CI (lint/typecheck, image build/publish, deploy); Phase 8 — Devin Security Swarm.

## Project structure

```bash
app/
├── main.py         # FastAPI app for the api service: startup (enqueues initial scan), mounts routers
├── api/
│   ├── read.py      # GET /, /status, /status/{n}, /healthz (read layer for the future SPA)
│   └── ingest.py    # POST /scan, /webhooks/github, /ingest/semgrep (hand off to Orchestrator)
├── remediation.py  # Scheduler-agnostic business logic (scan, process issue, poll session)
├── celery_app.py   # Celery app + Beat schedule, configured from env
├── tasks.py        # Celery task wrappers around app/remediation.py
├── orchestrator/
│   ├── base.py                 # Abstract Orchestrator interface
│   ├── celery_orchestrator.py  # CeleryOrchestrator (ORCHESTRATOR=celery)
│   └── __init__.py             # get_orchestrator() factory
├── discovery/
│   ├── base.py      # DiscoverySource interface + Finding dataclass
│   ├── semgrep.py   # SemgrepDiscoverySource + parse_sarif
│   └── ingest.py    # ingest_findings: fingerprint dedup -> github.create_issue
├── webhooks.py     # HMAC verification + issues/pull_request event normalization
├── github.py       # GitHub API client (issues, PRs, comments, webhooks)
├── devin.py        # Devin API client
├── store.py        # Persistent task store (same API as the old in-memory store)
├── db.py           # SQLAlchemy engine/session + Task model
└── templates/
    └── index.html  # Dashboard
alembic/            # Database migrations
scripts/webhook.py  # register / list / delete the repo webhook at runtime
.github/workflows/
├── tests.yml       # uv sync + pytest
└── semgrep.yml     # cron/workflow_dispatch Semgrep -> SARIF -> POST /ingest/semgrep
k8s/                # kustomize manifests: api, ingest-worker, devin-worker, beat, migrate job, redis, postgres
alembic.ini
Dockerfile          # one image; compose/k8s pick the command per service
docker-compose.yml  # migrate, api, ingest-worker, devin-worker, beat, redis, postgres
pyproject.toml
uv.lock
.env.example
```

