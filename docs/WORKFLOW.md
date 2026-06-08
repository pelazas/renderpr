# RenderPR Agent Workflow

This document describes the exact step-by-step workflow an agent follows when processing a GitHub Pull Request event. Every step includes inputs, outputs, error handling, and retry logic.

## 1. Webhook Received (Lambda)

**Trigger:** GitHub App sends a `pull_request.opened` or `issue_comment.created` (with `@renderpr`) webhook to API Gateway.

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
7. Lambda calls ECS `RunTask` with environment overrides:
   - `INSTALLATION_ID`
   - `REPO_FULL_NAME`
   - `PR_NUMBER`
8. Lambda returns 200 OK to GitHub

**Error handling:**

- HMAC mismatch: log minimal info, return 401, no retry
- RunTask API failure: log error, return 500, GitHub retries webhook
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
5. Agent installs dependencies: `npm ci`
6. Agent starts the dev server: `npm run dev &`
7. Agent waits for dev server readiness:
   - Poll `http://localhost:3000` until 200 or 60s timeout
   - Interval: 2s

**Error handling:**

- SSM Parameter Store access denied: log, exit with status 1
- JWT generation failure: log, exit with status 1
- Token exchange fails: log, exit with status 1
- Git clone fails: retry once, then post error comment, exit with status 1
- `npm ci` fails: capture stderr, post diagnostic comment, exit gracefully
- Dev server timeout: capture stderr, post diagnostic comment, exit gracefully

## 3. Initial Review

**Trigger:** Dev server is ready.

**Steps:**

1. Get the PR code diff:
   - `GET /repos/{owner}/{repo}/pulls/{pr_number}` with `Accept: application/vnd.github.v3.diff`
2. Launch headless Chromium via Playwright
3. For each configured viewport width (`[375, 768, 1280, 1920]`):
   a. Set browser viewport size
   b. Navigate to `http://localhost:3000`
   c. Wait for network idle
   d. Capture full-page screenshot
   e. Collect console errors and network failures
4. Send review request to OpenRouter:
   - System prompt with review instructions
   - User message containing the diff text and screenshots as base64 images
5. Parse LLM response into structured markdown
6. Post review comment via `POST /repos/{owner}/{repo}/issues/{pr_number}/comments`
7. Store `last_interaction_time = now()`

**Error handling:**

- Playwright launch fails: log, exit with status 1
- Screenshot fails: capture what's visible, note in review
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

## 4. Polling Loop

**Trigger:** Initial review posted. Agent enters conversational mode.

**Steps:**

1. Loop:
   a. Wait `POLL_INTERVAL` seconds (default: 10)
   b. Fetch PR comments: `GET /repos/{owner}/{repo}/issues/{pr_number}/comments`
   c. Filter comments newer than `last_seen_comment_id`
   d. For each new comment containing `@renderpr`:
      i. Update `last_interaction_time = now()`
      ii. Parse command from the comment text (strip `@renderpr` prefix)
      iii. Execute command:
           - `review` or no command → full re-review
           - `render [viewports]` → specific viewport screenshots
           - `dark mode` → enable `prefers-color-scheme: dark`
           - `accessibility` or `a11y` → run accessibility scan
           - `help` → post available commands
           - unknown → post "unknown command" response
      iv. Re-run Playwright with the specified parameters
      v. Send new screenshots to LLM (include previous context)
      vi. Post follow-up comment
   e. Check idle timeout:
      ```python
      if now() - last_interaction_time > IDLE_TIMEOUT:
          break
      ```
2. Exit loop

**State management:**

- `last_interaction_time`: updated on every `@renderpr` command
- `last_seen_comment_id`: tracks which comments have been processed
- `conversation_history`: list of (command, review) pairs for LLM context
- `browser_context`: reused across commands for performance, refreshed if corrupted

**Error handling:**

- Network error during poll: retry on next iteration (10s acceptable delay)
- Dev server crashed: attempt restart once, post notification, exit if failed
- Playwright crash: re-launch browser, retry command
- LLM error during follow-up: post error message, continue polling

## 5. Shutdown

**Trigger:** Idle timeout reached or shutdown command received.

**Steps:**

1. Post closing summary comment:
   - "RenderPR review session ended. Re-invoke with @renderpr to start a new session."
2. Cleanup:
   - Close Playwright browser
   - Kill dev server process
   - Clear temporary workspace
3. Exit with status 0

**Error handling:**

- Final comment post fails: log, exit anyway
- Cleanup fails: log, exit anyway
