# RenderPR Agent Workflow

This document describes the exact step-by-step workflow an agent follows when processing a GitHub Pull Request event. Every step includes inputs, outputs, error handling, and retry logic.

## 1. Webhook Received (Lambda)

**Trigger:** GitHub App sends a `pull_request.opened`, `pull_request.synchronize`, or `issue_comment.created` webhook to API Gateway.

**Steps:**

1. API Gateway proxies the request to Lambda
2. Lambda reads the `X-Hub-Signature-256` header
3. Lambda computes HMAC-SHA256 of the request body using the webhook secret
4. If HMAC doesn't match → return 401
5. Lambda parses the JSON body to extract:
   - `installation.id`
   - `repository.full_name`
   - `pull_request.number` (or `issue.number` for comments)
   - `action` (opened, synchronize, created)
6. If `action` is not a trigger event → return 200 OK (no-op)
7. For `issue_comment.created`, Lambda parses `@renderpr` commands:
   - empty/help/review → full review
   - `code change: <request>` or `change <request>` → code edit command
   - `apply` → apply pending user edit
   - `reject` → ignored (no-op; an unwanted change is left uncommitted)
8. For `change` or `apply`, Lambda looks for a running ECS task tagged with `PRNumber`.
9. If a running task exists, Lambda resolves the task public IP and POSTs to `http://<ip>:3001/__renderpr/command`.
10. If dispatch fails or no task is running, Lambda cold-starts ECS with a `COMMAND` environment variable.
11. For full reviews, Lambda calls ECS `RunTask` with environment overrides:
   - `INSTALLATION_ID`
   - `REPO_FULL_NAME`
   - `PR_NUMBER`
12. Lambda returns 200 OK to GitHub

**Error handling:**

- HMAC mismatch: log minimal info, return 401, no retry
- RunTask API failure: log error, return 500, GitHub retries webhook
- Command dispatch failure: log error, cold-start a task for the command
- Unparseable payload: log, return 400

## 2. Fargate Container Boots

**Trigger:** ECS launches the Fargate task with the injected environment variables.

**Steps:**

1. Docker container starts, runs `main.py`
2. Agent authenticates with SSM Parameter Store:
   a. `boto3.client("ssm").get_parameter(Name=GITHUB_PARAM_NAME, WithDecryption=True)`
   b. Parse JSON to extract `app_id` and `private_key`
3. Agent generates GitHub installation access token:
   a. Create JWT with `iat` (now) and `exp` (now + 10 min) using RS256
   b. Exchange JWT for token via `POST /app/installations/{id}/access_tokens`
   c. Extract `token` from response
4. Agent clones the repository:
   ```bash
   git clone https://x-access-token:{token}@github.com/{repo}.git .
   git fetch origin pull/{pr}/head:review-pr
   git checkout review-pr
   ```
5. Agent fetches the Fargate task public IP from ECS container metadata + EC2 ENI lookup
6. Agent temporarily patches/creates Next config so `allowedDevOrigins` includes the public IP
7. Agent installs dependencies: `npm ci`
8. Agent starts the dev server with `HOST=0.0.0.0 npm run dev`
9. Agent waits for dev server readiness:
   - Poll `http://localhost:3000` until 200 or 60s timeout
   - Interval: 2s

Important host split:

- The dev server binds to `0.0.0.0` so the live preview is reachable at `http://<public-ip>:3000`.
- RenderPR's own health checks and Playwright browser use `http://localhost:3000` to avoid Next.js dev-origin/HMR blocking.

**Error handling:**

- SSM Parameter Store access denied: log, exit with status 1
- JWT generation failure: log, exit with status 1
- Token exchange fails: log, exit with status 1
- Git clone fails: retry once, then post error comment, exit with status 1
- `npm ci` fails: capture stderr, post diagnostic comment, exit gracefully
- Dev server timeout: capture stderr, post diagnostic comment, exit gracefully
- Public IP lookup failure: fall back to `localhost`; live preview link may not be useful, but screenshots still run

## 3. Initial Review

**Trigger:** Dev server is ready.

**Steps:**

1. Get the PR code diff:
   - `GET /repos/{owner}/{repo}/pulls/{pr_number}` with `Accept: application/vnd.github.v3.diff`
2. Discover frontend package from the diff and repository structure
3. Route inference LLM analyzes:
   - filtered frontend diff
   - full changed frontend file contents
   - reverse dependencies
   - repo tree
4. Route inference returns:
   - affected routes
   - required interactions
   - mock API response data
