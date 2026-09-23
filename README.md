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
8. A live React dashboard at `http://localhost:5173` (`frontend/`, served separately from the API) shows the status of all tasks; the legacy inline dashboard remains at `http://localhost:8000`.
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
- `INGEST_TOKEN` — self-generated bearer token for `POST /ingest/semgrep` and `POST /scan` (`Authorization: Bearer ...` or `X-Ingest-Token`). Unset -> 503. The dashboard prompts for it once per browser session before triggering a scan.
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
| `frontend` | `frontend/Dockerfile` | React SPA built with Vite, served by nginx on `:5173`; proxies `/status`, `/status/{n}`, `/healthz` to `api` (same origin, no CORS) |
| `redis` | `redis:7-alpine` | Celery broker + result backend |
| `postgres` | `postgres:16-alpine` | Task state, persistent `pgdata` volume |

Scale a tier independently, e.g. `docker compose up --scale devin-worker=3`. `GET /healthz` checks database connectivity and backs the compose/k8s health probes.

The React dashboard will be available at **http://localhost:5173** (legacy inline dashboard at **http://localhost:8000**).
At startup, the app enqueues a scan for open issues labelled `devin-remediate` in the target repository.
Celery Beat enqueues a scan every `SCAN_INTERVAL_MINUTES` to pick up newly labelled issues.
You can also trigger a manual scan by using the **Scan Issues** button in the dashboard.
If a Devin session fails, you can retry it by clicking the **Retry Failed** button.

## React SPA (`frontend/`)

