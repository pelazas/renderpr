#!/usr/bin/env bash
set -euo pipefail

# Caps the size of the CDK container-asset repository.
#
# Every `cdk deploy` pushes a new image and never removes the old one. The
# bootstrap repo ships with a lifecycle rule that only expires *untagged*
# images, but CDK tags every asset with its content hash, so that rule never
# matches and the repo grows without bound. This adds a rule that does match.

if ! command -v aws &> /dev/null; then
  echo "Error: AWS CLI is not installed." >&2
  exit 1
fi

# Number of recent images to keep. The deployed image is always the newest, so
# it is never expired; the extra copies exist to keep a rollback target around.
KEEP="${KEEP:-5}"

# Days to retain untagged images. These only appear when a tag is overwritten,
# so they are dead on arrival.
UNTAGGED_DAYS="${UNTAGGED_DAYS:-1}"

# The CDK bootstrap qualifier. Override only if you bootstrapped with a custom
# one via `cdk bootstrap --qualifier`.
QUALIFIER="${CDK_QUALIFIER:-hnb659fds}"

echo "=== RenderPR — ECR Lifecycle Policy ==="
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=$(aws configure get region)
if [ -z "$REGION" ]; then
  echo "Error: no AWS region configured. Set one with 'aws configure'." >&2
  exit 1
fi

REPO="cdk-${QUALIFIER}-container-assets-${ACCOUNT_ID}-${REGION}"

if ! aws ecr describe-repositories \
  --region "$REGION" \
  --repository-names "$REPO" \
  --output json > /dev/null 2>&1; then
  echo "Error: repository not found: $REPO" >&2
  echo "Has this account been bootstrapped? Run 'cdk bootstrap'." >&2
  exit 1
fi

echo "Repository: $REPO"
echo "Keeping the $KEEP most recent images."
echo ""

# A rule with tagStatus "any" must carry the highest rulePriority, so the
# untagged sweep is evaluated first.
POLICY=$(jq -n \
  --argjson keep "$KEEP" \
  --argjson untagged_days "$UNTAGGED_DAYS" \
  '{
    rules: [
      {
        rulePriority: 1,
        description: "Expire untagged images",
        selection: {
          tagStatus: "untagged",
          countType: "sinceImagePushed",
          countUnit: "days",
          countNumber: $untagged_days
        },
        action: { type: "expire" }
      },
      {
        rulePriority: 2,
        description: "Keep only the most recent CDK asset images",
        selection: {
          tagStatus: "any",
          countType: "imageCountMoreThan",
          countNumber: $keep
        },
        action: { type: "expire" }
      }
    ]
  }'
)

aws ecr put-lifecycle-policy \
  --region "$REGION" \
  --repository-name "$REPO" \
  --lifecycle-policy-text "$POLICY" \
  --output json > /dev/null

echo "Lifecycle policy applied:"
echo "  1. untagged images expire after ${UNTAGGED_DAYS}d"
echo "  2. all but the ${KEEP} most recent images expire"
echo ""
echo "ECR evaluates the policy on its own schedule (usually within 24h)."
echo "To see what it would remove first:"
echo ""
echo "  aws ecr start-lifecycle-policy-preview --region $REGION \\"
echo "    --repository-name $REPO --output json"
