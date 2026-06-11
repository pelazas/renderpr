# RenderPR Architecture

## Overview

RenderPR is a GitHub bot that visually reviews frontend Pull Requests. It provisions ephemeral infrastructure in the user's own AWS account (BYOC), captures UI screenshots across viewports, exposes a temporary live preview URL, and uses an LLM to analyze frontend regressions alongside the code diff. After the initial review, it keeps the Fargate task alive for conversational `@renderpr` commands until a 15-minute idle timeout.

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
- **AWS Services** — SSM Parameter Store, ECS, Lambda, API Gateway

## Directory Structure

```
renderpr/
├── cdk/                          # AWS CDK infrastructure (TypeScript)
│   ├── bin/
│   └── lib/                      # VPC, Lambda, API Gateway, Fargate
├── src/
│   ├── lambda_handler/           # Lambda handler (lightweight, fast boot)
│   │   ├── __init__.py
│   │   └── webhook_handler.py    # Validates HMAC, starts/dispatches to Fargate
│   └── agent/                    # Fargate entry point (stateful workspace)
│       ├── __init__.py
│       ├── main.py               # Container orchestration loop
│       ├── polling.py            # Command parsing + change session state
│       ├── visual.py             # Playwright browser driver
│       ├── routes.py             # Route/action/mock inference
│       ├── mock_server.py        # Temporary server-side mock API routes
│       ├── command_server.py     # HTTP command listener for live @renderpr commands
│       ├── code_edit.py          # LLM-driven file selection and edit generation
│       ├── editor.py             # Applies/reverts edits and re-screenshots
│       ├── network.py            # ECS metadata/public IP lookup
│       ├── review.py             # LLM prompt execution
│       └── config.py             # System constants
├── .renderpr/                    # Screenshot output directory (ephemeral)
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
  - Starts an ECS Fargate task for PR `opened`, `synchronize`, or `@renderpr review`
  - Dispatches `@renderpr code change`, `@renderpr apply`, and `@renderpr reject` to the existing task when one is still running
  - Cold-starts a task with a `COMMAND` environment variable when no running task exists
  - Returns 200 OK immediately to GitHub
- **State:** Stateless, single-request lifespan
- **IAM:** ECS run/list/describe/tag tasks, pass task roles, describe network interfaces
- **Timeout:** 30 seconds

### 2. Execution Sandbox (Fargate)

- **Runtime:** Docker container (Python 3.12 + Node.js)
- **Trigger:** ECS RunTask from Lambda
- **Responsibilities:**
  - Clones the PR branch via HTTPS (using installation access token)
  - Installs project dependencies (`npm ci`)
  - Starts dev server (`npm run dev`) on port 3000 with `HOST=0.0.0.0`
  - Orchestrates the full review lifecycle
  - Runs a command server on port 3001 for conversational code changes
  - Publishes a temporary live preview URL using the task public IP
  - Auto-terminates after 15 minutes idle
- **Networking:** Public subnet with public IP, outbound access, inbound 3000 for live preview and 3001 for Lambda command dispatch
- **Secrets:** Reads GitHub App private key + App ID from SSM Parameter Store via IAM task role

RenderPR separates server binding from internal browser access:

- Next dev server binds to `0.0.0.0` so the live preview can be reached via the Fargate task public IP.
- Playwright and health checks browse `http://localhost:3000` to avoid Next.js dev-origin/HMR blocking.
- The live preview URL is `http://<task-public-ip>:3000` and disappears when the Fargate task stops.

### 3. Visual Automation Agent (Playwright)

- **Runtime:** Playwright for Python
- **Responsibilities:**
  - Launches headless Chromium browser
  - Navigates to `http://localhost:3000`
  - Resizes viewports and captures screenshots
  - Keeps Playwright network mocks as a fallback for inferred API data
  - Logs console errors and network failures
- **Context:** Ephemeral — fresh browser context per command

### 4. Server-Side Runtime Mocking

- **Runtime:** Temporary files in the disposable Fargate clone
- **Responsibilities:**
  - Uses the same LLM-inferred mock data generated for screenshots
  - Writes temporary Next App Router API routes such as `src/app/api/users/route.ts`
  - Backs up existing files as `*.renderpr.bak` before overwriting
  - Patches or creates temporary Next config with `allowedDevOrigins` for the Fargate public IP
  - Tracks runtime-generated files separately from user-requested edits
- **Safety:** Generated mock/config files are never staged during `@renderpr apply`; only user-edited files in the change session are committed.

Server-side mocks make screenshots and the live preview consistent. A human opening the live preview hits the same temporary mocked API route that Playwright sees.

### 5. Command Server Agent

- **Runtime:** Python, inside Fargate main loop
- **Responsibilities:**
  - Listens on `0.0.0.0:3001`
  - Receives authenticated JSON commands from Lambda
  - Executes code-change/apply/reject handlers asynchronously
  - Tracks idle timeout (15 min since last interaction)
- **State:** In-memory `ChangeSession`, tracking user-edited files separately from runtime-generated files

Supported commands:

- `@renderpr review` — start a full review task
- `@renderpr code change: <request>` — generate and apply a temporary code edit, then post updated screenshots/live preview
- `@renderpr apply` — commit and push only the user-edited files
- `@renderpr reject` — revert pending user edits

### 6. Multimodal Review Agent (LLM)

- **Provider:** OpenRouter
- **Input:** Git code diff + Playwright screenshot buffers
- **Output:** Structured markdown critique covering:
  - Layout and responsiveness regressions
  - Usability issues
  - WCAG accessibility violations
  - Code quality observations
