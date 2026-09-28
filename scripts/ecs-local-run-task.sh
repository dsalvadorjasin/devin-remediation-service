#!/usr/bin/env bash
# Local stand-in for `aws ecs run-task`: run a one-shot task to completion and
# propagate its exit code. ECS has no cross-service depends_on, so the schema
# migration is an explicit RunTask step executed before services are
# (re)deployed. Default task: migrate (api task definition, command override).
#
#   scripts/ecs-local-run-task.sh                # alembic upgrade head
#   scripts/ecs-local-run-task.sh migrate uv run alembic current
#
# Real ECS equivalent:
#   aws ecs run-task --cluster devin-remediation --launch-type FARGATE \
#     --task-definition devin-remediation-api \
#     --network-configuration 'awsvpcConfiguration={subnets=[...],securityGroups=[...]}' \
#     --overrides '{"containerOverrides":[{"name":"api","command":["uv","run","alembic","upgrade","head"]}]}'
#   aws ecs wait tasks-stopped ...   # then check containers[0].exitCode == 0
set -euo pipefail
cd "$(dirname "$0")/.."

task=${1:-migrate}
shift || true

if [[ ! -f .ecs-local/env/api.env ]]; then
  echo "ecs-local-run-task: .ecs-local/env/api.env missing - render task env first (python3 scripts/ecs_local_env.py)" >&2
  exit 1
fi

echo "==> RunTask ${task}${*:+ (command override: $*)}"
set +e
scripts/ecs-local-compose.sh run --rm --no-deps "$task" "$@"
status=$?
set -e
if [[ $status -ne 0 ]]; then
  echo "==> RunTask ${task}: STOPPED, exitCode=${status}" >&2
  exit "$status"
fi
echo "==> RunTask ${task}: STOPPED, exitCode=0"
