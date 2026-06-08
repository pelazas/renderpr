# RenderPR Architecture

## Overview

RenderPR is a GitHub bot that visually reviews frontend Pull Requests. It provisions ephemeral infrastructure in the user's own AWS account (BYOC), captures UI screenshots across viewports, and uses an LLM to analyze frontend regressions alongside the code diff. After the initial review, it enters a conversational polling mode, responding to `@renderpr` commands until a 15-minute idle timeout.

## System Context

```
[GitHub] ──webhook──> [API Gateway] ──> [Lambda]
                                            │
                                     RunTask (env overrides)
                                            │
                                            ▼
                                     [Fargate Task]
                                            │
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                      [GitHub API]   [OpenRouter LLM]  [Internet]
                       (clone PR,    (analyze diffs     (outbound only
                        post review)  + screenshots)     via IGW)
```

RenderPR is not a SaaS. Every user deploys the full stack into their own AWS account. The bot communicates with three external services:

- **GitHub** — webhook events, REST API for comments and cloning
- **OpenRouter** — LLM inference
- **AWS Services** — Secrets Manager, ECS, Lambda, API Gateway

## Directory Structure

```
renderpr/
├── cdk/                          # AWS CDK infrastructure (TypeScript)
│   ├── bin/
│   └── lib/                      # VPC, Lambda, API Gateway, Fargate
├── src/
│   ├── lambda/                   # Lambda handler (lightweight, fast boot)
│   │   ├── __init__.py
│   │   └── webhook_handler.py    # Validates HMAC, triggers Fargate
│   └── agent/                    # Fargate entry point (stateful workspace)
│       ├── __init__.py
│       ├── main.py               # Container orchestration loop
│       ├── polling.py            # GitHub REST API long-polling
│       ├── visual.py             # Playwright browser driver
│       ├── review.py             # LLM prompt execution
│       └── config.py             # System constants
├── .renderpr/
│   └── fixtures/                 # JSON mock data for frontend interception
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEPLOYMENT.md
│   └── WORKFLOW.md
├── Dockerfile                    # Packages src/agent/ for Fargate
├── requirements.txt
├── AGENTS.md                     # AI agent workflow instructions
└── README.md                     # Human onboarding
```

## Core Components

### 1. Webhook Router (Lambda)

- **Runtime:** Python 3.12
- **Trigger:** API Gateway receiving GitHub App webhooks
- **Responsibilities:**
  - Validates HMAC-SHA256 signature using the webhook secret
  - Parses `installation.id`, `repository.full_name`, `pull_request.number` from payload
  - Calls ECS `RunTask` API with environment variable overrides
  - Returns 200 OK immediately to GitHub
- **State:** Stateless, single-request lifespan
- **IAM:** Minimal — only `ecs:RunTask` and `ecs:DescribeTasks`
- **Timeout:** Configured for fast response (< 10s expected)

### 2. Execution Sandbox (Fargate)

- **Runtime:** Docker container (Python 3.12 + Node.js)
- **Trigger:** ECS RunTask from Lambda
- **Responsibilities:**
  - Clones the PR branch via HTTPS (using installation access token)
  - Installs project dependencies (`npm ci`)
  - Starts dev server (`npm run dev`) on port 3000
  - Orchestrates the full review lifecycle
  - Auto-terminates after 15 minutes idle
- **Networking:** Public subnet with public IP, egress-only security group
- **Secrets:** Reads GitHub App private key + App ID from Secrets Manager via IAM task role

### 3. Visual Automation Agent (Playwright)

- **Runtime:** Playwright for Python
- **Responsibilities:**
  - Launches headless Chromium browser
  - Navigates to `http://localhost:3000`
  - Resizes viewports and captures screenshots
  - Intercepts API requests via Playwright Router API or MSW
  - Returns fixture data from `.renderpr/fixtures/`
  - Logs console errors and network failures
- **Context:** Ephemeral — fresh browser context per command

### 4. Conversation Polling Agent

- **Runtime:** Python, inside Fargate main loop
- **Responsibilities:**
  - Polls GitHub PR issue comments every 10 seconds
  - Detects `@renderpr` keyword in new comments
  - Extracts natural language commands
  - Notifies main orchestrator to re-execute visual automation
  - Tracks idle timeout (15 min since last interaction)
- **State:** In-memory, stores last interaction timestamp and last seen comment ID

### 5. Multimodal Review Agent (LLM)

- **Provider:** OpenRouter
- **Input:** Git code diff + Playwright screenshot buffers
- **Output:** Structured markdown critique covering:
  - Layout and responsiveness regressions
  - Usability issues
  - WCAG accessibility violations
  - Code quality observations
- **Reliability:** Exponential backoff with jitter on 429s, 3 retries over 30s

### 6. Infrastructure as Code (CDK)

