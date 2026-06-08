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
- Secrets Manager write access
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

The webhook URL can be updated after deployment. The webhook secret and private key are injected into Secrets Manager after deploy.

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

Note: `githubAppId` and `githubWebhookSecret` are not stored in CDK context. They are injected into Secrets Manager post-deploy.

### 4. Deploy the CDK Stack

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
- **Secrets Manager Secret ARN**

### 5. Inject Secrets

Create `setup-secrets.sh` at the project root:

```bash
#!/bin/bash
# Run once after cdk deploy
aws secretsmanager put-secret-value \
  --secret-id renderpr/github-app \
  --secret-string '{
    "app_id": "123456",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
    "webhook_secret": "your-webhook-secret"
  }'
```

Make it executable and run:

```bash
chmod +x setup-secrets.sh
./setup-secrets.sh
```

This populates the placeholder secret created by CDK. The Fargate task role has read-only access to this secret.

### 6. Update GitHub App Webhook URL

Copy the API Gateway URL from the CDK output and set it as your GitHub App's Webhook URL in the GitHub App settings.

### 7. Verify

1. Push a PR to a watched repository
2. Comment `@renderpr review` on the PR
3. Check the PR thread for a review comment from the bot

## Environment Variables

### Lambda Function

| Variable | Source | Purpose |
|----------|--------|---------|
| `GITHUB_WEBHOOK_SECRET` | Secrets Manager | HMAC signature validation |
| `ECS_CLUSTER_ARN` | CDK (auto) | Target cluster for RunTask |
| `ECS_TASK_DEFINITION_ARN` | CDK (auto) | Task definition for RunTask |
| `SUBNET_IDS` | CDK (auto) | Target public subnets |
| `SECURITY_GROUP_ID` | CDK (auto) | Security group for Fargate |
| `SECRETS_ARN` | CDK (auto) | ARN of the Secrets Manager secret |

### Fargate Container

| Variable | Source | Purpose |
|----------|--------|---------|
| `INSTALLATION_ID` | ECS env override (from Lambda) | GitHub installation ID |
| `REPO_FULL_NAME` | ECS env override (from Lambda) | "owner/repo" |
| `PR_NUMBER` | ECS env override (from Lambda) | PR to review |
| `OPENROUTER_API_KEY` | CDK context (injected) | LLM inference |
| `SECRETS_ARN` | CDK (auto) | Secrets Manager secret ARN |
| `AWS_REGION` | ECS task role | AWS region for SDK calls |
| `IDLE_TIMEOUT` | CDK context | Seconds before auto-termination |
| `POLL_INTERVAL` | CDK context | Seconds between poll cycles |

## Updating

```bash
cd cdk
npx cdk deploy
```

CDK handles diffing and updating only changed resources. The Secrets Manager secret is preserved across updates.

## Teardown

```bash
cd cdk
npx cdk destroy
```

This removes all AWS resources created by the stack. Secrets Manager secrets are deleted by default (enable deletion protection if needed).

## Troubleshooting

### Lambda timeout
Increase the Lambda timeout in CDK context or verify the webhook payload format.

### Fargate task fails to start
Check CloudWatch Logs for the task. Common causes:

- Missing secrets in Secrets Manager (run `setup-secrets.sh`)
- Invalid private key format (ensure newlines are `\n`)
- ECR image not found (first deploy: wait for image push)

### Container exits immediately
Verify `npm ci` and `npm run dev` succeed in the cloned repository. Check git clone permissions (installation token scope).

### No review comment posted
Check the OpenRouter API key validity and the GitHub installation access token generation. Verify the container has outbound internet access (public subnet + IGW route).
