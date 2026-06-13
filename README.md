# RenderPR

A GitHub App that **visually reviews frontend pull requests**. On each PR it spins up an ephemeral environment, runs the app, screenshots the changed pages across viewports, and posts an LLM review of the screenshots alongside the diff. It runs entirely in **your own AWS account** (BYOC) and stores all secrets in your own SSM.

It triggers automatically when a PR is opened or updated, and you can drive it conversationally with `@renderpr` comments for a ~15-minute window after each review.

---

## Features

Everything below is implemented and in the codebase.

### Review
- **Visual PR review** — clones the PR branch, runs the dev server, and screenshots the affected pages at **4 viewports** (Mobile XS, Tablet, Desktop, Desktop XL), then sends the screenshots + git diff to an LLM and posts a structured markdown review.
- **Per-framework route inference** — parses the diff to figure out which routes changed and screenshots them. Strategies for **Next.js (app & pages router), SvelteKit, Astro, Remix**; SPAs (Vite/CRA) fall back to the homepage + LLM-found routes. Dynamic routes (with params) are skipped.
- **Frontend / monorepo discovery** — finds the frontend `package.json` (incl. monorepos), picks the most frontend-like candidate, and skips backend-only PRs with a comment instead of failing.
- **AI-generated mock data** — for API fetches found in the changed code, the LLM generates realistic mock responses so pages render with data instead of empty/error states. Served via Playwright `page.route()` interception (all frameworks) and temporary server route files (Next.js). No database or live backend is started.
- **Interaction screenshots** — when a change is behind a click (modal, dropdown, drawer), it clicks the trigger and captures the revealed UI as an "after interaction" screenshot in addition to the baseline.
- **Live progress comment** — a single comment shows a live checklist (setup → install/dev server → screenshots → review) and is edited in place into the final review.
- **Live preview link** — includes a `http://<public-ip>:<port>` URL to the running app so reviewers can click around.

### Conversational commands
After the initial review the container stays alive (idle timeout **15 min**, polling the thread every ~10s):
- `@renderpr review` — run (or re-run) a full review. *(also the default for a bare `@renderpr` or `@renderpr help`)*
- `@renderpr code change: <instruction>` — the LLM edits the cloned repo, re-runs the dev server, and posts updated screenshots.
- `@renderpr apply` — commits the proposed change to the PR branch (runtime-generated mock/config files are excluded from the commit).

### Stacks
- **Package managers:** npm, pnpm, yarn, bun — detected by lockfile; the right install + dev command and default port are chosen per framework, and the port is sniffed from the dev server's startup banner (with candidate-port fallback) rather than assuming 3000.
- **Dependency cache** — `node_modules` is tar'd to S3 keyed by `{package-manager}-{lockfile-hash}`; a cache hit skips install (seconds instead of minutes). Works across npm/yarn/pnpm/bun.

### Env & auth-gated apps
- **Env/secret injection** — vars an app declares (via `.env.example` and/or `.renderpr.yml`) are injected from your per-repo SSM secrets as an ephemeral `.env.local` + the dev-server env before boot. Injection is scoped to declared vars only.
- **Auth-gated review** — mints a session for a **synthetic** user so it can review pages behind a login wall, without scripting a real login. Supports **NextAuth/Auth.js v4 & v5**, **generic JWT**, **Supabase** (forge or GoTrue admin API), **Clerk**, and **Firebase**. If an unconfigured app still lands on a login page, the review degrades with guidance instead of reviewing the login screen.
- **`.renderpr.yml`** — optional per-repo config, layered over auto-detection: declare `env` vars and the `auth` method/synthetic user. Secret *values* always live in SSM, never in the file.

