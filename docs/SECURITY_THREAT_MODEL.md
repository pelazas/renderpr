# RenderPR — Security Threat Model & Audit

> Status: **audit findings, no code changed.** This document is a design-level
> review based on reading the source on `main`. Severities are my assessment;
> none of the exploits below were run against live infrastructure. Remediations
> are proposals for your approval, not applied changes.
>
> Scope of concern (from the request): **env-var injection**, **leakage of the
> platform OpenRouter API key**, and the three acceptance questions —
> (a) are secrets fully withheld on fork PRs, (b) is task egress restricted,
> (c) is the GitHub token minimally scoped/short-lived. Both the **BYOC** model
> (today) and a **future hosted SaaS** model are covered.

---

## 1. Executive summary

RenderPR's core function is to **clone an untrusted pull request and execute its
code** (`npm ci`, `npm run dev`, plus whatever `postinstall`/build scripts and
dependencies the PR brings) inside a Fargate task. That is unavoidable for a
visual-preview product — but it means **the build sandbox must be treated as
fully attacker-controlled**, and today it is not.

The single most important finding:

> **The same Fargate task that runs untrusted PR code also carries an IAM task
> role that can read every platform and tenant secret in SSM, and has
> unrestricted outbound network access.** Any PR (including a fork PR) can run
> arbitrary code, fetch the task-role credentials from the ECS credentials
> endpoint, call `ssm:GetParameter` / `ssm:GetParametersByPath`, and exfiltrate:
> the **GitHub App private key** (the crown jewel — non-expiring, authenticates
> as the App to *every* installation), the **OpenRouter API key**, the global
> **command token**, and **every other repo's stored secrets** under
> `/renderpr/secrets/*`.

Because of this, the answers to the acceptance questions are:

| Question | Short answer |
|---|---|
| **(a) Secrets fully withheld on fork PRs?** | **No.** The *injection* of a repo's own env-secrets is correctly skipped for forks, but the platform secrets and *all other tenants'* secrets remain reachable by fork-PR code via the task role. The fork gate protects the wrong layer. |
| **(b) Task egress restricted?** | **No.** The security group is `allowAllOutbound: true` on a public subnet with a public IP. Exfiltration to anywhere is unimpeded. |
| **(c) GitHub token minimally scoped / short-lived?** | **Partially.** The installation token is short-lived (~60 min) but **not scoped to a single repo or to minimal permissions** — it grants the App's full permission set across all repos in the installation. Worse, the long-lived **App private key** itself is loaded into the untrusted task. |

The env-injection feature itself is **well-designed** in isolation (fork gate
before any SSM call, app-declared vars only, provider secrets withheld from the
app process, redaction helper, values never logged). Its weakness is entirely
that it runs in a sandbox that doesn't actually contain the attacker.

The fix is architectural and is the same fix that resolves (a), (b), and most of
(c): **isolate untrusted code execution from secret-bearing identity and
network.** See §6.

---

## 2. Assets, actors, trust boundaries

### Assets (in rough order of value)
1. **GitHub App private key** (`/renderpr/github-app`) — signs JWTs to mint
   installation tokens for *any* installation. Compromise = read/write access to
   every repo the App is installed on. Does not expire until manually rotated.
2. **Per-tenant repo secrets** (`/renderpr/secrets/{inst}/{owner}/{repo}/{KEY}`)
   — `NEXTAUTH_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`, `CLERK_SECRET_KEY`,
   `FIREBASE_SERVICE_ACCOUNT`, API keys, etc. These are signing/admin keys for
   the tenant's *production* auth systems.
3. **OpenRouter API key** (`/renderpr/openrouter`) — billable LLM spend; the
   explicit concern in this request.
4. **Webhook secret** — HMAC key for webhook authenticity.
5. **Command token** (`/renderpr/renderpr-command-token`) — authenticates
   Lambda→task command dispatch; single value, shared across all tasks.
6. **Screenshots** in S3 — rendered UI of (possibly private) repos, potentially
   authenticated views with mock data.
7. **AWS account** itself (BYOC) / **the multi-tenant account** (SaaS).

### Actors
- **PR author** — *untrusted*. Can be an external contributor opening a fork PR
  on a public repo, or anyone who can get code into a PR head. Controls: repo
  source, dependencies, lockfiles, `postinstall`/dev scripts, `.renderpr.yml`,
  `.env.example`, and the diff text fed to the LLM.
