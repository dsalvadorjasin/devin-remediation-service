#!/usr/bin/env bash
# Phase 1 ECS migration check, fully local (no AWS account):
#
#   1. shape-check ecs/taskdef + ecs/service against the botocore ECS model
#   2. start the "AWS" side: LocalStack Secrets Manager (seeded), ECS agent
#      endpoints, managed-service stand-ins (postgres/redis), GitHub/Devin stub
#   3. render each task's env from its task definition (+ secrets by ARN)
#   4. RunTask migrate (one-shot), then start the ECS services, gated on health
#   5. probe /readyz, the frontend, Service Connect names, metadata/credentials
#   6. run the live dashboard e2e (Playwright video + screenshots + HTML report)
#   7. stage evidence in artifacts/ and tear everything down
#
#   bash scripts/verify-ecs-local.sh
#
# Needs: docker (compose v2.24+), node/npm, python3, curl, and a .env (copy
# .env.example; dummy GitHub/Devin values are fine - outbound calls go to the
# stub). Env knobs: KEEP_STACK=1 (skip teardown), ECS_LOCAL_PROJECT.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT=$PWD
ARTIFACTS=$ROOT/artifacts
COMPOSE=("$ROOT/scripts/ecs-local-compose.sh")
SERVICES=(api ingest-worker devin-worker beat)
export ECS_LOCAL_STUBS=1

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

collect() {
  mkdir -p "$ARTIFACTS/logs"
  "${COMPOSE[@]}" ps -a >"$ARTIFACTS/logs/compose-ps.txt" 2>&1 || true
  for svc in localstack ecs-local-endpoints upstream-stub postgres redis "${SERVICES[@]}" frontend; do
    "${COMPOSE[@]}" logs --no-color "$svc" >"$ARTIFACTS/logs/$svc.log" 2>&1 || true
  done
  timeout 30 "${COMPOSE[@]}" exec -T upstream-stub python -c \
    "import json,ssl,urllib.request as u;print(json.dumps(json.load(u.urlopen('https://api.github.com/__stub/requests',context=ssl.create_default_context(cafile='/stub-ca/ca.pem'))),indent=1))" \
    >"$ARTIFACTS/stub-upstream-requests.json" 2>/dev/null || true
  if [[ -d e2e/test-results ]]; then cp -R e2e/test-results "$ARTIFACTS/test-results"; fi
  if [[ -d e2e/playwright-report ]]; then cp -R e2e/playwright-report "$ARTIFACTS/playwright-report"; fi
}

cleanup() {
  local status=$?
  set +e
  log "Collecting evidence into artifacts/"
  collect
  if [[ ${KEEP_STACK:-0} == 1 ]]; then
    log "KEEP_STACK=1: stack left running (scripts/ecs-local-compose.sh down -v --remove-orphans)"
  else
    log "Tearing down"
    "${COMPOSE[@]}" --profile run-task down -v --remove-orphans >/dev/null 2>&1
    rm -rf .ecs-local  # rendered task env holds resolved secret values
  fi
  if [[ $status -eq 0 ]]; then log "PASS - evidence in artifacts/"; else log "FAILED (exit $status) - see artifacts/logs/"; fi
  exit "$status"
}

# --- preflight ---------------------------------------------------------------
[[ -f .env ]] || die ".env not found (cp .env.example .env; dummy GitHub/Devin values are fine)"
command -v docker >/dev/null || die "docker not found"
command -v npm >/dev/null || die "npm not found"
docker compose version >/dev/null || die "docker compose v2 not available"
for port in 5173 8000 4566; do
  port_in_use "$port" && die "port $port already in use; stop whatever is listening (the e2e must hit the ECS-local containers)"
done

rm -rf "$ARTIFACTS" e2e/test-results e2e/playwright-report
mkdir -p "$ARTIFACTS/ecs" .ecs-local/env
# Placeholders so the compose model loads before secrets are resolved.
for f in api ingest-worker devin-worker beat frontend; do : >".ecs-local/env/$f.env"; done
trap cleanup EXIT

"${COMPOSE[@]}" --profile run-task down -v --remove-orphans >/dev/null 2>&1 || true

log "Building images"
"${COMPOSE[@]}" --profile run-task build

log "Starting AWS/managed-service stand-ins (LocalStack, ECS agent endpoints, postgres, redis, GitHub/Devin stub)"
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 localstack ecs-local-endpoints postgres redis upstream-stub
"${COMPOSE[@]}" logs --no-color localstack | grep 'localstack-init:' | sed 's/^.*localstack-init: /  /' | tee "$ARTIFACTS/ecs/secrets-seeded.txt"

