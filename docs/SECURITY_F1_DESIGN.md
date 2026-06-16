# F-1 Remediation — Design (untrusted code vs. secret-bearing identity)

> Follows up on `docs/SECURITY_THREAT_MODEL.md` finding **F-1** and the pass-1
> hardening (PR #38). Implemented in this PR via **Option C** below.

## 1. Problem recap

The runner Fargate task **executes untrusted PR code** (`npm ci` postinstall
hooks, `npm run dev`) in the same task that holds an IAM role able to read every
secret in SSM (GitHub App private key, OpenRouter key, all tenants' secrets) and
that has unrestricted egress. Pass-1 stripped credential env vars from the child
processes, but that is bypassable: the dev server runs as **the same user
(root)** as the agent, so it can read `/proc/<agent-pid>/environ` and recover
`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, then fetch task-role credentials from
`169.254.170.2`. So F-1 was mitigated, not closed.

**Goal:** the untrusted `npm`/dev-server processes must be unable to reach
(a) the task-role credentials, (b) the OpenRouter key, (c) auth signing secrets,
(d) other tenants' secrets — even via `/proc` or the metadata endpoint.

## 2. What is actually untrusted

Only two processes run PR-controlled code:

- **dependency install** (`npm/pnpm/yarn/bun` + `postinstall` hooks)
- **the dev server** (`npm run dev` and everything it spawns)

Everything else is *our* code: git clone (runs `git`, not PR code), token
minting, OpenRouter calls (review/route/edit inference), auth forging, screenshot
capture (Playwright drives a sandboxed Chromium against the dev server), and S3
upload. The secrets (b)(c) live only in the agent's Python memory and HTTP
headers — never in env or on disk — so a *separate-uid* child cannot read them.
The one secret reachable by the child today is (a), via `/proc` + the metadata
endpoint, which then unlocks (d).

## 3. Options considered

### Option A — Two tasks (trusted orchestrator + role-less runner)
A trusted task/Lambda holds identity and S3; a separate role-less Fargate task
clones + installs + runs the dev server, receiving a scoped token, resolved env,
and pre-signed S3 URLs.

- ✅ Strongest isolation; the private key/OpenRouter key are physically absent
  from the untrusted task.
- ❌ Large rearchitecture: cross-task orchestration, networking, lifecycle, ~2×
  cost/cold-start. The OpenRouter key and auth signing secrets are still needed
  where the LLM/auth code runs, so the split only helps if we also ship
  screenshots/diffs between tasks. Significant CDK/IAM change.
- ❌ Introduces a **token-refresh wrinkle**: a Lambda-minted token expires in
  ~60 min, so long sessions need a refresh callback path.

### Option B — Sidecar containers in one task
Rejected: the ECS task IAM role is **per-task, not per-container**, so multiple
containers do not separate identity.

### Option C — Privilege separation within one task (CHOSEN)
Keep the single task and the in-task agent (root, holds the role + secrets), but
run the untrusted install/dev-server processes as a **dedicated unprivileged
user** with the already-stripped environment.

- ✅ Closes (a) — the runner user cannot read root's `/proc/environ`, so it never
  learns the metadata path; with no credentials it cannot read SSM, closing
  (d) too. (b)(c) were never reachable (in-memory only).
- ✅ **No token-refresh wrinkle** — minting stays in the in-task agent, which
  holds the private key in memory and can re-mint anytime.
- ✅ **No CDK/IAM change required** — Dockerfile + `main.py` only. Low deploy
  risk, easy to validate and roll back.
- ✅ Fargate-friendly: uses `subprocess` `user=`/`group=` (Python 3.12) — no
  `NET_ADMIN`/iptables (which Fargate disallows) needed.
- ⚠️ The App private key + OpenRouter key still physically reside in the task
  (walled off from untrusted code). Residual risk is an RCE in *our own*
  controlled dependencies — low. Option A remains a future step on top of C if
  SaaS assurance later demands the key be physically absent.

## 4. Implementation (Option C)

### Container (`Dockerfile`)
- Add an unprivileged user: gid/uid **10001** named `runner`, home
  `/home/runner`. The container still starts as root (the agent needs root to
  hold creds and to drop privileges for children). Node/pnpm/yarn/bun and the
  Playwright browsers stay where they are (browsers are used by the root agent).

### Agent (`src/agent/main.py`)
- `_can_drop_privileges()` — true only when the agent is `root` (`geteuid()==0`).
  Outside the container (tests/local non-root) it is false and we run children
  as the current user, logging a warning, so nothing crashes.
- Before launching untrusted processes, `chown -R 10001:10001` the repo tree so
  the runner can write `node_modules`, `.next`, `.vite`, etc. Root keeps write
  access regardless, so agent-side edits, mock writes and screenshot output still
  work. Because the root agent then runs `git` (revert/apply) on a now
  runner-owned tree, also add `git config --global --add safe.directory <repo>`
  to avoid git's dubious-ownership refusal.
- Launch **both** install (`_run_install`) and the dev server
  (`_start_dev_process`) with `subprocess.Popen(..., user=10001, group=10001,
  env=_untrusted_subprocess_env(..., {"HOME": "/home/runner"}))`. `HOME` must
  point at a runner-writable dir or npm/vite caches fail.
- npm-cache restore (root) extracts into the tree before the `chown`, so restored
  `node_modules` ends up runner-owned; cache **store** stays in the root agent
  thread (needs S3 creds, only reads the tree).
- Everything secret-bearing (token mint, OpenRouter calls, auth forging, S3
  upload, task registration) stays in the root agent, unchanged.

### CDK / IAM
- **No change.** The role stays on the task (the root agent uses it). Tightening
  the role toward least privilege (F-2) is a separate follow-up.

## 5. Testing & deploy validation
- Unit: assert install/dev `Popen` get `user`/`group` and a `HOME` overlay when
  privileges can be dropped, and do not when they can't; assert the sanitized env
  still strips creds; existing dev-server tests keep passing.
- Integration (on a real review): confirm the dev process runs as `runner`
  (`id`), and that from it `curl 169.254.170.2$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`,
  reading `/proc/1/environ`, and `aws sts get-caller-identity` all fail to yield
  credentials, while screenshots/preview still work.
- Deploy: new image ⇒ new task definition; the running task must **STOP** before
  the new behavior takes effect. Rollback = revert + redeploy (no infra state).

## 6. Sequenced follow-ups (not in this PR)
- **F-2** — scope the task role's `ssm:GetParametersByPath` off `/renderpr/secrets/*`.
- **F-5** — egress restriction (needs NAT+firewall or filtering proxy).
- **Option A** — physically remove the key from the task if SaaS assurance demands it.
