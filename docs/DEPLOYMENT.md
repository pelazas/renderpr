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
    "pollIntervalSeconds": "10"
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
| `pollIntervalSeconds` | GitHub comment poll interval | `10` |

Note: `githubAppId` and `githubWebhookSecret` are not stored in CDK context. They are injected into SSM Parameter Store post-deploy.

### 4. Create SSM Parameters

CloudFormation doesn't support `SecureString` parameters, so create them manually before deploying:

```bash
aws ssm put-parameter --name /renderpr/github-app --type SecureString --value '{}'
aws ssm put-parameter --name /renderpr/openrouter --type SecureString --value "placeholder" --overwrite
```

These are placeholders. You'll fill in real values in step 5.

### 5. Deploy the CDK Stack

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

### 7. Update GitHub App Webhook URL

Copy the API Gateway URL from the CDK output and set it as your GitHub App's Webhook URL in the GitHub App settings.

### 8. Verify

1. Push a PR to a watched repository
2. Comment `@renderpr review` on the PR
3. Check the PR thread for a review comment from the bot

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

## Updating

```bash
cd cdk
npx cdk deploy
```

CDK handles diffing and updating only changed resources. SSM parameters are preserved across updates (set with `--overwrite`).

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

### No review comment posted
Check the OpenRouter API key validity and the GitHub installation access token generation. Verify the container has outbound internet access (public subnet + IGW route).
