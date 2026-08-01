# RenderPR Deployment Guide

## Prerequisites

- **Node.js 18+** (for CDK)
- **Python 3.12+** (for Lambda and agent code)
- **AWS CLI** configured with credentials (admin or sufficient IAM permissions)
- **AWS CDK CLI** (`npm install -g aws-cdk`)
- **Docker** (for building the Fargate image)
- **A GitHub App** registered in your account/organization

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-org/renderpr.git
cd renderpr

# 2. Install CDK dependencies
cd cdk && npm install && cd ..

# 3. Bootstrap CDK (one-time per AWS account/region)
cdk bootstrap

# 4. Deploy the stack
cdk deploy

# 5. Inject GitHub App secrets
./setup-secrets.sh
```

## Step-by-Step

### 1. AWS Account Setup

Ensure your AWS CLI is configured:

```bash
aws configure
```

Required IAM permissions for the deploying user:

- CloudFormation full access
- IAM role creation
- ECS cluster + task definition creation
- Lambda function creation
- API Gateway creation
- SSM Parameter Store write access
- EC2 VPC + security group creation
- ECR repository push

CDK will create a dedicated `cdk-*` IAM role during bootstrap.

### 2. GitHub App Registration

Create a GitHub App in your account or organization:

1. Go to **Settings > Developer Settings > GitHub Apps > New App**
2. Set the **Webhook URL** to your API Gateway endpoint (shown after `cdk deploy`)
3. Set a **Webhook Secret** (save this for later)
4. Generate a **Private Key** (.pem file) — download and save securely
5. Note the **App ID** (shown on the app's page)
6. Install the app on your repository (or all repos)

The webhook URL can be updated after deployment. The webhook secret and private key are injected into SSM Parameter Store after deploy.

### 3. Configure CDK Context

Edit `cdk/cdk.json` or pass context parameters at deploy time:

```json
{
  "context": {
    "appName": "renderpr",
    "openrouterApiKey": "sk-or-v1-...",
    "repositoryAllowlist": ["owner/repo-name"],
    "fargateCpu": "256",
    "fargateMemory": "512",
    "idleTimeoutSeconds": "900",
    "pollIntervalSeconds": "10",
    "commandToken": "shared-command-token"
  }
}
```

| Context Variable | Description | Default |
|-----------------|-------------|---------|
| `appName` | Prefix for all AWS resources | `renderpr` |
| `openrouterApiKey` | OpenRouter API key | required |
| `repositoryAllowlist` | Comma-separated "owner/repo" patterns | `["*"]` |
| `fargateCpu` | CPU units for the Fargate task | `256` |
| `fargateMemory` | Memory (MiB) for the Fargate task | `512` |
| `idleTimeoutSeconds` | Max idle time before self-termination | `900` |
| `pollIntervalSeconds` | Legacy/default interval setting | `10` |
| `commandToken` | Shared token Lambda uses to authenticate with the task command server | required for conversational commands |

Note: `githubAppId` and `githubWebhookSecret` are not stored in CDK context. They are injected into SSM Parameter Store post-deploy. `commandToken` must be set consistently for Lambda and Fargate because the command server rejects unauthenticated requests.

### 4. Create SSM Parameters

CloudFormation doesn't support `SecureString` parameters, so create them manually before deploying:

```bash
aws ssm put-parameter --name /renderpr/github-app --type SecureString --value '{}'
aws ssm put-parameter --name /renderpr/openrouter --type SecureString --value "placeholder" --overwrite
```

These are placeholders. You'll fill in real values in step 5.

### 5. Deploy the CDK Stack

Before the first deploy, create the CloudFront signing key pair (see
[Screenshot delivery](#screenshot-delivery-cloudfront-signed-urls) below):

```bash
./scripts/setup-cloudfront-key.sh
```

```bash
cd cdk
npm install
npx cdk bootstrap   # One-time per account/region
npx cdk deploy      # Deploy or update
```

The first deployment takes several minutes (VPC creation, ECR image push).

After deployment, the output shows:

- **API Gateway URL** — use this as your GitHub App webhook URL
- **ECS Cluster ARN**
- **SSM Parameter Name** — `/renderpr/github-app` and `/renderpr/openrouter`
- **Task Definition ARN**
- **Screenshot Bucket Name**

### 6. Inject Secrets

Run the post-deploy script to fill the GitHub App credentials:

```bash
./scripts/setup-secrets.sh
```

This uploads the GitHub App ID, private key, and webhook secret into the `/renderpr/github-app` parameter. The Fargate task role has read-only access.

For the OpenRouter API key, update its parameter:

```bash
aws ssm put-parameter \
  --name /renderpr/openrouter \
  --type SecureString \
  --value "sk-or-v1-..." \
  --overwrite