5. Agent writes temporary server-side mock API routes for inferred mocks:
   - `/api/users` → `src/app/api/users/route.ts`
   - existing files are backed up as `*.renderpr.bak`
6. Launch headless Chromium via Playwright
7. Register Playwright network mocks as fallback
8. For each inferred route and configured viewport width (`[375, 768, 1280, 1920]`):
   a. Set browser viewport size
   b. Navigate to `http://localhost:3000/<route>`
   c. Wait for network idle and settle delay
   d. Execute inferred actions, if any
   e. Capture full-page screenshot
   f. Collect console errors and network failures
9. Upload screenshots to S3
10. Send review request to OpenRouter:
   - System prompt with review instructions
   - User message containing the diff text and screenshots as base64 images
11. Parse LLM response into structured markdown
12. Append live preview link: `http://<public-ip>:3000`
13. Post review comment via `POST /repos/{owner}/{repo}/issues/{pr_number}/comments`
14. Start command server on `0.0.0.0:3001`
15. Enter idle wait loop

**Error handling:**

- Playwright launch fails: log, exit with status 1
- Screenshot fails: capture what's visible, note in review
- Mock route write fails: log warning; Playwright fallback mocks may still serve screenshot data
- OpenRouter 429: exponential backoff with jitter, 3 retries over 30s
- OpenRouter 5xx: retry once after 5s
- All retries exhausted: post "Review paused: LLM unavailable" notification
- GitHub API rate limit: wait for reset, then retry
- Comment post fails: log, retry once

**Retry configuration (from `config.py`):**

```python
LLM_RETRY_MAX_ATTEMPTS = 3
LLM_RETRY_BASE_DELAY = 2
LLM_RETRY_MAX_DELAY = 30
LLM_RETRY_JITTER = 0.1
```

## 4. Conversational Command Loop

**Trigger:** Initial review posted. Agent starts the command server and waits for Lambda-dispatched commands.

**Steps:**

1. Command server listens on `0.0.0.0:3001`.
2. Lambda POSTs commands to `/__renderpr/command` with `X-RenderPR-Token`.
3. Server validates the token against `RENDERPR_COMMAND_TOKEN`.
4. Server accepts command, returns immediately, and executes work in a background thread.
5. Supported commands:
   - `change` — LLM selects files, generates edit, applies it to the cloned repo, validates dev server health, re-screenshots, uploads screenshots, and posts a preview comment.
   - `apply` — stages and commits only user-edited files, pushes to the PR branch, and clears pending user edits.
6. If the task was cold-started with `COMMAND`, it executes that command after boot and then waits for more commands.
7. If no command arrives before idle timeout, the task exits.

**State management:**

- `ChangeSession.edited_files`: user-edited files that may be committed on `apply`
- `ChangeSession.runtime_generated_files`: temporary mock/config files that must never be committed
- Command server idle timer: reset on accepted commands
- Dev server process: kept alive for live preview and command edits

**Error handling:**

- Lambda cannot reach command server: Lambda cold-starts a new task for the command
- Dev server crashed: attempt restart once, post notification, exit if failed
- Playwright crash: re-launch browser, retry command
- LLM error during follow-up: post error message, keep command server alive

## 5. Runtime-Generated File Safety

RenderPR may create or patch files inside the disposable clone to make previews realistic:

- temporary API routes such as `src/app/api/users/route.ts`
- backup files such as `src/app/api/users/route.ts.renderpr.bak`
- temporary Next config changes for `allowedDevOrigins`

These files are runtime infrastructure, not user-requested code changes.

Rules:

1. Runtime-generated files are tracked separately from user edits.
2. `@renderpr apply` uses `ChangeSession.stageable_edits()` and stages only user-edited files.
3. Runtime-generated files and `*.renderpr.bak` files are never staged.
4. If a user edit touches the same file as a runtime-generated file, the runtime-generated file is excluded from apply by default.
5. The workspace is disposable; generated files disappear when the Fargate task stops.

## 6. Shutdown

**Trigger:** Idle timeout reached or shutdown command received.

**Steps:**

1. Post closing summary comment:
   - "RenderPR review session ended. Re-invoke with @renderpr to start a new session."
2. Cleanup:
    - Close Playwright browser
    - Kill dev server process
    - Clear temporary workspace, including runtime-generated mock/config files
3. Exit with status 0

**Error handling:**

- Final comment post fails: log, exit anyway
- Cleanup fails: log, exit anyway
