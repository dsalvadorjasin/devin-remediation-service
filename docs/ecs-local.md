# Running under ECS-like conditions locally

Phase 1 of the self-managed → ECS migration: prove the app runs from its ECS
task definitions ([ecs-mapping.md](ecs-mapping.md)) under the runtime contract
ECS imposes — fully local, no AWS account. Phase 2 validates the same
definitions against the real ECS control plane.

## Run it

```bash
cp .env.example .env        # dummy GitHub/Devin values are fine; set INGEST_TOKEN
bash scripts/verify-ecs-local.sh
```

Needs Docker with Compose v2.24+, Node/npm (Playwright), python3, curl, and free
ports 5173, 8000 and 4566. First run takes a few minutes (image builds,
Playwright chromium). Exit code 0 = every stage passed. `KEEP_STACK=1` leaves
the stack up afterwards.

| Stage | What happens |
|---|---|
| 1 | Build the app and frontend images |
| 2 | Start LocalStack (Secrets Manager seeded by `scripts/localstack-init.sh`), the ECS agent endpoints, postgres/redis (RDS/ElastiCache stand-ins) and the GitHub/Devin stub |
| 3 | Shape-check `ecs/taskdef/*.json` (`RegisterTaskDefinition`) and `ecs/service/*.json` (`CreateService`) against the botocore ECS model |
| 4 | `scripts/ecs_local_env.py`: render each task's environment from its task definition — `environment` + `ecs.local.env` overrides + `secrets` fetched **by ARN** from Secrets Manager — into `.ecs-local/env/<task>.env` |
| 5 | `scripts/ecs-local-run-task.sh migrate` — one-shot "RunTask", must exit 0 |
| 6 | Start `api`, `ingest-worker`, `devin-worker`, `beat`, then `frontend`, each gated on its container health check |
| 7 | Probe `/readyz`, the frontend (nginx on :5173), the nginx → `api.ecs.local` proxy, Service Connect names, task metadata v3 and task-role credentials from inside the `api` task; assert exactly one `beat` |
| 8 | `E2E_LIVE_API=1 npx playwright test tests/dashboard.live.spec.ts` against the running frontend container |
| 9 | Copy evidence to `artifacts/`, `down -v` |

Manual operation of the same stack:

```bash
scripts/ecs-local-compose.sh up -d --wait localstack ecs-local-endpoints postgres redis upstream-stub
python3 scripts/ecs_local_env.py
scripts/ecs-local-run-task.sh                      # migrate
scripts/ecs-local-compose.sh up -d --no-deps --wait api ingest-worker devin-worker beat frontend
scripts/ecs-local-compose.sh logs -f api
```

## Files

