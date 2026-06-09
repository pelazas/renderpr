#!/usr/bin/env bash
set -euo pipefail

PARAM_NAME="${1:-/renderpr/github-app}"

if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is not installed." >&2
  exit 1
fi

echo "=== RenderPR — Post-Deploy Secret Injection ==="
echo "Target SSM parameter: $PARAM_NAME"
echo ""

read -rp "GitHub App ID: " APP_ID

read -rp "Path to GitHub App private key (.pem file): " PEM_PATH
if [ ! -f "$PEM_PATH" ]; then
  echo "Error: File not found: $PEM_PATH" >&2
  exit 1
fi
PRIVATE_KEY=$(cat "$PEM_PATH")

read -rsp "Webhook Secret: " WEBHOOK_SECRET
echo ""

SECRET_JSON=$(jq -n \
  --arg app_id "$APP_ID" \
  --arg private_key "$PRIVATE_KEY" \
  --arg webhook_secret "$WEBHOOK_SECRET" \
  '{
    app_id: $app_id,
    private_key: $private_key,
    webhook_secret: $webhook_secret
  }'
)

aws ssm put-parameter \
  --name "$PARAM_NAME" \
  --type "SecureString" \
  --value "$SECRET_JSON" \
  --overwrite \
  --output json

echo ""
echo "SSM parameter '$PARAM_NAME' updated successfully."