Vite + React 19 + TypeScript. It consumes **only** the read-API contract below.

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173, proxies /status + /healthz to API_PROXY_TARGET (default http://localhost:8000)
npm run build        # production bundle in frontend/dist/
npm run lint         # oxlint
docker build -t remediation-frontend frontend/   # nginx image used by docker-compose (API_UPSTREAM=http://api:8000)
```

The SPA polls `GET /status` every 5 s (`POLL_MS` in `frontend/src/api/types.ts`) and renders summary counts (running / completed / failed), a per-issue table with issue, Devin session and PR links, status badges and a last-updated label. Requests are same-origin by default (Vite dev proxy / nginx); set `VITE_API_BASE_URL` at build time to target a remote API and `CORS_ALLOW_ORIGINS` on the API for read-only CORS.

### Read-API contract

Frozen in `contracts/read-api.openapi.yaml`, mirrored by `frontend/src/api/types.ts` (`TaskView`), and enforced by `tests/test_read_api_contract.py`.

| Endpoint | Response |
|---|---|
| `GET /status` | `TaskView[]` sorted by `issue_number` ascending |
| `GET /status/{issue_number}` | `TaskView` or `404 {"detail": "not tracked"}` |
| `GET /healthz` | `{"ok": true}` |
| `GET /readyz` | `{"ready": true, "checks": {"database": "ok", "broker": "ok"}}` (503 when either check fails) |
| `GET /metrics` | Prometheus text exposition |

`TaskView` has exactly: `issue_number` (int), `title`, `issue_url`, `session_id` (str\|null), `session_url` (str\|null), `status` (`running`\|`completed`\|`failed`), `pr_url` (str\|null), `created_at`, `updated_at` (ISO-8601). The internal poll-lease columns `poll_token` / `poll_lease_until` are stripped by the read layer and never reach the UI.

### End-to-end tests (`e2e/`)

Playwright (chromium) drives the SPA with `GET /status` mocked via `page.route`, so no API or credentials are needed. Every test records screenshots **and** video.

```bash
cd e2e
npm ci
npx playwright install --with-deps chromium
npx playwright test      # starts the Vite dev server on :5173 automatically
npm run report           # open the HTML report
```

Covers the empty state, a populated table with running/completed/failed rows, issue/session/PR links, and auto-refresh. Artifacts: `e2e/test-results/<test>/` (named `*.png` screenshots + `video.webm`, traces on failure) and `e2e/playwright-report/`. CI (`.github/workflows/tests.yml`, job `e2e`) uploads them as the `playwright-screenshots-videos` and `playwright-report` artifacts on every run.

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
kubectl -n devin-remediation create secret generic remediation-secrets --from-env-file=.env  # incl. POSTGRES_PASSWORD + DATABASE_URL
# set your image in `images:` and the Ingress host / TLS secret (or cert-manager issuer) in api.yaml, then:
k8s/deploy.sh ghcr.io/<org>/devin-remediation-service:<unique-tag-or-digest> devin-remediation
```

Releases go through `k8s/deploy.sh <image-ref> [namespace]`: it renders a temporary kustomize overlay pinning that namespace and an immutable image (unique tag or digest; `:latest` is refused since re-applying it never triggers a rollout), deletes the previous (immutable) `remediation-migrate` Job, applies the overlay, waits for the new Job to complete, then waits for the rollouts. Each workload additionally has a `wait-for-migrations` init container that blocks until `alembic current` reports head, so new pods never start against an old schema even if the script is bypassed. Redis runs as a StatefulSet with AOF persistence so queued Celery messages survive a pod replacement; Postgres takes its password (and the app its `DATABASE_URL`) from `remediation-secrets`, not the ConfigMap. The Ingress forces HTTPS (`ssl-redirect`) and expects a certificate in `remediation-api-tls`; webhook and ingest secrets must never travel over plain HTTP.

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

This service was built as six PRs: phases 1-5 stacked, each branched off the previous one (merge bottom-up; when a lower PR changes, rebase the ones above it):

| # | Branch | Base | Phase |
|---|---|---|---|
| 1 | `phase-1-postgres` | `main` | PostgreSQL persistence via SQLAlchemy + Alembic |
| 2 | `phase-2-celery` | `phase-1-postgres` | Celery worker/beat behind the `Orchestrator` interface |
| 3 | `phase-3-webhooks` | `phase-2-celery` | GitHub webhook ingestion (HMAC) + reconciliation scan |
| 4 | `phase-4-semgrep` | `phase-3-webhooks` | Semgrep discovery source, fingerprint-deduped issue creation, `/ingest/semgrep` |
| 5 | `phase-5-microservices` | `phase-4-semgrep` | api / ingest-worker / devin-worker split, compose + k8s |

**Temporal upgrade path.** Implement `TemporalOrchestrator(Orchestrator)` in `app/orchestrator/` (`enqueue_scan`, `enqueue_remediation`, `enqueue_discovery`, `schedule_poll` — the last one becomes a durable workflow timer instead of a self-re-queuing task), register it in `get_orchestrator()` under `ORCHESTRATOR=temporal`, and replace the Celery worker deployables with Temporal workers running the same `app/remediation.py` functions as activities. Routes, clients and the store are untouched.

| 6 | `phase-6-frontend` | `main` | React SPA (`frontend/`), frozen read-API contract, Playwright e2e suite (`e2e/`) — integrated from three parallel lane PRs |

| 7 | `phase-7-hardening` | `main` | Production hardening: HTTP timeouts + retry/backoff, JSON logs with request ids, `/metrics`, OTEL tracing, `/readyz`, graceful shutdown, ruff/mypy, Postgres+Redis integration CI, GHCR image publishing; fixes the deferred duplicate-session bug |

**Follow-up (not in this repo yet):** Phase 8 — Devin Security Swarm, an optional additional `DiscoverySource` implementation (`app/discovery/base.py`) selected with the `DISCOVERY_SOURCE` toggle alongside Semgrep. Nothing in Phases 1-7 depends on it.

## Production hardening (Phase 7)

### Outbound HTTP resilience

Every GitHub and Devin call goes through `app/http_client.py`: `client()` builds an `httpx.Client` with connect/read/write/pool timeouts (`HTTP_CONNECT_TIMEOUT`, `HTTP_READ_TIMEOUT`) and `request()` retries with exponential backoff + full jitter (`HTTP_MAX_ATTEMPTS`, `HTTP_BACKOFF_BASE_SECONDS`, `HTTP_BACKOFF_CAP_SECONDS`):

| Failure | GET / HEAD / OPTIONS / PUT / DELETE | POST (`create_issue`, `create_session`, `post_comment`, ...) |
|---|---|---|
| timeout / connection error | retried | **not retried** (the request may have been applied) |
| 500 / 502 / 503 / 504 | retried | **not retried** |
| 429 | retried, honouring `Retry-After` (seconds or HTTP-date, capped) | retried, honouring `Retry-After` (nothing was applied) |
| other 4xx | raised immediately | raised immediately |

Public function signatures in `app/github.py` / `app/devin.py` are unchanged. Each retry increments `external_http_retries_total{host}`.

### Observability

- **Structured logs.** `LOG_FORMAT=json` (default) emits one JSON object per line with `ts`, `level`, `logger`, `message`, `request_id` and, when a span is active, `trace_id`/`span_id`. `LOG_FORMAT=text` for local development.
- **Request ids.** The API accepts `X-Request-ID` (or generates one) and echoes it on the response; it is propagated into every Celery task published from that request (task header) so worker logs for a scan/remediation/poll carry the same id.
- **Metrics.** `GET /metrics` on the `api` service: HTTP request metrics (`prometheus-fastapi-instrumentator`) plus domain counters `remediation_sessions_created_total`, `remediation_session_create_failures_total`, `remediation_polls_total{outcome}`, `remediation_outcomes_total{status}`, `remediation_claim_conflicts_total`, `discovery_findings_total{source}`, `discovery_issues_created_total`, `external_http_retries_total{host}`. Workers share the counter definitions; scrape them by running a worker-side exporter if needed.
- **Tracing.** OpenTelemetry spans wrap incoming API requests, every outbound HTTP attempt, Devin session create/poll, GitHub scans and each Celery task. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/HTTP, e.g. `http://otel-collector:4318`) and optionally `OTEL_SERVICE_NAME` / `SERVICE_NAME` to export; unset, the SDK is a no-op.
- **Probes.** `GET /healthz` (liveness: DB ping, unchanged) and `GET /readyz` (readiness: `SELECT 1` **and** a broker connection). Compose and k8s use `/readyz` for readiness so pods stop receiving traffic when Redis is unreachable.

