#!/usr/bin/env bash
#
# Driver for the RenderPR Phase 3 end-to-end harness.
#
# The harness runs the REAL src.agent pipeline (detection + on-disk mock writers
# + dev-server boot) against each framework fixture, then HTTP-asserts runtime
# behavior. Two modes:
#
#   Docker (default) — mirrors the production agent image; needs a GH token to
#   clone the private fixture mirrors.
#
#   --use-local      — runs on the host against the committed fixture source
#   trees (no clone, no GH token). Fast iteration; needs node + the package
#   managers (pnpm/yarn/npm) on PATH.
#
# Usage:
#   tests/integration/run.sh                       # Docker, all frameworks
#   tests/integration/run.sh --frameworks next     # Docker, subset
#   tests/integration/run.sh --use-local           # host run, all frameworks
#   tests/integration/run.sh --use-local --frameworks sveltekit,remix
#
set -euo pipefail

# Resolve the repo root (this script lives in tests/integration/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUT_DIR="${WORKTREE}/tests/integration/_out"

USE_LOCAL=0
FRAMEWORKS="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --use-local) USE_LOCAL=1; shift ;;
    --frameworks) FRAMEWORKS="$2"; shift 2 ;;
    --frameworks=*) FRAMEWORKS="${1#*=}"; shift ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "${OUT_DIR}"

if [[ "${USE_LOCAL}" -eq 1 ]]; then
  echo "[run.sh] host run (--use-local), frameworks=${FRAMEWORKS}" >&2
  exec python -m tests.integration.entrypoint \
    --frameworks "${FRAMEWORKS}" \
    --use-local \
    --out "${OUT_DIR}"
fi

# Docker flow: build the production-shaped agent image and run the harness in it.
# GH_TOKEN is forwarded so the runner can clone the private fixture mirrors.
echo "[run.sh] Docker run, frameworks=${FRAMEWORKS}" >&2

docker build -t renderpr:harness "${WORKTREE}"

docker run --rm \
  -e GH_TOKEN="$(gh auth token)" \
  -v "${WORKTREE}:/work" \
  -w /work \
  renderpr:harness \
  python -m tests.integration.entrypoint \
    --frameworks "${FRAMEWORKS}" \
    --out /work/tests/integration/_out