- **Reliability:** Exponential backoff with jitter on 429s, 3 retries over 30s

### 7. Infrastructure as Code (CDK)

- **Runtime:** AWS CDK v2 (TypeScript)
- **Responsibilities:**
  - Defines VPC with public subnets only
  - Creates ECS Fargate task definition + cluster
  - Provisions Lambda function + API Gateway route
  - Creates SSM Parameter Store SecureString parameters for secrets
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
[7] Agent fetches private key + App ID from SSM Parameter Store
[8] Agent generates JWT, exchanges for installation access token
[9] Agent clones PR branch (HTTPS with token)
[10] Agent discovers frontend package and gets task public IP
[11] Agent patches temporary Next config for public-IP dev origin
[12] Agent runs npm ci && starts npm run dev with HOST=0.0.0.0
[13] Agent infers affected routes, actions, and mock API data from the diff/source
[14] Agent writes temporary server-side mock API routes
[15] Playwright launches headless browser using http://localhost:3000
[16] Playwright captures screenshots at all viewport widths
[17] Agent sends diff + screenshots to OpenRouter LLM
[18] LLM returns structured markdown review
[19] Agent posts review plus live preview link as PR comment via GitHub API
[20] Agent starts command server and enters idle loop
```

### Conversational Flow

```
[21] GitHub sends issue_comment.created webhook for @renderpr command
[22] Lambda parses command
[23] If command is change/apply/reject, Lambda looks for a running task tagged with the PR number
[24] Lambda POSTs command to http://<task-public-ip>:3001/__renderpr/command
[25] If no running task exists, Lambda cold-starts Fargate with COMMAND env var
[26] Command server executes code change, apply, or reject
[27] For code changes, agent applies edit, re-screenshots, and posts updated comment
[28] For apply, agent stages only user-edited files, excluding runtime-generated mocks/config
[29] If idle > 15 min, task exits and the live preview URL stops working
```

## Authentication & Secret Management

### GitHub App Installation Tokens

RenderPR generates GitHub tokens dynamically — no static PATs are used.

1. CDK provisions SSM Parameter Store SecureString parameters
2. User runs `setup-secrets.sh` to upload private key (.pem), App ID, and webhook secret
3. Lambda reads webhook secret from environment (injected via CDK)
4. Fargate reads private key + App ID from SSM Parameter Store using IAM task role
5. Fargate agent signs a JWT using the private key, exchanges it for a 60-minute installation access token
6. Token is used for git clone, comment posting, and pushing applied changes
7. Token expires automatically; no manual rotation needed

### Variable Scope

| Variable | Injected Via | Accessed By | Purpose |
|----------|-------------|-------------|---------|
| Webhook secret | SSM Parameter Store | Lambda | HMAC validation |
| App ID | SSM Parameter Store (Fargate task role) | Fargate | JWT generation |
| Private key (.pem) | SSM Parameter Store (Fargate task role) | Fargate | JWT signing |
| Installation ID | ECS env override (Lambda) | Fargate | Token exchange |
| OpenRouter API key | SSM Parameter Store (Fargate exec role) | Fargate | LLM inference |
| RenderPR command token | CDK context/env | Lambda + Fargate | Command server authentication |

### Post-Deployment Secret Injection

```bash
# setup-secrets.sh — run once after cdk deploy
aws ssm put-parameter \
  --name /renderpr/github-app \
  --type SecureString \
  --value '{
    "app_id": "123456",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...",
    "webhook_secret": "your-webhook-secret"
  }' \
  --overwrite
```

## State Boundaries & Security

| Component | State Type | Lifespan | Permissions |
|-----------|-----------|----------|-------------|
| Webhook Lambda | Stateless | Single request | HMAC verify, ecs:RunTask |
| Fargate Sandbox | Stateful (isolated) | 15 min idle window | Local FS, git/node_modules, temp mock files |
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
| Next dev origin blocks live preview HMR | Temporary `allowedDevOrigins` patch using task public IP |
| API/data dependency unavailable in sandbox | Temporary server-side mock API routes generated from inferred mock data |

## Key Design Decisions

1. **Lambda + Fargate split** — Lambda handles the latency-sensitive webhook acknowledgment. Fargate provides the long-lived stateful environment for the 15-minute review cycle.
2. **Public subnets, no NAT Gateway** — The Fargate task only makes outbound calls (GitHub API, OpenRouter). Putting it in a public subnet with an egress-only security group avoids NAT Gateway costs (~$32/month).
3. **OpenRouter as primary LLM gateway** — Provides access to multiple models through one API, simplifies provider abstraction for future expansion.
4. **GitHub App tokens over PATs** — Short-lived (60 min), scoped per installation, generated at runtime. Aligns with BYOC zero-trust model.
5. **SSM Parameter Store with post-deploy injection** — CDK creates SecureString parameters; the user fills them via `setup-secrets.sh`. Free tier, encrypted at rest, same IAM integration as Secrets Manager.
6. **Internal localhost browsing, external public preview** — The app binds to `0.0.0.0`, but RenderPR browses `localhost` internally. This avoids Next.js dev-origin blocking while preserving a public preview link.
7. **Server-side runtime mocks for data** — Mock data is generated at runtime by the LLM based on the code diff. Temporary API route files serve that data to both Playwright screenshots and the live preview. Playwright route interception remains as a fallback, but generated files are tracked separately and excluded from commits.
8. **HTTP command dispatch instead of long polling inside the task** — GitHub comments trigger Lambda. Lambda dispatches commands to the running task's command server when possible, or cold-starts a task when needed.
