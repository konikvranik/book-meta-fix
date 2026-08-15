#!/usr/bin/env bash
# Run a one-shot `bmf` command in Kubernetes and stream its output.
# The pod is deleted when it finishes (--rm), so nothing accumulates.
#
# Usage:
#   ./scripts/k8s-bmf.sh analyze --llm
#   ./scripts/k8s-bmf.sh apply --apply
#   ./scripts/k8s-bmf.sh organize --apply
#   ./scripts/k8s-bmf.sh crosscheck --apply
#
# Configuration via environment (defaults below):
#   BMF_K8S_IMAGE    container image
#   BMF_K8S_NFS_SRV  NFS server hosting the library ("" = use BMF_K8S_PVC instead)
#   BMF_K8S_NFS_PATH NFS export path of the library
#   BMF_K8S_PVC      PersistentVolumeClaim holding the library (NFS-less setup)
#   BMF_K8S_SECRET   Secret with env config (ZAI_API_KEY, ...)
#   BMF_K8S_REVIEW   value for BMF_REVIEW (where review.yaml lives)
#   BMF_K8S_CACHE    value for BMF_CACHE ("" = /cache/bmf_cache.db on emptyDir)
set -euo pipefail

BMF_K8S_IMAGE="${BMF_K8S_IMAGE:-ghcr.io/konikvranik/book-meta-fix:latest}"
BMF_K8S_NFS_SRV="${BMF_K8S_NFS_SRV:-nfs.example.com}"
BMF_K8S_NFS_PATH="${BMF_K8S_NFS_PATH:-/export/books}"
BMF_K8S_PVC="${BMF_K8S_PVC:-books}"
BMF_K8S_SECRET="${BMF_K8S_SECRET:-bmf-env}"
BMF_K8S_REVIEW="${BMF_K8S_REVIEW:-/review/review.yaml}"
BMF_K8S_CACHE="${BMF_K8S_CACHE:-/cache/bmf_cache.db}"

if [ $# -eq 0 ]; then
	sed -n 's/^# \(Usage\|\s*bmf\).*/\1/p' "$0" | sed 's/^# \?//' >&2
	exit 2
fi

# JSON-quoted bmf argv: --library /library <subcommand> [extra flags...]
# analyze writes review.yaml wherever --output says (BMF_REVIEW env is only
# the default for apply/gui), so route it at the review PVC explicitly.
EXTRA_ARGS=()
if [ "$1" = "analyze" ]; then
	EXTRA_ARGS=(--output "$BMF_K8S_REVIEW")
fi
ARGS=""
for arg in --library /library "$@" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"; do
	ARGS+="\"$arg\", "
done
ARGS="[${ARGS%, }]"

# Library volume: NFS share when a server is configured, else the PVC.
if [ -n "$BMF_K8S_NFS_SRV" ]; then
	LIBRARY_VOLUME="{\"name\": \"library\", \"nfs\": {\"server\": \"$BMF_K8S_NFS_SRV\", \"path\": \"$BMF_K8S_NFS_PATH\", \"readOnly\": false}}"
else
	LIBRARY_VOLUME="{\"name\": \"library\", \"persistentVolumeClaim\": {\"claimName\": \"$BMF_K8S_PVC\"}}"
fi

# review.yaml on its own PVC (survives the pod, editable from the workstation
# mount of the same share); cache on an emptyDir (it is only a cache — a cold
# rebuild costs time, not correctness).
OVERRIDES=$(cat <<EOF
{
  "apiVersion": "v1",
  "kind": "Pod",
  "spec": {
    "restartPolicy": "Never",
    "containers": [
      {
        "name": "bmf",
        "image": "$BMF_K8S_IMAGE",
        "args": $ARGS,
        "env": [
          {"name": "BMF_REVIEW", "value": "$BMF_K8S_REVIEW"},
          {"name": "BMF_CACHE", "value": "$BMF_K8S_CACHE"}
        ],
        "envFrom": [{"secretRef": {"name": "$BMF_K8S_SECRET"}}],
        "volumeMounts": [
          {"name": "library", "mountPath": "/library"},
          {"name": "review", "mountPath": "/review"},
          {"name": "cache", "mountPath": "/cache"}
        ]
      }
    ],
    "volumes": [
      $LIBRARY_VOLUME,
      {"name": "review", "persistentVolumeClaim": {"claimName": "bmf-review"}},
      {"name": "cache", "emptyDir": {}}
    ]
  }
}
EOF
)

# One-shot pod: streams output, self-deletes, propagates the exit code.
# (Pod name must match RFC1123: lowercase, no slashes in the subcommand.)
exec kubectl run --rm -i --restart=Never \
	"bmf-$(date +%s)-${1//[^a-z]/}" \
	--image "$BMF_K8S_IMAGE" \
	--overrides "$OVERRIDES" \
	--command -- bmf "${@:1}"