```

### 7. Cap ECR Storage Growth

Run this once per bootstrapped account:

```bash
./scripts/setup-ecr-lifecycle.sh
```

Every `cdk deploy` pushes a new container image to the bootstrap asset repository and never removes the old one. The repository ships with a lifecycle rule that expires only *untagged* images, but CDK tags every asset with its content hash, so that rule never matches and storage grows without bound.

The script adds a rule that keeps the 5 most recent images and expires the rest. The deployed image is always the newest, so it is never expired; the extra copies exist as rollback targets. Override the count with `KEEP=10 ./scripts/setup-ecr-lifecycle.sh`.

### 8. Update GitHub App Webhook URL

Copy the API Gateway URL from the CDK output and set it as your GitHub App's Webhook URL in the GitHub App settings.

### 9. Verify

1. Push a PR to a watched repository
2. Comment `@renderpr review` on the PR
3. Check the PR thread for a review comment from the bot
4. Open the `Live app: http://<public-ip>:3000` link while the task is still running
5. Comment `@renderpr code change: <request>` to verify conversational edits
6. Use `@renderpr apply` to commit pending edits (leave them uncommitted to discard)

The live app URL uses the Fargate task's ephemeral public IP. It is expected to stop working after the task exits its idle window.

## Screenshot delivery (CloudFront signed URLs)

Screenshots can show private/authenticated UI, so the S3 screenshot bucket is
**fully private** (no public read). Screenshots are served through **CloudFront
with Origin Access Control (OAC)**, and every URL posted in a PR comment is a
**signed URL** — only RenderPR's private key can mint a working link.

Before the first `cdk deploy`, run:

```bash
./scripts/setup-cloudfront-key.sh
```

This generates an RSA-2048 key pair, stores the **private** key in SSM
SecureString `/renderpr/cloudfront-private-key`, and writes the **public** key
to `cdk/cloudfront-public-key.pem` (gitignored), which CDK reads at deploy to
provision the CloudFront public key and key group. The Fargate task signs URLs
at runtime using the private key from SSM.

Signed URLs expire after 7 days, matching the bucket's screenshot retention
lifecycle, so links stay valid for the screenshots' whole life.

## Environment Variables

### Lambda Function

| Variable | Source | Purpose |
|----------|--------|---------|
| `GITHUB_WEBHOOK_SECRET` | SSM Parameter Store | HMAC signature validation |
| `ECS_CLUSTER_ARN` | CDK (auto) | Target cluster for RunTask |
| `ECS_TASK_DEFINITION_ARN` | CDK (auto) | Task definition for RunTask |
| `SUBNET_IDS` | CDK (auto) | Target public subnets |
| `SECURITY_GROUP_ID` | CDK (auto) | Security group for Fargate |
| `GITHUB_PARAM_NAME` | CDK (auto) | SSM parameter name for GitHub App credentials |
| `RENDERPR_COMMAND_TOKEN` | CDK context/env | Auth token for task command server |

### Fargate Container

