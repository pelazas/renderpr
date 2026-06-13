# Future Features

Ideas scoped out for RenderPR. Implemented items are kept here briefly as product history; pending items remain actionable.

## Implemented

### 1. Intelligent Route Navigation

**Problem:** Early RenderPR screenshots only captured the homepage (`localhost:3000`). Real PRs change specific pages.

**Approach:**
- Parse git diff to find changed file paths
- Strip framework conventions to guess routes (e.g., `app/profile/page.tsx` → `/profile`, `pages/about.tsx` → `/about`)
- Framework-specific inference: Next.js app dir, Next.js pages router, React Router patterns
- Navigate to each unique route and screenshot at all viewports
- If route can't be inferred (e.g., shared component), screenshot from parent route or homepage

### 2. Monorepo / Frontend Discovery

**Problem:** Not all PRs have `package.json` at the repo root. Some use monorepos (`packages/web/package.json`), and some PRs are backend-only with no frontend changes at all.

**Approach:**
- Scan repo for `package.json` files (up to a depth limit)
- Filter by heuristics: presence of `next`, `react`, `vite`, `@angular/core` in dependencies
- If multiple candidates found, pick the one with the most frontend-indicative deps
- If none found, post a comment saying the PR doesn't appear to contain frontend changes and exit gracefully

### 3. AI-Generated Mock Data

**Problem:** Without mock data, screenshots show loading spinners or empty states.

**Approach:**
- Parse the diff to identify which routes/pages changed and what API data they consume
- For each affected component, trace which fields are rendered
- Search the codebase for those specific fields to determine their real shapes and types
- Generate mock data only for what's actually needed on screen
- Write temporary server-side API routes so screenshots and live preview use the same mock data
- Register Playwright route handlers as fallback
- No committed fixture files: everything is ephemeral, PR-specific, and adaptive to schema changes

### 4. Conversational Code Changes via @renderpr

**Problem:** Users can request changes in comments but the bot only reviews, never edits code.

**Approach:**
- Parse `@renderpr` commands for code change requests (e.g., "change the button color to orange")
- Apply the requested change to the cloned repo in the active Fargate container
- Re-run the dev server, capture updated screenshots
- Post a follow-up comment with the new screenshots
- Uses the LLM to select files and generate edits
- Supports `@renderpr apply`
- Excludes runtime-generated mock/config files from apply commits

### 5. Live Preview Links

**Problem:** Screenshots are useful, but reviewers often need to click around the running app.

**Approach:**
- Run the dev server with `HOST=0.0.0.0`
- Resolve the Fargate task public IP from ECS metadata
- Include `Live app: http://<public-ip>:3000` in review comments
- Patch temporary Next config with `allowedDevOrigins` for the public IP
- Keep internal RenderPR browsing on `localhost` for reliable screenshots

### 6. Framework & Package-Manager Breadth

**Problem:** Discovery was hardcoded to npm + Next.js + port 3000 and hard-failed on any other stack (install, boot/readiness, and route/mock inference all assumed it).

**Approach (shipped in two phases):**
- **Package manager** (`stack.py`): detect by lockfile (`pnpm-lock.yaml`/`yarn.lock`/`bun.lockb`/`package-lock.json`) → pick the install command and key the S3 cache as `{pm}-{lockfile-hash}`. pnpm is forced to `node-linker=hoisted` so the tar-to-S3 cache captures real files, not symlinks into the store.
- **Boot** (`stack.py` + `main.py`): derive the dev command from the framework, pass the right host flag (Vite/Astro/SvelteKit `--host`, CRA `DANGEROUSLY_DISABLE_HOST_CHECK`, Next `HOST=0.0.0.0`), and sniff the printed `http://…:PORT` line from stdout (falling back to candidate ports) instead of polling a fixed 3000.
- **Route/mock model** (`routing.py` + `mock_server.py`): routing is a per-framework strategy (Next app/pages router, Astro, SvelteKit, Remix; SPAs degrade to home + LLM-found routes), and the LLM prompt is framework-parameterized. Server-side mock route files are written only for Next; everything else relies on the browser-layer `page.route()` interception. The dev-origin allowlist is per-framework (Next `allowedDevOrigins`, Vite `server.allowedHosts` best-effort).

## Pending

### 1. Launch

- Buy `renderpr.com` domain
- Build a landing page
- Demo video showcasing the full workflow
- Tweet / X thread
- Product Hunt launch

### 2. npm Cache

