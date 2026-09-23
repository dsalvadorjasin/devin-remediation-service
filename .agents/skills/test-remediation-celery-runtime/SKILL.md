---
name: test-remediation-celery-runtime
description: Run safe end-to-end tests of the remediation dashboard and Celery poll lifecycle using isolated PostgreSQL and Redis.
---

# Remediation runtime testing

## Devin Secrets Needed
- For real external calls: GITHUB_TOKEN, GITHUB_REPO, DEVIN_API_KEY, DEVIN_ORG_ID.
- The git-ignored `.env` may contain these plus DATABASE_URL and CELERY URLs.
  Confirm key presence without printing values or copying the file into artifacts.
- Controlled local HTTP fixtures need no real external credentials.

## Runtime
- Use `uv run` from the repository root. The project `.venv` may differ from an
  inherited VIRTUAL_ENV; do not install into an unrelated repository environment.
- Run PostgreSQL, Redis, API, worker, and Beat as separate processes/services.
- If Docker Hub pulls are unavailable, alternatives are
  `quay.io/sclorg/postgresql-16-c9s` and `quay.io/sclorg/redis-7-c9s`.
  The Postgres image uses POSTGRESQL_USER, POSTGRESQL_PASSWORD, POSTGRESQL_DATABASE,
  not the POSTGRES_* variables used by the Docker Hub image.
- Use dedicated container names and ports to protect other sessions.
- Set DATABASE_URL before importing app.db; run `uv run alembic upgrade head`
  before API startup. PostgreSQL schema creation is migration-managed.
- Run `uv run uvicorn app.main:app --port 8000`,
  `uv run celery -A app.celery_app worker -Q ingest,devin -l info -c 2`,
  and `uv run celery -A app.celery_app beat -l info`.
- For periodic recovery testing set SCAN_INTERVAL_MINUTES=1. On branches with
  discovery scheduling, disable or safely isolate discovery to avoid real issue creation.

## Safe fixtures
- Never run a reconciliation scan against real labeled issues without assessing
  session-creation cost and external-write side effects.
- With user-approved stubbing, a temporary launcher outside the checkout can set
  app.devin.DEVIN_API and app.github.GITHUB_API to a localhost HTTP fixture server
  before starting the worker. Keep real application logic, database, broker,
  task scheduling, and status mapping unchanged.
- Use dummy credentials for fixture services. Return no labeled GitHub issues
  and reject all external POSTs. Audit requests by time/path/status; never log headers.
- Seed rows directly in the isolated database, scoped by repository and issue number.
  Label fixture rows clearly in screenshots.

## Observable assertions
- Dashboard `/` has **Scan Issues**, sending POST /scan; the response acknowledges
  enqueueing, not scan completion. Use worker result logs to verify polls_rearmed.
- Before phase 6, `/status` exposes poll_token and poll_lease_until; phase-5
  also has `/status/{issue}`. From phase 6, both read routes project exactly
  nine public fields and exclude lease fields. Read leases through the store
  when testing workers. Dashboard's 5-second refresh shows status totals.
- For lost-chain recovery, kill worker AND prefork children, purge only the
  isolated broker database, expire the row's lease, restart worker, click Scan Issues.
- Record original/new tokens; a second scan must not change an active owner.
- Inject a stale-token task through Celery and verify no external HTTP request
  or lease renewal; Celery still logs receipt/success(False).
- Count actual external session GETs, not Celery "received" logs: countdown tasks
  can be received long before execution. Observe two 60-second hops and lease renewal.
- For terminal states, verify both token and lease clear, then observe longer
  than one interval to prove the chain has stopped.
- For startup-specific recovery, seed after stopping the API and after any old
  poll chain has stopped (or purge it with the worker down). Otherwise an old
  countdown can finish the row before startup scans it, making the check inconclusive.
- Stop processes, remove disposable containers, and verify no listeners remain.

## React SPA (phase 6+)
- Run `npm ci` then `npm run dev -- --host 0.0.0.0` from `frontend/` (:5173).
  Vite proxies `/status` and `/healthz` to API_PROXY_TARGET (default
  http://localhost:8000); set it when using an isolated backend port.
- Two test modes:
  - **Mocked (frontend-only changes, no secrets):** an external temporary HTTP
    fixture serving TaskView arrays. Include running/completed/failed rows, null
    session/PR links, and a mode that changes an existing issue's status without
    reloading the page. Label fixture titles clearly; this validates the SPA,
    not backend integration or deployed routing.
  - **Real API (read-only):** run only isolated PostgreSQL, Redis, migrations,
    and API. Explicitly set `CELERY_TASK_ALWAYS_EAGER=0` (eager mode would run the
    startup scan inside the API process), then keep workers and Beat stopped: API
    startup enqueues a scan but will not execute external work without workers. That queued `scan_task` stays in
    the broker: use a disposable Redis (or purge the isolated broker DB) before
    ever starting a worker against it. Use the same DATABASE_URL and
    GITHUB_REPO for API and store seed commands. `store.upsert` inserts realistic
    rows; use a dedicated repository namespace and visibly label titles TEST.
    Never clear a shared repository.
- Seed running/completed/failed rows without reloading the browser; verify
  1/1/1 summary counts, colored badges, and null session/PR placeholders.
  Update a row, then stop/restart the API to verify retained data and recovery.
- Test HTTP 500 and hanging responses separately: retain last good rows and
  timestamp while showing an error, then clear the error on a successful poll.
- Measure request start/finish timestamps. The SPA schedules the next poll
  5000ms after the previous request settles, not at fixed 5-second intervals.
  A 7-second response should therefore produce about 12 seconds between starts.
  Hanging requests time out after 15 seconds; recovering a fixture does not
  retroactively complete an already hanging request.
- Set a non-null lease with `store.claim_poll` before checking that HTTP
  responses omit poll_token/poll_lease_until.
- For production smoke checks, build `frontend/Dockerfile`, run nginx on an
  unused port, and set API_UPSTREAM to an API address reachable inside Docker
  (host.docker.internal with host-gateway when the API runs on the host).
  Unknown `/status/<number>` should return 404 JSON, not SPA HTML.
- Computer/browser inspection may rewrite link targets. If `_blank` appears
  missing despite source markup, verify in a clean Chrome profile or a fresh
  Playwright Chromium context (no routes/mocks) against the same backend:
  check popup events, exact URLs, and original-page preservation. Chrome
  `--headless --dump-dom --virtual-time-budget=2500` is a supplemental DOM
  check; screenshots remain required for visual claims. Do not change
  application DOM to compensate; distinguish tooling from app bugs.
