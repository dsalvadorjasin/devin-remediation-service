#!/usr/bin/env bash
# LocalStack "ready" hook (mounted into /etc/localstack/init/ready.d/): seeds
# the Secrets Manager secrets referenced by ecs/taskdef/*.json `secrets` blocks.
#
# Values come from the operator's .env (mounted read-only at /seed/app.env) and
# ecs.local.env (/seed/ecs.local.env, ${NAME} expanded from .env). Only the
# secret names and ARNs are logged, never values. Idempotent: re-running puts a
# new version of each secret.
set -euo pipefail

APP_ENV=/seed/app.env
ECS_ENV=/seed/ecs.local.env
PREFIX=devin-remediation

# name-in-secrets-manager  VAR  source-file
SECRETS=(
  "database-url          DATABASE_URL          $ECS_ENV"
  "postgres-password     POSTGRES_PASSWORD     $APP_ENV"
  "github-token          GITHUB_TOKEN          $APP_ENV"
  "github-webhook-secret GITHUB_WEBHOOK_SECRET $APP_ENV"
  "ingest-token          INGEST_TOKEN          $APP_ENV"
  "devin-api-key         DEVIN_API_KEY         $APP_ENV"
  "devin-org-id          DEVIN_ORG_ID          $APP_ENV"
)

# Unset in .env is valid for these (the app disables the feature).
OPTIONAL=(GITHUB_WEBHOOK_SECRET)

# Last assignment of KEY in a dotenv file; surrounding quotes stripped, no
# shell evaluation of the value.
read_key() {
  local key=$1 file=$2 line value=""
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ $line =~ ^[[:space:]]*(export[[:space:]]+)?${key}=(.*)$ ]] && value=${BASH_REMATCH[2]}
  done <"$file"
  if [[ $value =~ ^\"(.*)\"$ || $value =~ ^\'(.*)\'$ ]]; then
    value=${BASH_REMATCH[1]}
  fi
  printf '%s' "$value"
}

# Expand ${NAME} references against .env.
expand() {
  local value=$1 name
  while [[ $value =~ \$\{([A-Za-z_][A-Za-z0-9_]*)\} ]]; do
    name=${BASH_REMATCH[1]}
    value=${value//\$\{$name\}/$(read_key "$name" "$APP_ENV")}
  done
  printf '%s' "$value"
}

for f in "$APP_ENV" "$ECS_ENV"; do
  [[ -f $f ]] || { echo "localstack-init: missing $f" >&2; exit 1; }
done

for entry in "${SECRETS[@]}"; do
  read -r name var file <<<"$entry"
  value=$(expand "$(read_key "$var" "$file")")
  if [[ -z $value && " ${OPTIONAL[*]} " == *" $var "* ]]; then
    # The task definition still references the secret, so it must exist; an
    # unguessable value keeps the feature effectively disabled.
    value=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
    echo "localstack-init: $var is empty; seeding a random value for $PREFIX/$name"
  elif [[ -z $value ]]; then
    echo "localstack-init: $var is empty in $(basename "$file"); cannot seed $PREFIX/$name" >&2
    exit 1
  fi
  if awslocal secretsmanager describe-secret --secret-id "$PREFIX/$name" >/dev/null 2>&1; then
    awslocal secretsmanager put-secret-value --secret-id "$PREFIX/$name" \
      --secret-string "$value" >/dev/null
  else
    awslocal secretsmanager create-secret --name "$PREFIX/$name" \
      --description "ECS-local demo value for $var" \
      --tags Key=app,Value=devin-remediation --secret-string "$value" >/dev/null
  fi
  arn=$(awslocal secretsmanager describe-secret --secret-id "$PREFIX/$name" --query ARN --output text)
  echo "localstack-init: seeded $var -> $arn"
done
