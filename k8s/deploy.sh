#!/usr/bin/env sh
# Release recipe: pin an immutable image, re-run migrations, then roll the
# workloads. Every release MUST pass a unique image reference (tag or digest);
# a re-applied `:latest` does not change the pod templates, so the Deployments
# would keep the old image while the fresh migration Job ran the new one.
#
# Usage: k8s/deploy.sh <image-ref> [namespace]
#   image-ref  e.g. ghcr.io/org/devin-remediation-service:v1.4.2
#              or   ghcr.io/org/devin-remediation-service@sha256:...
set -eu
IMAGE="${1:?image reference required, e.g. ghcr.io/org/devin-remediation-service:v1.4.2}"
NS="${2:-devin-remediation}"
BASE="$(cd "$(dirname "$0")" && pwd)"

case "$IMAGE" in
  *@sha256:*) NAME="${IMAGE%@*}"; REF="digest: ${IMAGE#*@}" ;;
  */*:*) NAME="${IMAGE%:*}"; REF="newTag: ${IMAGE##*:}" ;;
  *) echo "image-ref needs a registry path plus a tag or digest: $IMAGE" >&2; exit 2 ;;
esac
case "$REF" in "newTag: latest") echo "refusing ':latest'; use a unique tag or digest" >&2; exit 2 ;; esac

# Overlay so namespace and image are set in one rendered manifest (kustomize
# has no CLI flags for either); every kubectl call below uses the same NS.
# The overlay is a sibling of the base dir: kustomize rejects absolute bases
# and treats an overlay nested inside its base as a cycle.
OVERLAY="$(mktemp -d "$(dirname "$BASE")/.k8s-release.XXXXXX")"; trap 'rm -rf "$OVERLAY"' EXIT
cat > "$OVERLAY/kustomization.yaml" <<KUSTOMIZATION
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources: [../$(basename "$BASE")]
images:
  - name: devin-remediation-service
    newName: $NAME
    $REF
KUSTOMIZATION

kubectl -n "$NS" get secret remediation-secrets >/dev/null  # must exist (see README)
kubectl -n "$NS" delete job remediation-migrate --ignore-not-found
kubectl apply -k "$OVERLAY"
kubectl -n "$NS" wait --for=condition=complete --timeout=10m job/remediation-migrate
for d in remediation-api remediation-ingest-worker remediation-devin-worker remediation-beat; do
  kubectl -n "$NS" rollout status "deploy/$d"
done
