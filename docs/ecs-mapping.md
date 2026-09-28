# Compose → ECS mapping

Phase 1 of the self-managed → ECS migration. `docker-compose.yml` stays the
source of truth for local development; `ecs/` is the ECS translation of the
same topology, and `compose.ecs.local.yml` runs the translation locally under
ECS-like runtime rules (see [ecs-local.md](ecs-local.md)).

```
ecs/
  taskdef/   RegisterTaskDefinition input, one file per deployable
    api.json  ingest-worker.json  devin-worker.json  beat.json  frontend.json
  service/   CreateService input (desired count, deployment config, Service Connect, ALB)
    api.json  ingest-worker.json  devin-worker.json  beat.json  frontend.json
```

Both sets are plain AWS CLI input (`aws ecs register-task-definition
--cli-input-json file://ecs/taskdef/api.json`, `aws ecs create-service
--cli-input-json file://ecs/service/api.json`). `scripts/ecs_validate_defs.py`
checks them against the botocore ECS model on every verify run. Account
`000000000000` / region `us-east-1` match LocalStack's defaults so the secret
ARNs resolve locally; `REPLACE_*` values (image tag, subnets, security groups,
target groups, ElastiCache endpoint) are filled in per environment in Phase 2.

## Topology

| Compose service | ECS construct | Notes |
|---|---|---|
| `migrate` | **One-shot `RunTask`** of the `api` task definition with a command override (`uv run alembic upgrade head`) | Not a service. Run before every deployment; exit code gates the rollout. `scripts/ecs-local-run-task.sh` |
| `api` | Service `devin-remediation-api`, 2 tasks, ALB target group on :8000, Service Connect `api.ecs.local:8000` | Container health check = the compose `/readyz` check |
| `ingest-worker` | Service, 1 task, no ports | 30 GiB ephemeral storage + task volume for the Semgrep checkout |
| `devin-worker` | Service, 2 tasks, no ports | Scales on queue depth later (Phase 2+) |
| `beat` | Service, **exactly 1 task** | Singleton deployment config, see below |
| `frontend` | Service, 2 tasks, ALB target group on :80, Service Connect `frontend.ecs.local:80` | nginx proxies `/status`, `/healthz` to `API_UPSTREAM=http://api.ecs.local:8000` |
| `postgres` | Amazon RDS for PostgreSQL 16 | Not in `ecs/`. Endpoint lives in the `database-url` secret |
| `redis` | Amazon ElastiCache (Redis OSS 7) | Not in `ecs/`. Endpoint in `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` |

All app task definitions use the same image (ECR
`devin-remediation-service`), differing only in `command`, exactly as the compose
services share one `build: .`.

## Decisions

### `depends_on` → health checks + explicit ordering

ECS has no cross-service `depends_on` (container `dependsOn` only orders
containers *inside one task*). The compose graph is replaced by:

- **Schema first, as a task.** `migrate: condition: service_completed_successfully`
  becomes a `RunTask` that must stop with `exitCode == 0` before services are
  created/updated. Deploy pipeline order: build → push → `RunTask migrate` →
  `update-service` ×5. Alembic migrations must stay backwards compatible for
  one release because old tasks keep running during the rolling deploy.
- **Dependencies on postgres/redis** (`service_healthy`) become the app's own
  retry behaviour plus health checks: Celery retries the broker on startup
  (`broker_connection_retry_on_startup`), and the `api` health check hits
  `/readyz`, which probes both the database and the broker. A task whose
  dependencies are down stays unhealthy, is kept out of the ALB, and the
  deployment circuit breaker rolls back instead of shifting traffic.
- The local overlay enforces the same rules: `depends_on: !reset {}` on every
  app service, and `scripts/verify-ecs-local.sh` starts services only after the
  migrate task succeeded.

### `restart: unless-stopped` → service scheduler

A service keeps `desiredCount` tasks running and replaces tasks that stop or
fail their container health check (`essential: true`). There is no per-container
restart policy on Fargate; the replacement is a new task (new IP, empty
ephemeral storage). `restart: "no"` on `migrate` maps naturally to `RunTask`,
which never restarts.

`deploymentConfiguration` for the stateless services is
`maximumPercent 200 / minimumHealthyPercent 100` with the deployment circuit
breaker (`rollback: true`): new tasks must become healthy before old ones stop.

### `beat` singleton

Two Beat processes would enqueue every periodic task twice. `ecs/service/beat.json`:

```json
"desiredCount": 1,
"deploymentConfiguration": { "maximumPercent": 100, "minimumHealthyPercent": 0, ... }
```

`maximumPercent 100` forbids ECS from starting the new task before the old one
stopped (no overlap during deploys); `minimumHealthyPercent 0` allows the gap.
This is the ECS equivalent of the k8s `replicas: 1` + `Recreate` strategy. The
service also carries a `scheduling` tag spelling this out, since JSON has no
comments. Never attach auto scaling to this service. Dedup in the store
(`claim_remediation`) makes an accidental duplicate harmless, but not free.