- **Runtime:** AWS CDK v2 (TypeScript)
- **Responsibilities:**
  - Defines VPC with public subnets only
  - Creates ECS Fargate task definition + cluster
  - Provisions Lambda function + API Gateway route
  - Creates Secrets Manager placeholder for GitHub App secrets
  - Defines IAM roles and security groups
  - Packages and deploys everything via `cdk deploy`

## Data Flow

### Initial Review Pipeline

```
[1] GitHub App sends webhook to API Gateway
[2] Lambda validates HMAC signature
[3] Lambda parses installation.id, repo, PR number
[4] Lambda calls ECS RunTask with env overrides
[5] Lambda returns 200 OK to GitHub
[6] Fargate container boots
[7] Agent fetches private key + App ID from Secrets Manager
[8] Agent generates JWT, exchanges for installation access token
[9] Agent clones PR branch (HTTPS with token)
[10] Agent runs npm ci && npm run dev
[11] Playwright launches headless browser
[12] Playwright captures screenshots at all viewport widths
[13] Agent extracts git diff
[14] Agent sends diff + screenshots to OpenRouter LLM
[15] LLM returns structured markdown review
[16] Agent posts review as PR comment via GitHub API
[17] Agent enters polling loop
```

### Conversational Flow

```
[18] Agent polls PR comments every 10s
[19] New comment containing @renderpr detected
[20] Agent extracts command
[21] Agent re-runs Playwright with command parameters
[22] Agent sends new screenshots + context to LLM
[23] Agent posts follow-up review comment
[24] Back to step 18
[25] If idle > 15 min, post closing summary and exit
```

## Authentication & Secret Management

### GitHub App Installation Tokens

RenderPR generates GitHub tokens dynamically — no static PATs are used.

1. CDK provisions empty AWS Secrets Manager secret
2. User runs `setup-secrets.sh` to upload private key (.pem), App ID, and webhook secret
3. Lambda reads webhook secret from environment (injected via CDK)
4. Fargate reads private key + App ID from Secrets Manager using IAM task role
5. Fargate agent signs a JWT using the private key, exchanges it for a 60-minute installation access token
6. Token is used for git clone, comment posting, and thread polling
7. Token expires automatically; no manual rotation needed

### Variable Scope

| Variable | Injected Via | Accessed By | Purpose |
|----------|-------------|-------------|---------|
| Webhook secret | Lambda env var (CDK) | Lambda | HMAC validation |
| App ID | Secrets Manager (Fargate role) | Fargate | JWT generation |
| Private key (.pem) | Secrets Manager (Fargate role) | Fargate | JWT signing |
| Installation ID | ECS env override (Lambda) | Fargate | Token exchange |
| OpenRouter API key | Fargate env var (CDK) | Fargate | LLM inference |

### Post-Deployment Secret Injection

```bash
# setup-secrets.sh — run once after cdk deploy
aws secretsmanager put-secret-value \
  --secret-id renderpr/github-app \
  --secret-string '{
    "app_id": "123456",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
    "webhook_secret": "your-webhook-secret"
  }'
```

## State Boundaries & Security

| Component | State Type | Lifespan | Permissions |
|-----------|-----------|----------|-------------|
| Webhook Lambda | Stateless | Single request | HMAC verify, ecs:RunTask |
| Fargate Sandbox | Stateful (isolated) | Max 15 min | Local FS, git/node_modules |
| Playwright | Ephemeral | Per command | Isolated cookies/storage |
| LLM Layer | Stateless | Per tick | OpenRouter key from env |

## Failure Modes

| Failure | Mitigation |
|---------|-----------|
| npm compilation crash | Capture stderr, post diagnostic comment, graceful exit |
| LLM rate limit (429) | Exponential backoff + jitter, 3 retries over 30s |
| Idle timeout (15 min) | Post summary, self-destruct container |
| Git clone failure | Retry once, post error comment, exit |
| Secrets access failure | Log context-free error, exit with status 1 |

## Key Design Decisions

1. **Lambda + Fargate split** — Lambda handles the latency-sensitive webhook acknowledgment. Fargate provides the long-lived stateful environment for the 15-minute review cycle.
2. **Public subnets, no NAT Gateway** — The Fargate task only makes outbound calls (GitHub API, OpenRouter). Putting it in a public subnet with an egress-only security group avoids NAT Gateway costs (~$32/month).
3. **OpenRouter as primary LLM gateway** — Provides access to multiple models through one API, simplifies provider abstraction for future expansion.
4. **GitHub App tokens over PATs** — Short-lived (60 min), scoped per installation, generated at runtime. Aligns with BYOC zero-trust model.
5. **Secrets Manager with post-deploy injection** — CDK creates the secret skeleton; the user fills it via a script. Avoids storing secrets in CloudFormation state.
6. **Playwright Router API for data mocking** — No backend needed. Fixtures live in the repo under `.renderpr/fixtures/`. Enables deterministic testing of loading, empty, error, and populated states.
