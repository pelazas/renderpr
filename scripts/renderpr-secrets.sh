#!/usr/bin/env bash
set -euo pipefail

# Store per-repo user secrets (env vars + auth signing secrets/provider keys) for
# RenderPR. One SSM SecureString parameter per secret under:
#
#   /renderpr/secrets/{installation_id}/{owner}/{repo}/{KEY}
#
# The Fargate agent reads these (via GetParametersByPath) to inject env vars and
# to forge/mint a synthetic-user session for auth-gated apps. Secrets are NEVER
# injected on fork PRs (enforced in the agent).
#
# Usage:
#   scripts/renderpr-secrets.sh <installation_id> <owner/repo> KEY=VALUE [KEY=VALUE ...]
#   scripts/renderpr-secrets.sh <installation_id> <owner/repo> --from-file path/to/.env
#   scripts/renderpr-secrets.sh --list <installation_id> <owner/repo>
#
# Examples:
#   scripts/renderpr-secrets.sh 12345 acme/web NEXT_PUBLIC_API_URL=https://api.acme.dev
#   scripts/renderpr-secrets.sh 12345 acme/web NEXTAUTH_SECRET="$(openssl rand -hex 32)"
#   scripts/renderpr-secrets.sh 12345 acme/web --from-file .env.preview

if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is not installed." >&2
  exit 1
fi

PREFIX="/renderpr/secrets"

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ $# -lt 2 ] && usage

# --list mode
if [ "$1" == "--list" ]; then
  shift
  INSTALLATION_ID="$1"; REPO="$2"
  aws ssm get-parameters-by-path \
    --path "${PREFIX}/${INSTALLATION_ID}/${REPO}/" \
    --recursive --query "Parameters[].Name" --output text || true
  exit 0
fi

INSTALLATION_ID="$1"; REPO="$2"; shift 2
[ $# -eq 0 ] && usage

put_secret() {
  local key="$1" value="$2"
  if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Skipping invalid key: $key" >&2
    return
  fi
  aws ssm put-parameter \
    --name "${PREFIX}/${INSTALLATION_ID}/${REPO}/${key}" \
    --type "SecureString" \
    --value "$value" \
    --overwrite \
    --output json > /dev/null
  echo "  stored ${key}"
}

echo "Storing secrets under ${PREFIX}/${INSTALLATION_ID}/${REPO}/"

if [ "$1" == "--from-file" ]; then
  FILE="$2"
  [ -f "$FILE" ] || { echo "Error: file not found: $FILE" >&2; exit 1; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"   # ltrim
    [ -z "$line" ] && continue
    [[ "$line" == \#* ]] && continue
    line="${line#export }"
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"; value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"        # rtrim key
    value="${value%\"}"; value="${value#\"}"     # strip surrounding quotes
    put_secret "$key" "$value"
  done < "$FILE"
else
  for pair in "$@"; do
    [[ "$pair" != *=* ]] && { echo "Skipping (not KEY=VALUE): $pair" >&2; continue; }
    put_secret "${pair%%=*}" "${pair#*=}"
  done
fi

echo "Done."
