#!/usr/bin/env bash
set -euo pipefail

if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is not installed." >&2
  exit 1
fi

if ! command -v openssl &> /dev/null; then
  echo "Error: openssl is not installed." >&2
  exit 1
fi

echo "=== RenderPR — CloudFront Signing Key Setup ==="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUB_PATH="$SCRIPT_DIR/../cdk/cloudfront-public-key.pem"
PRIV_TMP="$(mktemp)"
trap 'rm -f "$PRIV_TMP"' EXIT

echo "Generating RSA-2048 key pair..."
openssl genrsa -out "$PRIV_TMP" 2048 2>/dev/null
openssl rsa -in "$PRIV_TMP" -pubout -out "$PUB_PATH" 2>/dev/null

echo "Storing private key in SSM SecureString /renderpr/cloudfront-private-key..."
aws ssm put-parameter \
  --name /renderpr/cloudfront-private-key \
  --type SecureString \
  --value "file://$PRIV_TMP" \
  --overwrite \
  --output json > /dev/null

echo ""
echo "Done."
echo "  Private key -> SSM /renderpr/cloudfront-private-key (SecureString)"
echo "  Public key  -> $PUB_PATH (read by CDK at deploy; gitignored)"
echo ""
echo "Next steps:"
echo "  1. cd cdk && npx cdk deploy"
echo "  2. ./scripts/setup-secrets.sh"