It's not only possible — it's a well-known pattern. Here's how it would work:
1. Before npm ci, compute a SHA256 hash of the repo's package-lock.json
2. Check if s3://renderpr-cache-{account}/npm/{hash}.tar.gz exists
3. If cache hit: download the tarball (~5s) and extract it into the repo's node_modules/ — skip npm ci entirely
4. If cache miss: run npm ci, then tar up node_modules and upload to S3 in the background (future runs benefit)
The cache is keyed by the lockfile hash, so it's safe — different dependency trees never collide. Across multiple PRs on the same repo, only the first one pays npm ci; the rest download in seconds.
Implementation: around 20 lines in _start_dev_server + reuse the existing screenshots bucket (or a new one) + an S3 IAM permission.

### 3. Auth-Gated Apps & Env/Secret Injection

**Problem:** The dev server launches with `{**os.environ, "HOST": "0.0.0.0"}` — it injects *nothing* from the repo. No `.env` reading, no credentials. The mock system only fakes outbound calls that already exist in source. So the moment an app needs a `NEXT_PUBLIC_*`/`VITE_*` var or sits behind a login, it renders blank or throws — and the AI then confidently reviews a broken page, which is worse than not running. This is the single biggest "it doesn't work on my app" wall.

**Approach** (three needs, increasing difficulty, each unblocks a class of apps):
- **Env var injection (highest ROI):** read `.env.example` to learn required vars, and inject user-provided secrets stored per-installation/repo (encrypted in SSM/Secrets Manager). Public/build-time vars must be present *before* dev/build starts. Security gate: never inject secrets on fork PRs (ties to untrusted-code isolation).
- **Auth bypass (medium):** ship Playwright `storageState` injection first — user records cookies+localStorage from a logged-in session once, loaded into the browser context before navigating (`newContext({ storageState })`). The 80/20 for skipping login walls without the app cooperating. Config-driven login recipe (visit /login, fill, submit) as fallback; provider-specific session forging (NextAuth/Clerk/Auth0) is the brittle long tail to avoid early.
- **Real backend + seeded data (hardest):** lean into mocks rather than booting databases — user-declared fixtures in config, POST/PUT support, `page.route()` interception for external domains. "Bring-your-own staging API URL" is the escape hatch for teams that need a real backend (later, security-sensitive).

### 4. Repo Config File (`.renderpr.yml`)

**Problem:** There is zero repo-level configuration today — everything is auto-inferred or a constant in `config.py`. Beyond being an escape hatch, a config file is the *delivery vehicle* for the framework-breadth and auth/env-injection features: env var declarations, the login recipe / storageState ref, framework and dev-command overrides, custom viewports, and explicit fixtures all need somewhere to live. Build the config loader first/alongside, not last.

**Approach:**
- Fields map to current hardcodes they override: `install`/`dev`/`build` + `packageManager` (over discovery), `port` (over `DEV_SERVER_PORT`), `framework` (over detection), `viewports` (over `config.py` `VIEWPORTS`), `routes` + per-route actions, `env`/`.env` selection, `auth`, `mocks`/`fixtures`, `paths`/`runOn`.
- **Layered override, not replacement:** schema-validate, then merge over auto-detected defaults so zero-config keeps working and config only overrides what's set.
- **Split where it's consumed:** most is read by the agent after clone, but `runOn`/`paths.ignore` ("only run when `src/**` changes") should be read in the Lambda webhook handler via the GitHub contents API *before* paying to boot a Fargate task — a direct unit-economics win.
- **Surface errors through the progress comment:** an invalid `.renderpr.yml` edits the comment to "config error on line N" instead of failing silently. Version the schema so it can evolve.

### 5. Hosted SaaS Offering

**Problem:** RenderPR is BYOC today — every user deploys the stack into their own AWS and stores secrets in their own SSM (written by a CLI, mirroring `setup-secrets.sh`). That's the right v1 for the auth/env-injection feature, but it's a high barrier for non-AWS users who'd rather pay for a managed product.

**Approach:**
- Standard SaaS funnel: landing page → payment → account → connect the RenderPR GitHub App → per-repo **settings UI** for env vars and the auth method.
- Secrets/auth material live in **our** encrypted store instead of the user's SSM; the Fargate agent reads from there.
- Crucially, because the auth strategy is **session forging / provider admin-API** (not `storageState` capture), the settings UI only needs to collect *secrets* — plain text fields (`NEXTAUTH_SECRET`, `service_role` key, etc.). There is **no cookie/session-capture step**, so the SaaS build stays small: no hosted remote-browser, no record-your-login flow.
- Inherits the same fork-PR secret gate and redaction guarantees as the BYOC path.