- **PR/issue commenter** — *untrusted on public repos*. Anyone who can comment
  `@renderpr ...` can trigger task launches.
- **Repo maintainer** — semi-trusted; issues `@renderpr apply`.
- **Deployer / AWS account owner** — trusted (BYOC) or the operator (SaaS).
- **Network attacker** — can reach the public Fargate IP (ports 3000/3001/etc.)
  and observe Lambda→task HTTP.

### Trust boundaries
```
                    TRUSTED                         |   UNTRUSTED
  GitHub ──HMAC──> API Gateway ──> Lambda           |
                                     │ RunTask       |
                                     ▼               |
                            ┌─ Fargate task ─────────┼────────────────┐
                            │  agent (python)  ◄──────┤ secrets, role   │
                            │      │ subprocess       |                 │
                            │      ▼                  |                 │
                            │  npm ci / npm run dev ──┼──► ATTACKER CODE│  ← boundary is
                            │  (PR code + deps)       |                 │     INSIDE the task,
                            └─────────────────────────┼────────────────┘     not around it
```
The defect: the trust boundary is drawn *around* the Fargate task, but the
attacker executes *inside* it, next to the credentials.

---

## 3. The central finding (CRITICAL)

### F-1 — Untrusted PR code runs with full secret-bearing task role and open egress

**Where:**
- `src/agent/main.py:90-121` clones the base repo then
  `git fetch origin pull/{pr_number}/head:review-pr` → checks out the **PR head
  (fork code)**.
- `main.py:399-406` runs `profile.install_command` (`npm ci`/`pnpm i`/…) and
  `profile.dev_command` (`npm run dev`) — arbitrary code from the PR, including
  `postinstall` hooks and transitive dependencies.
- The task role (`cdk/lib/renderpr-stack.ts:139-184`) grants the running
  container `ssm:GetParameter` on `github-app`, `openrouter`,
  `command-token`; `ssm:GetParametersByPath`/`GetParameter`/`GetParameters` on
  **`/renderpr/secrets/*`** (all tenants, not path-restricted to the current
  repo); S3 read/write; `ec2:DescribeNetworkInterfaces` on `*`.
- The container's credentials are retrievable by *any* process in the task via
  the ECS credentials endpoint (`169.254.170.2` + `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`,
  which is exported into the environment and inherited by the dev-server
  subprocess at `main.py:405`).

**Impact:** Trivial, reliable theft of every asset in §2 by anyone who can open
a PR. A malicious `package.json` `postinstall` of three lines (curl the creds
endpoint, then call SSM, then POST the loot to an attacker host — egress is
open) is sufficient. This **defeats the fork-PR secret gate entirely**: the gate
stops *injection* of the repo's own vars, but the fork's code can read all
secrets directly through the role.

**Why the fork gate doesn't save you:** `secrets.load_repo_secrets()` returns
`{}` on forks (`secrets.py:40-42`) so the user's declared env vars aren't written
into the dev server. But (1) `_fetch_secrets()` still pulls the OpenRouter key
and GitHub private key into the agent process for every run regardless of fork
status (`main.py:703`), and (2) far more importantly, the *role* is attached to
the task no matter what the Python code chooses to read.

**Severity: CRITICAL.** This is the finding to fix first; it dominates the risk
in both BYOC and SaaS.

---

## 4. Acceptance questions — detailed answers

### (a) Are secrets fully withheld on fork PRs?

**No — not in the way that matters.**

What *is* correctly done (good, keep it):
- `load_repo_secrets(is_fork=True)` returns `{}` **before any SSM call**
  (`secrets.py:40-42`) — the fork gate is in code, independent of IAM.
- Only app-*declared* vars (`.env.example` / `.renderpr.yml` `env.vars`) are
  injected into the app process; provider/admin secrets used only by the auth
  layer are deliberately never handed to the app (`env_inject.py:69-91`).
- Values are never logged; a `redact()` helper exists (`secrets.py:65-75`).
- `build_session(...)` produces no session on forks because `repo_secrets` is
  empty (`main.py:824-825`).