### `stop_grace_period` → `stopTimeout`

`api` 30 s (uvicorn `--timeout-graceful-shutdown 20`), workers 120 s (Celery
warm shutdown; `acks_late` redelivers anything unfinished), frontend 10 s. Fargate
caps `stopTimeout` at 120 s, which is why the compose `2m` maps exactly.

### `env_file: .env` → `environment` + `secrets`

The whole `.env` is no longer shipped. Each task definition lists only what that
process reads:

- **`environment`** (plain, visible in the task definition): repo, intervals,
  Celery/HTTP/logging knobs, broker URLs, `SERVICE_NAME`, `API_UPSTREAM`.
- **`secrets`** (Secrets Manager ARNs `arn:aws:secretsmanager:us-east-1:000000000000:secret:devin-remediation/<name>`,
  resolved by the agent with the *execution role* at task start):

| Variable | Secret | api | ingest-worker | devin-worker | beat |
|---|---|:-:|:-:|:-:|:-:|
| `DATABASE_URL` | `database-url` | ✓ | ✓ | ✓ | |
| `GITHUB_TOKEN` | `github-token` | ✓ | ✓ | ✓ | |
| `GITHUB_WEBHOOK_SECRET` | `github-webhook-secret` | ✓ | | | |
| `INGEST_TOKEN` | `ingest-token` | ✓ | | | |
| `DEVIN_API_KEY` | `devin-api-key` | | | ✓ | |
| `DEVIN_ORG_ID` | `devin-org-id` | | | ✓ | |

`POSTGRES_PASSWORD` is only needed to create the database (RDS master
password, `postgres-password` secret); no task reads it. The `api` never calls
Devin, so it never receives Devin credentials; `beat` only enqueues and gets no
secrets at all. Least privilege: each service has its own `taskRoleArn`, and
the shared execution role only needs `secretsmanager:GetSecretValue` on
`devin-remediation/*` plus ECR/CloudWatch Logs.

Broker URLs are plain `environment` because ElastiCache uses in-VPC auth
(security groups; add AUTH/TLS → move them to `secrets` too).

### `UV_NO_SYNC=1` (runtime finding)

The image is built with `uv sync --no-dev`, but `uv run` re-syncs the project at
container start and, because `[tool.uv] default-groups = ["dev"]`, tries to
download the dev group (mypy, pytest, ...). Under compose this silently needs
internet at every start; in ECS it would slow every task start and fail in a
private subnet without egress. All app task definitions set `UV_NO_SYNC=1` so
`uv run` uses the baked virtualenv as-is. (Found by the ECS-local run, where the
first task failed exactly this way.)

### `ports:` → ALB target groups + Service Connect

Host port publishing does not exist in `awsvpc` mode; each task has its own ENI.
Public entry points (`api` for `/ingest/semgrep` + `/webhooks/github`, `frontend`
for the dashboard) sit behind an ALB (`loadBalancers` in the service
definitions). East–west traffic uses Service Connect in the `ecs.local`
namespace: `api` and `frontend` publish `portMappings[].name` endpoints, workers
and beat are clients only.

### compose DNS names → Service Connect / Cloud Map

`http://api:8000`, `postgres:5432`, `redis:6379` become `api.ecs.local`,
`postgres.ecs.local`, `redis.ecs.local` (the latter two are RDS/ElastiCache
endpoints in AWS; the local overlay aliases them so the same values work).
`ecs.local.env` holds the local values.

### `volumes: semgrep-checkout` → task storage

The named volume only caches the target-repo clone. On Fargate it becomes a
task-scoped volume on 30 GiB ephemeral storage (`ephemeralStorage`), so a new
task re-clones once. Use EFS only if clone time becomes a problem.

### Logging → `awslogs`

Every container logs JSON (`LOG_FORMAT=json`) to `/ecs/devin-remediation/<service>`
with `awslogs-create-group`. Service Connect proxies log to
`/ecs/devin-remediation/<service>-service-connect`.

### Sizing (initial, tune in Phase 2)

| Task | CPU | Memory |
|---|---|---|
| api | 0.5 vCPU | 1 GiB |
| ingest-worker | 1 vCPU | 2 GiB (Semgrep) |
| devin-worker | 0.5 vCPU | 1 GiB |
| beat | 0.25 vCPU | 0.5 GiB |
| frontend | 0.25 vCPU | 0.5 GiB |

### Worker health checks

Compose has none for workers; ECS container health checks decide replacement,
so both worker task definitions ping their own Celery node
(`celery inspect ping -d <queue>@$HOSTNAME`). The local overlay adds the same
checks so `docker compose up --wait` has the same gate.
