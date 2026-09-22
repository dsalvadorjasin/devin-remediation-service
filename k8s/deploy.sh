#!/usr/bin/env sh
# Release recipe: re-run migrations, then roll the workloads.
# Usage: k8s/deploy.sh [namespace]
set -eu
NS="${1:-devin-remediation}"
kubectl -n "$NS" get secret remediation-secrets >/dev/null  # must exist (see README)
kubectl -n "$NS" delete job remediation-migrate --ignore-not-found
kubectl apply -k "$(dirname "$0")"
kubectl -n "$NS" wait --for=condition=complete --timeout=10m job/remediation-migrate
kubectl -n "$NS" rollout status deploy/remediation-api
kubectl -n "$NS" rollout status deploy/remediation-ingest-worker
kubectl -n "$NS" rollout status deploy/remediation-devin-worker
kubectl -n "$NS" rollout status deploy/remediation-beat