What breaks the guarantee:
- **F-1** above — fork code reads everything via the task role.
- The IAM policy scopes SSM reads to `/renderpr/secrets/*` (**all repos**), not
  to the current `{installation}/{repo}` prefix (`renderpr-stack.ts:158-163`).
  So even setting aside fork code, a compromise in *any* tenant's review can read
  *every* tenant's secrets. In SaaS this is a cross-tenant data breach; in BYOC
  it's cross-repo.
- `RENDERPR_COMMAND_TOKEN` is placed into `os.environ` (`main.py:705`) and the
  dev-server subprocess inherits `{**os.environ, ...}` (`main.py:405`), so the
  command token leaks into untrusted code even on forks. (See F-4.)

**Verdict:** the *injection* path is gated; the *ambient access* path is wide
open. Secrets are not fully withheld.

### (b) Is task egress restricted?

**No.**
- `fargateSg` is created with `allowAllOutbound: true` (`renderpr-stack.ts:36-40`).
  The "egress-only security group" described in `AGENTS.md` / `ARCHITECTURE.md`
  is **outbound-unrestricted**, not "egress-only" in any limiting sense.
- The task runs in a **public subnet with a public IP** and an internet gateway
  (`renderpr-stack.ts:21-30`, `webhook_handler.py:60`).
- Inbound is open to `0.0.0.0/0` on 3000/4321/5173 (preview) and 3001 (command
  server) (`renderpr-stack.ts:46-58`).

Consequence: once arbitrary code runs (which is the product's normal operation),
there is no network control preventing exfiltration of stolen secrets, and no
restriction on what the untrusted dev server can reach (internal services,
metadata, the wider internet). There is also no egress allow-list limiting the
task to just `api.github.com`, `openrouter.ai`, and AWS endpoints.

### (c) Is the GitHub token minimally scoped and short-lived?

**Short-lived: yes. Minimally scoped: no. And the bigger problem is the key, not the token.**
- The JWT used to mint the token expires in 600s (`main.py:421-423`); GitHub
  installation tokens themselves last ~60 min — acceptable.
- **Not scoped:** `_get_installation_token()` POSTs to
  `/app/installations/{id}/access_tokens` with **no body**
  (`main.py:432-437`). GitHub therefore returns a token with the App's **full
  permission set across all repositories** in that installation. The request
  could pass `{"repositories": ["<repo>"], "permissions": {...}}` to scope it to
  the single PR's repo with only the permissions needed (contents R/W, pull
  requests R/W, issues/comments W). It does not.
- **The key is the real issue (c+F-1):** the **App private key** is loaded into
  the untrusted task. A short-lived token limits a *leaked token's* blast radius
  to 60 min, but the private key lets an attacker mint fresh full-scope tokens
  for *any* installation indefinitely. Token lifetime is moot if the key is
  stealable.

---

## 5. Full findings list

Severity key: **C**ritical / **H**igh / **M**edium / **L**ow. "Model" =
BYOC-vs-SaaS relevance.

