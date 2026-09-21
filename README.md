# Devin Remediation Service

An event-driven automation that scans a GitHub repository for issues labelled `devin-remediate` and spins up [Devin](https://devin.ai) sessions to fix them — opening pull requests with proposed solutions.

## How it works

1. On startup, the service scans the GitHub repo for open issues with the `devin-remediate` label.
2. For each issue, it checks for an existing open PR before creating a Devin session, so restarting the service never triggers duplicate work.
3. If no open PR exists, a Devin session is created with a structured prompt to fix the issue and open a PR.
4. A background loop repeats the scan every `SCAN_INTERVAL_MINUTES` to pick up newly labelled issues.
5. A separate background loop polls the Devin API every 60 seconds to update the status of running sessions. A session is marked completed when Devin reports `status: exit`, `status: running` with `status_detail: finished`, or when a PR is found but the session is still finishing up; it is marked failed on `error` or `suspended`.
6. A live dashboard at `http://localhost:8000` shows the status of all tasks.
7. Task state (issue, session, status, PR URL) is persisted in PostgreSQL, so it survives restarts.

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
```

- `GITHUB_TOKEN` — fine-grained personal access token scoped to the target repository. Full permission footprint:
  - **Issues: read and write** — list labelled issues, post comments, create issues from static-analysis findings.
  - **Pull requests: read** — detect existing PRs for an issue.
  - **Contents: read** — clone the (possibly private) target repository for Semgrep scans.
  - **Webhooks: read and write** — programmatic webhook registration.
  - **Metadata: read** — baseline permission required by every fine-grained token.
- `DATABASE_URL` — SQLAlchemy connection string. The default in `.env.example` points at the `postgres` service in `docker-compose.yml`. If unset, the app falls back to a local SQLite file (`sqlite:///./remediation.db`), which is what the unit tests use (in-memory).
- `DEVIN_API_KEY` — API key for Devin (starts with `cog_`), which you can generate following the instructions [here](https://docs.devin.ai/api-reference/getting-started/teams-quickstart#step-2-generate-an-api-key).
- `DEVIN_ORG_ID` - Organization ID for Devin (starts with `org-`), which you can find under `Settings -> General` in [app.devin.ai](app.devin.ai).

`.gitignore` already excludes `.env`, so real secrets stay local; `.env.example` only ever contains placeholders.

> **Note:** Devin must be linked to a GitHub account with **write access** to the target repository so that it can open pull requests on your behalf. If the repository is private, that account also needs read access to clone it.

### 2. Run with Docker

Install Docker Desktop and run the following command in the project root:

```bash
docker compose up --build
```

This starts the `app` container plus a `postgres` (16) container with a persistent `pgdata` volume. On startup the app runs `alembic upgrade head` to apply migrations before serving.

The dashboard will be available at **http://localhost:8000**.
At startup, the app will scan for open issues labelled `devin-remediate` in the target repository.
A background loop will scan every `SCAN_INTERVAL_MINUTES` to pick up newly labelled issues.
You can also trigger a manual scan by using the **Scan Issues** button in the dashboard.
If a Devin session fails, you can retry it by clicking the **Retry Failed** button.

## Development with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run the following commands in the project root:

```bash
uv sync
uv run pytest
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`uv sync` installs the runtime dependencies and the `dev` dependency group (which includes the test tools). `uv run pytest` runs the test suite, and `uv run uvicorn app.main:app` starts the local dashboard at **http://localhost:8000**.

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

## Architecture decisions

**Stack.** FastAPI + uvicorn for the API server, httpx for HTTP calls to the GitHub and Devin APIs, python-dotenv for configuration, SQLAlchemy + Alembic on PostgreSQL for persistence.

**Persistent storage.** `app/store.py` keeps the original function signatures (`upsert`, `get`, `get_all`, `get_status`, `clear`) but is backed by a `tasks` table (`app/db.py`). On startup the service still re-scans GitHub and treats the live PR state as the source of truth, so a stale database never blocks work.

**Polling over webhooks.** Rather than using a GitHub webhook, the service polls on a configurable interval. This avoids the need for a public endpoint and makes local development and Docker deployment straightforward with no external infrastructure. In a production deployment, a webhook could be added so that the service is notified immediately when an issue is labelled, but the polling approach is simpler and sufficient for a demo/local deployment.

**Single process, async background tasks.** The periodic scan and session polling loops run as asyncio tasks within the same FastAPI process. This keeps the deployment footprint to a single container with no separate worker process or message broker.

## Project structure

```bash
app/
├── main.py         # FastAPI app, startup, background loop, routes
├── github.py       # GitHub API client
├── devin.py        # Devin API client
├── store.py        # Persistent task store (same API as the old in-memory store)
├── db.py           # SQLAlchemy engine/session + Task model
└── templates/
    └── index.html  # Dashboard
alembic/            # Database migrations
alembic.ini
Dockerfile
docker-compose.yml
pyproject.toml
uv.lock
.env.example
```