| Variable | Source | Purpose |
|----------|--------|---------|
| `INSTALLATION_ID` | ECS env override (from Lambda) | GitHub installation ID |
| `REPO_FULL_NAME` | ECS env override (from Lambda) | "owner/repo" |
| `PR_NUMBER` | ECS env override (from Lambda) | PR to review |
| `OPENROUTER_API_KEY` | SSM Parameter Store (via ECS secrets) | LLM inference |
| `GITHUB_PARAM_NAME` | CDK (auto) | SSM parameter name for GitHub App credentials |
| `AWS_REGION` | ECS task role | AWS region for SDK calls |
| `IDLE_TIMEOUT` | CDK context | Seconds before auto-termination |
| `POLL_INTERVAL` | CDK context | Seconds between poll cycles |
| `RENDERPR_COMMAND_TOKEN` | CDK context/env | Auth token for command server requests |
| `COMMAND` | ECS env override (from Lambda) | Cold-start command to execute, if any |
| `CLOUDFRONT_DOMAIN` | CDK (auto) | CloudFront distribution domain for signed screenshot URLs |
| `CLOUDFRONT_KEY_PAIR_ID` | CDK (auto) | CloudFront public key ID used to sign URLs |
| `CLOUDFRONT_PRIVATE_KEY_PARAM` | CDK (auto) | SSM parameter name holding the URL-signing private key |

### Network Ports

| Port | Direction | Purpose |
|------|-----------|---------|
| `3000` | Public inbound to Fargate | Live preview app served by `npm run dev` |
| `3001` | Public inbound to Fargate | RenderPR command server for Lambda dispatch |

RenderPR intentionally runs Fargate in public subnets with `assignPublicIp: true`. Do not move tasks to private subnets or add NAT unless the architecture is explicitly changed.

## Updating

```bash
cd cdk
npx cdk deploy
```

CDK handles diffing and updating only changed resources. SSM parameters are preserved across updates (set with `--overwrite`).

The GitHub Actions deploy workflow rebuilds and redeploys when deployment-relevant paths change, including `src/**`, `cdk/**`, `Dockerfile`, and `.github/workflows/deploy.yml`.

## Teardown

```bash
cd cdk
npx cdk destroy
```

This removes all AWS resources created by the stack. SSM parameters are not deleted (they persist in the account).

## Troubleshooting

### Lambda timeout
Increase the Lambda timeout in CDK context or verify the webhook payload format.

### Fargate task fails to start
Check CloudWatch Logs for the task. Common causes:

- Missing SSM parameters (run `scripts/setup-secrets.sh`)
- Invalid private key format (ensure newlines are `\n`)
- ECR image not found (first deploy: wait for image push)

### Container exits immediately
Verify `npm ci` and `npm run dev` succeed in the cloned repository. Check git clone permissions (installation token scope).

### Native module compilation fails during npm ci
If the cloned project depends on native modules (`better-sqlite3`, `sharp`, `canvas`, `node-sass`, etc.), the container needs build tools to compile them. The Docker image includes `build-essential`, `libsqlite3-dev`, `libpng-dev`, `libjpeg-dev`, `libpixman-1-dev`, `libcairo2-dev`, `libpango1.0-dev`, and `pkg-config`. If a project needs a library not in this list, extend the `apt-get install` line in `Dockerfile` and re-deploy.

### No review comment posted
Check the OpenRouter API key validity and the GitHub installation access token generation. Verify the container has outbound internet access (public subnet + IGW route).

### Live preview link does not work
Verify the task is still running and within the idle timeout. The live preview uses the task's ephemeral public IP on port 3000, so the URL dies when the task exits.

### Screenshots work but live preview shows loading data
Check CloudWatch logs for temporary mock route generation. RenderPR writes server-side mock API routes for inferred API data so public live preview and screenshots see the same data. Playwright network mocks only affect screenshots and do not help an external browser.

### Next.js blocks dev resources from the public IP
RenderPR patches or creates temporary Next config with `allowedDevOrigins` for the task public IP. If HMR or dev resources are blocked, inspect the generated Next config logs and confirm the public IP was detected.

### `@renderpr code change` does nothing
Check Lambda logs for command parsing and dispatch. Lambda should find a running task tagged for the PR and POST to port 3001. If no task is running, Lambda should cold-start a new task with the command in `COMMAND`.