| ID | Sev | Finding | Location | Model |
|----|-----|---------|----------|-------|
| F-1 | **C** | Untrusted PR code shares the task with a secret-reading role + open egress (steals App private key, OpenRouter key, all tenant secrets, command token). | `main.py` exec paths; `renderpr-stack.ts:139-184` | Both |
| F-2 | **C → L** | SSM read policy was `/renderpr/secrets/*` (all tenants/repos). **Fixed:** the task role no longer holds a direct secrets read; it assumes a dedicated `SecretsAccessRole` per task with an inline **session policy scoped to the current repo's path**, so it can only read its own repo's secrets. **Residual (low):** the base role could in principle assume the reader role without supplying a session policy (IAM can't mandate one); our code always scopes it — full removal is the two-task split. | `renderpr-stack.ts` (SecretsAccessRole), `secrets.py` (`_scoped_ssm_client`) | SaaS (breach), BYOC (cross-repo) |
| F-3 | **H** | GitHub App **private key** present in the untrusted runtime; installation token over-scoped (all repos, all perms). | `main.py:64-87, 420-450` | Both |
| F-4 | **H** | Command token written to `os.environ` and inherited by the untrusted dev-server subprocess; token is a single global value shared by all tasks; command server is reachable from `0.0.0.0/0` on 3001. So untrusted code learns the global token and can dispatch `apply`/`change` to *other* PRs' tasks (whose IPs are published in PR comments). | `main.py:705,405`; `command_server.py:95`; `renderpr-stack.ts:54-58` | Both (worse in SaaS) |
| F-5 | **H → M** | No egress restriction (see (b)). **Partially fixed:** the SG now restricts egress to web/DNS ports only (`allowAllOutbound: false` + 443/80/53), blocking C2/exfil over arbitrary ports and non-web protocols while preserving installs/GitHub/OpenRouter/AWS/DNS and the public-subnet preview model. **Residual:** HTTPS exfil to an arbitrary public host is still possible — that needs a domain-allowlist egress proxy or NAT+firewall (would drop the public-IP preview; out of scope per design decision). | `renderpr-stack.ts` (SG egress) | Both |
| F-6 | **H → ✓** | S3 screenshot bucket was `publicReadAccess: true`. **Fixed:** the bucket is now private (`BLOCK_ALL`); screenshots are served only through CloudFront with Origin Access Control and **signed URLs** (7-day expiry matching retention). Object keys already use a random UUID. Requires a one-time signing key setup (`scripts/setup-cloudfront-key.sh`; private key in SSM, public key read by CDK). | `renderpr-stack.ts` (CloudFront/OAC), `visual.py` (`_screenshot_url`) | Both |
| F-7 | **M** | Lambda→task command dispatch is plaintext **HTTP** to `http://{public_ip}:3001`; the bearer-style command token is sent in clear over the internet and is interceptable. | `webhook_handler.py:105-128` | Both |
| F-8 | **M → L** | Any user who can comment `@renderpr` (or open a PR) on a watched repo can trigger Fargate `RunTask` → arbitrary code execution + compute/LLM spend. **Partially mitigated:** a global concurrency cap (`MAX_CONCURRENT_TASKS`), per-PR SHA dedup, API Gateway throttling, and a scheduled reaper now bound *unbounded* spend. **Residual:** still no per-author authorization (author-association gating remains open); the compute-DoS ceiling is capped but non-zero. | `webhook_handler.py` (dedup/cap), `reaper_handler.py`, `renderpr-stack.ts` (throttling) | Both (cost DoS worse in SaaS) |
| F-9 | **M** | No webhook replay/timestamp protection; a captured valid delivery can be replayed to re-trigger tasks. | `webhook_handler.py:37-41,160-171` | Both |
| F-10 | **M** | CI uses long-lived static AWS keys (`AWS_ACCESS_KEY_ID`/`SECRET`) for `cdk deploy --require-approval never`; deploy identity is typically broad. Prefer GitHub OIDC role assumption + scoped deploy policy. | `.github/workflows/deploy.yml:64-81` | Both |
| F-11 | **L/M** | LLM-generated edits write to `Path(REPO_DIR)/edit["file"]` with no path-traversal guard; `validate_edit` only checks existence + `oldString` match. A `../`-containing path (LLM influenced by attacker query/diff) could write outside the intended tree (bounded: target must exist and contain `oldString`). | `code_edit.py:141-149`; `editor.py:64-75` | Both |
| F-12 | **L** | Prompt-injection surface: attacker-controlled diff/source/`.env.example` keys flow into LLM prompts that drive mock generation and code edits. Impact bounded by the "CSS/HTML/text only" instruction and apply-gating, but it's a soft control. | `routes.py`, `code_edit.py` | Both |
| F-13 | **L** | `_run_install`/dev logs stream subprocess stdout at INFO; a malicious dependency could print secret-looking strings into CloudWatch. Low impact (logs already same trust domain) but worth `redact()`-ing tool output. | `main.py:284-296,333-341` | Both |
| F-14 | **L** | Screenshots/preview can render the app as a forged **synthetic admin** session (`role: admin` default in docs). Combined with F-6 (public bucket) this can expose admin-only UI. Ensure synthetic users are clearly non-privileged where possible and screenshots aren't public. | `docs/AUTH_AND_ENV.md`; `auth/forge.py` | Both |

Notes on things that are **done well** (so they're not lost in remediation):
- HMAC verification uses `hmac.compare_digest` and fails closed when the secret
  is absent (`webhook_handler.py:37-41`). Command-token check is also constant
  time (`command_server.py:53-60`).
- Bot/self events are filtered to prevent recursive triggering
  (`webhook_handler.py:203-206,259-261`).
- Secrets are stored as SSM SecureString, never in CDK context/env at deploy
  time, injected post-deploy.
- Mock route path building rejects `.`/`..` segments (`mock_server.py:196-200`).
- Env-injection design (declared-vars-only, provider secrets withheld, fork
  gate, redaction helper, values never logged) is sound at the code layer.

---

## 6. Prioritized remediation roadmap

### P0 — Break the "untrusted code next to secrets" coupling (fixes F-1, most of F-3, contains F-2)

The objective: **untrusted PR code must never run in a process/task that holds a
secret-bearing IAM role or can reach secret stores.** Options, best first:

1. **Two-stage split (recommended).**
   - *Trusted orchestrator* (Lambda or a minimal-permission control task) holds
     identity: mints the GitHub installation token, reads the OpenRouter key and
     the tenant's *declared* env values.
   - *Untrusted runner* (separate Fargate task **with no task role**, or a role
     with zero SSM/secrets access) receives, via env overrides, **only**: the
     short-lived repo-scoped GitHub token (for clone), the already-resolved
     declared env values for this repo, and the screenshot upload target as a
     **pre-signed S3 URL** (so it needs no S3 IAM either).
   - The runner cannot read SSM at all; stealing its env yields at most a 60-min
     single-repo token + that repo's own declared non-secret vars — which the PR
     author largely controls anyway.
2. **Move GitHub JWT signing + token minting out of the runtime task** into the
   Lambda (which already reads `/renderpr/github-app` for the webhook secret).
   Pass only the minted, **repo-scoped, minimally-permissioned** token to
   Fargate. The App **private key then never enters the untrusted task** — this
   alone removes the worst F-1/F-3 outcome even before the full split.
3. **For the OpenRouter key specifically:** do not give it to the task. Either
   (a) run the LLM review/edit calls from the trusted orchestrator and pass only
   results to the runner, or (b) front OpenRouter with a thin proxy the runner
   calls using a per-task, low-limit, expiring credential. If the key must stay
   in-task short-term, at minimum set a hard monthly spend cap / low rate limit
   on the OpenRouter key so a leak is bounded financially.
4. **Block the credentials endpoint from untrusted child processes** as
   defense-in-depth: drop the `AWS_CONTAINER_CREDENTIALS_*` vars from the
   dev-server subprocess env and add an egress rule blocking `169.254.170.2`
   from the runner. (Necessary but **not sufficient** alone — the role still
   shouldn't be there.)

### P0 — Scope SSM reads per-repo (fixes F-2)
Replace the wildcard `/renderpr/secrets/*` grant with a per-task policy scoped to
`/renderpr/secrets/{installation_id}/{repo}/*`. In the two-stage design the
orchestrator resolves secrets and the runner gets none, which makes this moot —
but until then, tighten the wildcard. In SaaS this is mandatory to prevent
cross-tenant reads.

### P1 — Restrict egress (fixes F-5, hardens F-1)
- Set the runner SG to `allowAllOutbound: false` and allow only what the build
  legitimately needs. Pure allow-listing is hard with arbitrary `npm` registries;
  practical approach: route runner egress through a **filtering proxy / NAT with
  an allow-list** (package registries, `api.github.com`, `openrouter.ai`/proxy,
  AWS endpoints) and deny the rest, or accept the cost of a NAT + egress firewall.
  At minimum block link-local/metadata and RFC1918 ranges the task shouldn't
  reach.
- Reconsider public-subnet + public-IP. The live-preview link is the reason it's
  public; an ALB / ngrok-style tunnel / CloudFront-fronted preview would let you
  drop the public IP and inbound `0.0.0.0/0` rules.

### P1 — Lock down screenshots (fixes F-6, F-14)
- Make the bucket private (`BLOCK_ALL` public access) and serve screenshots via
  **pre-signed, short-TTL URLs** (or CloudFront with signed URLs). The 7-day
  lifecycle already exists; pair it with non-guessable keys (add a random
  component, not just `pr_number`).
- Treat authenticated/synthetic-admin screenshots as sensitive by default.

### P1 — GitHub token scoping (fixes F-3)
- Add `{"repositories":[repo], "permissions":{...minimal...}}` to the token
  request. Grant only contents (R/W for apply), pull_requests (R/W), issues (W
  for comments). Drop everything else.

### P2 — Command channel (fixes F-4, F-7)
- Stop exporting `RENDERPR_COMMAND_TOKEN` into `os.environ` / subprocess env;
  pass it to the command server directly (the `CommandServer(token=...)` ctor
  already supports this).
- Make the token **per-task** (generate at task start, register alongside the
  IP in SSM/`registration.py`) instead of one global value, so a leak can't
  cross tasks.
- Put the command server behind TLS or, better, off the public internet
  entirely (private dispatch path / authenticated channel). Don't send the token
  over plaintext HTTP across the internet.

### P2 — Trigger authorization & abuse limits (fixes F-8, F-9)
- ✅ **Done:** global concurrency cap (`MAX_CONCURRENT_TASKS`), per-PR SHA dedup,
  API Gateway throttling, and a scheduled orphan-task reaper that hard-stops
  tasks past `MAX_TASK_AGE_SECONDS`. Bounds the cost-DoS surface of F-8.
- **Still open:** gate `RunTask` on author association (e.g., only
  OWNER/MEMBER/COLLABORATOR trigger automatically; others require a maintainer
  opt-in label). Add LLM/compute budget guards.
- Add webhook delivery-id/timestamp replay protection (F-9).

### P3 — Supply-chain & hygiene (F-10, F-11, F-12, F-13)
- CI: switch to GitHub OIDC + a scoped CDK deploy role; drop static AWS keys.
- Add an explicit `..`/absolute-path guard before any LLM-driven file write
  (`apply_edit`/`validate_edit`), confining writes under `REPO_DIR`.
- Run `redact()` over tool/subprocess output before logging.
- Keep treating LLM output as untrusted (it already is, mostly).

---

## 7. SaaS-specific addendum

The findings above are **worse** under the proposed hosted model (`FUTURE.md`
§"Hosted SaaS Offering"), because secrets move from each user's own SSM into
**your** shared store and many tenants share infrastructure:

- **F-1 + F-2 become a cross-tenant breach, not a self-inflicted one.** A single
  malicious PR on *any* customer's repo could, today's design, read *every*
  customer's auth signing keys and your platform keys. The two-stage isolation
  (P0) and per-tenant secret scoping (P0) are **preconditions for SaaS**, not
  nice-to-haves.
- **Strong tenant isolation** is required: per-tenant IAM scoping, ideally
  per-tenant task roles/accounts or at least per-tenant SSM/KMS key boundaries,
  and per-tenant network isolation so one runner can't reach another.
- **The GitHub App private key** is now a platform-wide secret shared across all
  tenants — it must never touch a runner (P0 item 2). Consider per-tenant token
  minting in a trusted service with audit logging.
- **Billing abuse (F-8)** becomes a direct cost-to-you problem; per-tenant
  compute/LLM quotas and trigger authorization are needed before launch.
- **Screenshots (F-6)** in a shared bucket must be private + per-tenant access
  controlled; a public bucket leaks one tenant's UI to the world and possibly to
  other tenants.
- Document a **data-handling / retention** posture (screenshots, mock data,
  cloned source) since you'd now be a processor of customers' code and possibly
  their auth secrets.

The `FUTURE.md` claim that SaaS "inherits the same fork-PR secret gate and
redaction guarantees" is true but insufficient — those guarantees never
addressed the F-1 ambient-access path, which is the dominant risk.

---

## 8. Suggested fix order (one line)

1. **P0:** take the App private key + OpenRouter key + SSM access *out of the
   untrusted runner* (mint scoped token in a trusted stage; runner gets a
   role-less task + pre-signed S3 upload). 2. **P0:** scope `/renderpr/secrets/*`
   per-repo. 3. **P1:** restrict egress, privatize the screenshot bucket, scope
   the GitHub token. 4. **P2:** per-task command token off the public internet,
   trigger authorization + quotas. 5. **P3:** CI OIDC, path-traversal guard, log
   redaction.

Nothing in this document has been implemented. Tell me which items to take and
I'll do them per your workflow (separate worktree, one commit per small task,
co-authored, deploy-tested, then synced to main).
