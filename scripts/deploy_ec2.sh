#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  deploy_ec2.sh STAGED_GGUF RUN_ID EXPECTED_SHA256 MODEL_ROOT CURRENT_LINK SERVICE_NAME HEALTH_URL

The script installs a versioned GGUF, atomically switches CURRENT_LINK, restarts
SERVICE_NAME, checks HEALTH_URL, and restores the previous symlink on failure.
EOF
}

[[ $# -eq 7 ]] || { usage >&2; exit 2; }

STAGED_GGUF=$1
RUN_ID=$2
EXPECTED_SHA256=$3
MODEL_ROOT=$4
CURRENT_LINK=$5
SERVICE_NAME=$6
HEALTH_URL=$7

RELEASE_DIR="${MODEL_ROOT}/releases/${RUN_ID}"
FINAL_MODEL="${RELEASE_DIR}/model-Q8_0.gguf"
PREVIOUS_TARGET=""

mkdir -p "${RELEASE_DIR}"
[[ -f "${STAGED_GGUF}" ]] || { echo "Missing staged model: ${STAGED_GGUF}" >&2; exit 1; }

ACTUAL_SHA256=$(sha256sum "${STAGED_GGUF}" | awk '{print $1}')
[[ "${ACTUAL_SHA256}" == "${EXPECTED_SHA256}" ]] || {
  echo "SHA256 mismatch: expected=${EXPECTED_SHA256} actual=${ACTUAL_SHA256}" >&2
  exit 1
}

if [[ -L "${CURRENT_LINK}" ]]; then
  PREVIOUS_TARGET=$(readlink "${CURRENT_LINK}")
fi

install -m 0644 "${STAGED_GGUF}" "${FINAL_MODEL}"
ln -sfn "${FINAL_MODEL}" "${CURRENT_LINK}"

rollback() {
  echo "Deployment failed; rolling back." >&2
  if [[ -n "${PREVIOUS_TARGET}" ]]; then
    ln -sfn "${PREVIOUS_TARGET}" "${CURRENT_LINK}"
    sudo systemctl restart "${SERVICE_NAME}" || true
  fi
}
trap rollback ERR

sudo systemctl restart "${SERVICE_NAME}"

for _ in $(seq 1 60); do
  if curl --fail --silent --show-error "${HEALTH_URL}" >/dev/null; then
    trap - ERR
    echo "Deployment healthy: ${FINAL_MODEL}"
    exit 0
  fi
  sleep 2
done

echo "Health check timed out: ${HEALTH_URL}" >&2
exit 1
