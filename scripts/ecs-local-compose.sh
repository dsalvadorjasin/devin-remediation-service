#!/usr/bin/env bash
# `docker compose` bound to the ECS-local stack (project name + overlays).
#
#   scripts/ecs-local-compose.sh ps
#   scripts/ecs-local-compose.sh logs -f api
#   scripts/ecs-local-compose.sh down -v --remove-orphans
#
# ECS_LOCAL_PROJECT  compose project name   (default: remediation-ecs-local)
# ECS_LOCAL_STUBS    1 = include compose.e2e-stubs.yml (GitHub/Devin stub; default 1)
set -euo pipefail
cd "$(dirname "$0")/.."

files=(-f docker-compose.yml -f compose.ecs.local.yml)
if [[ ${ECS_LOCAL_STUBS:-1} == 1 ]]; then
  files+=(-f compose.e2e-stubs.yml)
fi
exec docker compose -p "${ECS_LOCAL_PROJECT:-remediation-ecs-local}" "${files[@]}" "$@"