### Security
- **BYOC / zero-trust** — deployed into your AWS; secrets stay in your SSM (SecureString).
- **GitHub auth** uses short-lived **installation access tokens** (no PATs).
- **Secrets are never injected on fork PRs**, never logged, and only ever mint sessions for a synthetic user.
- The bot **ignores its own events** (its commits/comments don't re-trigger reviews).

---

## How it works

```
GitHub PR event / @renderpr comment
        │
        ▼
API Gateway → Lambda (webhook_handler.py)
   • verifies HMAC signature
   • parses command / event
   • ECS RunTask (or dispatches to a running task's command server)
        │
        ▼
Fargate task (src/agent/main.py)
   clone → detect PM+framework → install (S3 cache) → inject env/auth
   → start dev server → infer routes from diff → generate mocks
   → Playwright screenshots @ 4 viewports → LLM review (OpenRouter)
   → post/edit PR comment → stay alive for @renderpr follow-ups (15 min idle)
        │
        ├─ screenshots + npm cache → S3
        └─ secrets ← SSM Parameter Store
```

| Layer | Tech |
|---|---|
| Webhook router | AWS API Gateway + Lambda (Python 3.12) |
| Sandbox | AWS Fargate / ECS (Python + Node image; npm/pnpm/yarn/bun) |
| Browser automation | Playwright (Python) |
| LLM | OpenRouter (default model `google/gemini-2.5-flash`, set in `config.py`) |
| Infrastructure | AWS CDK v2 (TypeScript) |
| Screenshots & cache | Amazon S3 |
| Secrets | AWS SSM Parameter Store (SecureString) |

---

## Installation

RenderPR deploys into your own AWS account.

### Prerequisites
- An **AWS account** with credentials configured locally (`aws configure`), and permission to deploy CloudFormation/IAM/ECS/Lambda/S3.
- **Node.js 20+** and the **AWS CDK CLI** (`npm install -g aws-cdk`).
- **Docker** running locally (CDK builds the Fargate container image).
- A **GitHub App** (created below).
- An **OpenRouter API key** (https://openrouter.ai).

### 1. Register a GitHub App
**Settings → Developer settings → GitHub Apps → New GitHub App**
- **Repository permissions:** *Contents* — Read & write (clone/diff, and commits for `@renderpr apply`); *Pull requests* — Read & write; *Issues* — Read & write (PR comments).
- **Subscribe to events:** *Pull request* and *Issue comment*.
- **Webhook secret:** set one (save it).
- **Private key:** generate and download the `.pem` (save it).
- Note the **App ID**, then **Install** the app on the repos you want reviewed.
- Leave the Webhook URL blank for now — you'll set it after deploy (step 4).

### 2. Deploy the infrastructure
```bash
git clone https://github.com/pelazas/renderpr.git
cd renderpr/cdk
npm install
npx cdk bootstrap          # one-time per AWS account/region
```
Set a shared command token in `cdk/cdk.json` context (Lambda ↔ task-server auth; required for `@renderpr` commands):
```json
{ "context": { "appName": "renderpr", "commandToken": "<random-string>" } }
```
Then deploy:
```bash
npx cdk deploy
```
Note the **`ApiGatewayUrl`** in the outputs.

### 3. Inject secrets into SSM
```bash
cd ..
bash scripts/setup-secrets.sh
```
Prompts for your **GitHub App ID**, **private key (.pem path)**, **webhook secret**, and **OpenRouter API key**, and writes them to `/renderpr/github-app` and `/renderpr/openrouter` (SecureString). The Fargate task role has read-only access.

### 4. Point the GitHub App at your deployment
Set the GitHub App's **Webhook URL** to the `ApiGatewayUrl` from step 2.

### 5. Verify
Open a PR (or comment `@renderpr review this`) on an installed repo. You should see the live progress comment, then a review with screenshots and a live preview link.

### (Optional) Per-repo env vars & auth
Store secrets for a repo (used by env injection / auth):
```bash
scripts/renderpr-secrets.sh <installation_id> <owner/repo> \
  NEXT_PUBLIC_API_URL=https://api.example.com \
  NEXTAUTH_SECRET="$(openssl rand -hex 32)"
```
Secrets are stored at `/renderpr/secrets/{installation_id}/{owner}/{repo}/{KEY}`. Then add a `.renderpr.yml` to the repo:
```yaml
env:
  from: .env.example          # which vars to inject from your stored secrets
auth:
  type: jwt                   # nextauth | jwt | supabase | clerk | firebase
  user:
    email: preview@example.com
    name: Preview User
    role: admin
```

---

## Development

```bash
pip install -r requirements.txt
pytest tests/                 # Python unit/integration tests
cd cdk && npm test            # CDK tests
ruff check src/ tests/ && mypy src/
```

Tunable constants (timeouts, viewports, default model, idle window, cache settings) live in `src/agent/config.py`. See `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/WORKFLOW.md`, and `docs/AUTH_AND_ENV.md` for details, and `docs/FUTURE.md` for the roadmap.
