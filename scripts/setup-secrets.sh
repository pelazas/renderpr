#!/usr/bin/env bash
set -euo pipefail

if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is not installed." >&2
  exit 1
fi

echo "=== RenderPR — Post-Deploy Secret Injection ==="
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

read -rsp "OpenRouter API Key: " OPENROUTER_KEY
echo ""

GITHUB_SECRET_JSON=$(jq -n \
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
  --name "/renderpr/github-app" \
  --type "SecureString" \
  --value "$GITHUB_SECRET_JSON" \
  --overwrite \
  --output json > /dev/null

aws ssm put-parameter \
  --name "/renderpr/openrouter" \
  --type "SecureString" \
  --value "$OPENROUTER_KEY" \
  --overwrite \
  --output json > /dev/null

echo ""
echo "SSM parameters updated:"
echo "  /renderpr/github-app"
echo "  /renderpr/openrouter"
echo ""

if aws ssm get-parameter --name "/renderpr/renderpr-command-token" --with-decryption > /dev/null 2>&1; then
  echo "SSM parameter /renderpr/renderpr-command-token already exists."
else
  echo "Generating new command token..."
  TOKEN=$(openssl rand -hex 32)
  aws ssm put-parameter \
    --name "/renderpr/renderpr-command-token" \
    --type "SecureString" \
    --value "$TOKEN" \
    --overwrite \
    --output json > /dev/null
  echo "SSM parameter /renderpr/renderpr-command-token created."
fi