| File | Role |
|---|---|
| `compose.ecs.local.yml` | ECS runtime overlay on top of `docker-compose.yml` |
| `compose.e2e-stubs.yml` | Test-only: GitHub/Devin stub on the real hostnames |
| `ecs.local.env` | Local Service Connect names for `DATABASE_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `API_UPSTREAM` |
| `scripts/localstack-init.sh` | LocalStack ready hook: creates the `devin-remediation/*` secrets from `.env` (an unset `GITHUB_WEBHOOK_SECRET` gets a random value) |
| `scripts/ecs_local_env.py` | Plays the ECS agent's env/secret injection |
| `scripts/ecs_validate_defs.py` | Offline botocore shape check of `ecs/` |
| `scripts/ecs-local-run-task.sh` | `aws ecs run-task` stand-in (wraps `docker compose run --rm migrate`) |
| `scripts/ecs-local-compose.sh` | `docker compose` with the project name and overlays preset |
| `scripts/verify-ecs-local.sh` | The end-to-end check above |
| `e2e/tests/dashboard.live.spec.ts` | Live-backend dashboard spec (skipped unless `E2E_LIVE_API` is set) |
| `e2e/stubs/upstream_stub.py` | In-memory GitHub + Devin API stub (TLS, throwaway CA) |

## What is emulated, and what real ECS does instead

| Concern | Local emulation | Real ECS (Fargate) |
|---|---|---|
| Task environment | Rendered from the task definition; the operator's `.env` is **not** passed to containers | Agent injects `environment` from the registered revision |
| Secrets | `secrets[].valueFrom` ARNs resolved against LocalStack Secrets Manager (`GetSecretValue`, exact ARN match) before start | Agent resolves with the execution role at task start; failure → task stops with `ResourceInitializationError` |
| Task metadata | `amazon-ecs-local-container-endpoints` at `169.254.170.2`, `ECS_CONTAINER_METADATA_URI=http://169.254.170.2/v3` | Per-task metadata endpoint v4 (`ECS_CONTAINER_METADATA_URI_V4`), task-scoped |
| Task-role credentials | Same sidecar, `/creds`, dummy static keys (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`/`_FULL_URI`) | STS credentials for `taskRoleArn`, rotated |
| Service discovery | Compose network aliases `api.ecs.local`, `postgres.ecs.local`, `redis.ecs.local`, `frontend.ecs.local` | Service Connect (Envoy sidecar) / Cloud Map; RDS + ElastiCache endpoints for data stores |
| Ordering | No `depends_on` (`!reset`); migrate is an explicit one-shot task; services gated by health checks | No cross-service ordering; `RunTask` in the pipeline; health checks + circuit breaker |
| Restarts | `restart: unless-stopped` | Service scheduler replaces the *task* (new IP, fresh ephemeral storage) |
| Beat singleton | One container; the verify script asserts one `beat` | `desiredCount 1`, `maximumPercent 100`, `minimumHealthyPercent 0` |
| Ingress | Host ports `127.0.0.1:8000` / `127.0.0.1:5173` | ALB target groups, `awsvpc` ENIs |
| Logs | `docker compose logs` (JSON) | `awslogs` → CloudWatch Logs |
| GitHub / Devin | TLS stub answering on `api.github.com` / `api.devin.ai` inside the Docker network; containers trust its CA via `SSL_CERT_FILE` | The real APIs over NAT egress |

The GitHub/Devin stub lets the live spec seed data through the real write path
(`POST /ingest/semgrep` → issue creation → scan → Devin session → PR) with no
external side effects and no app code changes; `artifacts/stub-upstream-requests.json`
lists every upstream call the app made. The stub completes each session with a
PR, except for findings whose rule id contains `stub-devin-error`, which fail.

## Limitations

- **Archived endpoints image.** `awslabs/amazon-ecs-local-container-endpoints`
  is archived; 1.4.2 (pinned by digest) is the last release. It serves metadata
  **v3** (no v4, which Fargate uses), describes the whole Compose project as one
  "task", and needs `DOCKER_API_VERSION=1.44` to talk to current Docker daemons.
  The app does not read task metadata today, so this only proves the endpoints
  are reachable; don't build new features on its exact payloads.
- **No control-plane validation.** Nothing here calls ECS. The botocore check
  proves request *shape* only — not that roles, subnets, target groups, ECR
  images, Service Connect namespaces or quotas exist, nor Fargate CPU/memory
  pairing, secret IAM permissions, or rollout behaviour. That is **Phase 2**:
  register the task definitions and create the services in a sandbox account,
  run the migrate task via `aws ecs run-task`, and repeat the live spec
  against the ALB.
- No Envoy / Service Connect proxy, no `awsvpc` isolation (all containers share
  one bridge network), no task IAM enforcement (LocalStack accepts any key), no
  CPU/memory limits, no CloudWatch.
- Worker health checks use `celery inspect ping`, which goes through Redis; a
  broker outage marks workers unhealthy (in ECS: task replacement).
- Findings reach a *stub* GitHub; the live spec does not prove real GitHub/Devin
  credentials work.

## Artifacts

Written by every run (pass or fail), gitignored:

```
artifacts/
  playwright-report/index.html   HTML report (videos + screenshots embedded)
  test-results/<test>/           video.webm, live-*.png screenshots (trace.zip on failure)
  ecs/                           per-stage output: definitions-validation.txt, task-env.txt,
                                 secrets-seeded.txt, run-task-migrate.txt, probes.txt,
                                 runtime-contract.txt, beat-singleton.txt
  logs/                          docker compose logs per service, compose-ps.txt
  stub-upstream-requests.json    upstream calls the app made (no headers/bodies)
```

`cd e2e && npx playwright show-report ../artifacts/playwright-report` to browse.

## Teardown

`verify-ecs-local.sh` tears down on exit. After `KEEP_STACK=1` or a manual run:

```bash
scripts/ecs-local-compose.sh --profile run-task down -v --remove-orphans
rm -rf .ecs-local artifacts     # rendered task env (contains secret values) + evidence
```
