# RenderPR — AGENTS.md

## Project Overview

GitHub bot that visually reviews frontend PR changes. It provisions ephemeral infrastructure, captures UI screenshots across viewports, and uses an LLM to analyze frontend regressions alongside the code diff. Deployed into the user's own AWS cloud (BYOC).

After the initial review, the bot enters a conversational state, polling the PR thread for `@renderpr` commands until a 15-minute idle timeout.

## Directory Structure

```
renderpr/
├── cdk/                          # AWS CDK infrastructure (TypeScript)
│   ├── bin/                      # CDK app entry point
│   └── lib/                      # Stack definitions: VPC, Lambda, API Gateway, Fargate
├── src/
│   ├── lambda_handler/           # Lambda handler (Python)
│   │   ├── __init__.py
│   │   └── webhook_handler.py    # HMAC validation, ECS RunTask trigger
│   └── agent/                    # Fargate container entry point (Python)
│       ├── __init__.py
│       ├── main.py               # Orchestration loop
│       ├── polling.py            # GitHub PR comment polling
│       ├── visual.py             # Playwright browser automation
│       ├── review.py             # LLM review execution
│       └── config.py             # System constants (timeouts, retries, viewports)
├── .renderpr/
│                                 # (ephemeral screenshot output dir)
├── tests/                        # Mirrors src/ structure
│   ├── test_lambda/
│   └── test_agent/
├── docs/
│   ├── ARCHITECTURE.md           # System architecture & design decisions
│   ├── DEPLOYMENT.md             # AWS deployment guide
│   └── WORKFLOW.md               # Agent runtime workflow
├── scripts/
│   └── setup-secrets.sh          # Post-deploy secret injection
├── Dockerfile                    # Fargate container image (Python + Node)
├── requirements.txt              # Python dependencies
├── AGENTS.md                     # This file
└── README.md                     # Human-facing project overview
```

## Required Reading

Before writing any code, read the following documents **in order**. They are not optional — code must be compliant with them.

1. **`docs/ARCHITECTURE.md`** — Component design, data flow, security model, key decisions
2. **`docs/DEPLOYMENT.md`** — How the system is deployed and configured
3. **`docs/WORKFLOW.md`** — The exact execution flow agents follow at runtime

## Stack

| Layer | Technology |
|-------|-----------|
| Router | AWS API Gateway + Lambda (Python 3.12) |
| Sandbox | AWS Fargate (ECS, Python + Node base image) |
| Visual Automation | Playwright (Python) |
| LLM Gateway | OpenRouter |
| Infrastructure | AWS CDK v2 (TypeScript) |
| GitHub Auth | Short-lived installation access tokens (JWT → API) |
| Secret Storage | AWS SSM Parameter Store (SecureString) |

## Coding Conventions

### General

- Python 3.12+ type hints on all function signatures and public methods
- TypeScript strict mode for CDK code
- No hardcoded timeouts or magic numbers — use `config.py` constants
- No secrets or keys in code, logs, or commits
- Functions under 50 lines; break down complex logic

### Python

- Use `pathlib` for filesystem paths
- Use `httpx` for HTTP client calls (not `requests`)
- Async I/O for polling loops and Playwright calls where practical
- `boto3` for AWS SDK calls
- Logging via `structlog` or standard library `logging` — never `print()`

### TypeScript (CDK)

- One stack per concern (network, compute, storage)
- Use `CfnOutput` for all deploy-time exports (API URL, ARNs)
- `aws-sdk` v3 style imports

### Testing

- `pytest` for Python tests (both unit and integration)
- Tests live in `tests/` mirroring `src/` structure
- Every feature includes a test; every bugfix starts with a failing test
- Use `moto` for AWS SDK mocking
- Use `pytest-asyncio` for async test support
- Playwright tests use Playwright's native test runner (or `pytest-playwright`)

## Development Workflow

### Making Changes

1. Read the relevant docs in `docs/` first
2. Understand the component you are changing and its boundaries (ARCHITECTURE.md)
3. Write tests first where applicable (TDD for bugfixes)
4. Implement the change
5. Run verification:
   ```bash
   # Python
   pytest tests/ --cov=src --cov-fail-under=80

   # TypeScript (CDK)
   cd cdk && npm run test

   # Lint
   ruff check src/ tests/
   mypy src/
   ```
6. Ensure all infrastructure changes are reflected in the CDK stack

### Bug Reporting Workflow

When a bug is reported:

1. Write a failing test that reproduces the bug BEFORE attempting any fix
2. Confirm the test fails as expected
3. Use subagents to try different fixes
4. A fix is only accepted when the test passes

## Compliance Rules

- All infrastructure changes must be reflected in the CDK stack
- Secrets and keys must never be logged, committed, or exposed
- Every new feature must include a corresponding test
- Playwright screenshots must be captured at all defined viewport widths
- No hardcoded timeouts — use the configurable constants in `src/agent/config.py`

## Deployment (eu-west-1)

| Output | Value |
|--------|-------|
| ApiGatewayUrl | `https://drdidhjxv8.execute-api.eu-west-1.amazonaws.com/` |
| ClusterArn | `arn:aws:ecs:eu-west-1:303859149452:cluster/renderpr-cluster` |
| GitHubParamName | `/renderpr/github-app` |
| TaskDefinitionArn | `arn:aws:ecs:eu-west-1:303859149452:task-definition/renderpr-review:12` |
| ScreenshotBucketName | `renderpr-screenshots-303859149452` |
| StackArn | `arn:aws:cloudformation:eu-west-1:303859149452:stack/RenderprStack/e0b45ae0-6347-11f1-86a8-0668387b3249` |

## Key Constraints

- Fargate runs in **public subnets** with `assignPublicIp: true` and egress-only security groups. No NAT Gateway. Do not add private subnets without explicit approval.
- GitHub auth uses **installation access tokens** (JWT-signed, 60min expiry). No PATs.
- Secrets are stored in **AWS SSM Parameter Store** (SecureString) with post-deploy injection via `setup-secrets.sh`. Not in CDK context or env vars at deploy time.

## Deployment Outputs

After every `cdk deploy`, paste the `Outputs:` block here and say "store deploy info" — I'll update the table above.