log "Shape-checking task + service definitions (botocore ECS model, offline)"
"${COMPOSE[@]}" exec -T -e ECS_DEFS_DIR=/ecs-defs localstack python3 - <scripts/ecs_validate_defs.py \
  | tee "$ARTIFACTS/ecs/definitions-validation.txt"

log "Rendering task environments (taskdef environment + ecs.local.env; secrets by ARN from Secrets Manager)"
python3 scripts/ecs_local_env.py --endpoint http://127.0.0.1:4566 | tee "$ARTIFACTS/ecs/task-env.txt"

log "RunTask: migrate"
scripts/ecs-local-run-task.sh migrate | tee "$ARTIFACTS/ecs/run-task-migrate.txt"

log "Starting ECS services: ${SERVICES[*]}"
"${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout 240 "${SERVICES[@]}"
log "Starting ECS service: frontend (:5173)"
"${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout 120 frontend

log "Probing the running stack"
{
  echo "# GET http://localhost:8000/readyz"
  curl -fsS --retry 10 --retry-delay 2 --retry-all-errors http://localhost:8000/readyz
  echo
  echo "# GET http://localhost:5173/ (frontend task, nginx)"
  curl -fsS -o /dev/null -D - http://localhost:5173/ | grep -iE '^(HTTP|server|content-type)'
  echo "# GET http://localhost:5173/healthz (nginx -> API_UPSTREAM=http://api.ecs.local:8000)"
  curl -fsS http://localhost:5173/healthz
  echo
  echo "# GET http://localhost:5173/status"
  curl -fsS http://localhost:5173/status
  echo
} | tee "$ARTIFACTS/ecs/probes.txt"
grep -q '"ready":true' "$ARTIFACTS/ecs/probes.txt" || die "/readyz is not ready"

"${COMPOSE[@]}" exec -T api python - <<'PY' | tee "$ARTIFACTS/ecs/runtime-contract.txt"
import json, os, socket, urllib.request

def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)

print("# Service Connect / Cloud Map names (from the api task)")
for name in ("api.ecs.local", "postgres.ecs.local", "redis.ecs.local", "frontend.ecs.local"):
    print(f"  {name:<22} -> {socket.gethostbyname(name)}")
print("# Task environment (secrets redacted)")
for key in ("AWS_EXECUTION_ENV", "ECS_CONTAINER_METADATA_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "CELERY_BROKER_URL", "GITHUB_REPO"):
    print(f"  {key}={os.environ.get(key, '<unset>')}")
db = os.environ.get("DATABASE_URL", "")
print(f"  DATABASE_URL=<from secret devin-remediation/database-url, host {db.rsplit('@', 1)[-1]}>")
meta = os.environ["ECS_CONTAINER_METADATA_URI"]
container = get(meta)
task = get(meta + "/task")
print("# Task metadata v3 ($ECS_CONTAINER_METADATA_URI, $ECS_CONTAINER_METADATA_URI/task)")
print(f"  container: {container.get('Name')} image={container.get('Image')}")
print(f"  task: {task.get('Family')} rev {task.get('Revision')} status={task.get('KnownStatus')} "
      f"containers={sorted(c.get('Name') for c in task.get('Containers', []))}")
creds = get("http://169.254.170.2" + os.environ["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"])
print("# Task role credentials ($AWS_CONTAINER_CREDENTIALS_RELATIVE_URI)")
print(f"  keys={sorted(creds)} AccessKeyId={creds.get('AccessKeyId', '')[:4]}... Expiration={creds.get('Expiration')}")
assert creds.get("AccessKeyId") and creds.get("SecretAccessKey")
PY

log "Beat singleton check"
beats=$("${COMPOSE[@]}" ps -q beat | wc -l)
echo "beat tasks running: $beats (ecs/service/beat.json: desiredCount=1, maximumPercent=100, minimumHealthyPercent=0)" \
  | tee "$ARTIFACTS/ecs/beat-singleton.txt"
[[ $beats -eq 1 ]] || die "expected exactly one beat task, found $beats"

log "Live dashboard e2e against the deployed frontend container (:5173)"
INGEST_TOKEN_VALUE=$(sed -n 's/^INGEST_TOKEN=//p' .ecs-local/env/api.env)
(
  cd e2e
  [[ -d node_modules ]] || npm ci
  npx playwright install chromium >/dev/null
  E2E_LIVE_API=1 E2E_INGEST_TOKEN="$INGEST_TOKEN_VALUE" E2E_API_URL=http://localhost:8000 \
    npx playwright test tests/dashboard.live.spec.ts
)