### Graceful shutdown & restarts

- **api:** uvicorn `--timeout-graceful-shutdown 20`; the lifespan flushes the tracer, disposes the SQLAlchemy engine and closes Celery connections. `stop_grace_period: 30s` / `terminationGracePeriodSeconds: 30`.
- **workers:** `task_acks_late=True`, `worker_prefetch_multiplier=1` and `worker_cancel_long_running_tasks_on_connection_loss=True`, so a killed worker's in-flight task is redelivered instead of lost; `CELERY_TASK_SOFT_TIME_LIMIT` / `CELERY_TASK_TIME_LIMIT` bound runaway tasks. Grace period 2 min so warm shutdown can drain.
- **Poll chain across restarts:** each poll is guarded by a `poll_token` + `poll_lease_until` lease in Postgres. A redelivered or duplicate poll for a stale token is a no-op; a lease that expires (worker died mid-poll) is re-armed by the next reconciliation scan.
- **Deferred bug (duplicate Devin sessions).** Overlapping remediation tasks for the same issue (webhook + reconciliation scan, or a redelivered task) used to both see "not running" and each open a Devin session. `store.claim_remediation()` now reserves the issue atomically (`INSERT ... ON CONFLICT DO NOTHING ... RETURNING` / conditional `UPDATE`): exactly one caller wins, rows with a live `session_id` are never reclaimed, and a `running` row without a session (worker died between claim and `create_session`) becomes reclaimable only after its creation lease expires. Regression tests: `tests/test_remediation_claim.py`.

### Lint, type-check and CI

`uv run ruff check . && uv run ruff format --check . && uv run mypy` must pass. `.github/workflows/tests.yml` runs on every PR: `lint` (ruff), `typecheck` (mypy over `app/`), `test` (pytest on SQLite), `integration` (Alembic migrations + pytest against Postgres 16 and Redis 7 service containers — exercises the dialect-specific atomic claim, `/readyz` broker check and request-id propagation through a real Celery worker, `tests/test_integration.py`), `frontend` (oxlint + Vite build), `e2e` (Playwright, uploads `playwright-screenshots-videos` + `playwright-report` artifacts) and `image-build` (builds both Dockerfiles without pushing).

### Image publishing & deployment

`.github/workflows/images.yml` builds and pushes `ghcr.io/<owner>/devin-remediation-service/service` and `.../frontend` on every push to `main`, tagged with the full commit SHA and `main`, authenticated with the workflow's `GITHUB_TOKEN` (no extra secrets). The `deploy` job is gated: it only runs when the repository variable `DEPLOY_ENABLED=true` and the `production` environment provides `DEPLOY_KUBECONFIG` (base64 kubeconfig); it then runs `k8s/deploy.sh <image>:<sha> devin-remediation` (migration Job, then every Deployment rollout). Without those, deploy is skipped and CI stays green.

### Secrets management

`.env` is git-ignored; `.env.example` lists every key with placeholders only. In production do not mount a `.env` file: source `GITHUB_TOKEN`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_WEBHOOK_SECRET`, `INGEST_TOKEN` and `POSTGRES_PASSWORD` from a secret manager (External Secrets Operator / Vault Agent / AWS Secrets Manager / GCP Secret Manager) into the `remediation-secrets` Kubernetes Secret (`k8s/secret.example.yaml`) or compose `secrets:`. Rotate by updating the manager and restarting deployments; nothing secret is baked into the images.

## Project structure

```bash
app/
├── main.py         # FastAPI app for the api service: startup (enqueues initial scan), mounts routers
├── api/
│   ├── read.py      # GET /, /status, /status/{n}, /healthz, /readyz, /metrics (read layer for the SPA)
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
├── http_client.py  # Shared httpx client: timeouts + idempotency-aware retry/backoff
├── observability.py # JSON logging, request ids, Prometheus counters, OTEL tracing, Celery signals
├── github.py       # GitHub API client (issues, PRs, comments, webhooks)
├── devin.py        # Devin API client
├── store.py        # Persistent task store (same API as the old in-memory store)
├── db.py           # SQLAlchemy engine/session + Task model
└── templates/
    └── index.html  # Legacy inline dashboard (GET /)
contracts/read-api.openapi.yaml  # Frozen read-API contract consumed by the SPA
frontend/           # React SPA (Vite + TS); Dockerfile + nginx.conf for the compose `frontend` service
e2e/                # Playwright e2e suite (screenshots + video on every run)
alembic/            # Database migrations
scripts/webhook.py  # register / list / delete the repo webhook at runtime
.github/workflows/
├── tests.yml       # pytest, frontend lint/build, Playwright e2e (uploads screenshots/videos)
└── semgrep.yml     # cron/workflow_dispatch Semgrep -> SARIF -> POST /ingest/semgrep
k8s/                # kustomize manifests: api, ingest-worker, devin-worker, beat, migrate job, redis, postgres
alembic.ini
Dockerfile          # one image; compose/k8s pick the command per service
docker-compose.yml  # migrate, api, ingest-worker, devin-worker, beat, frontend, redis, postgres
pyproject.toml
uv.lock
.env.example
```

