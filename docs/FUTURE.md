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

### 3. Framework & Package-Manager Breadth

> **Status:** Phase 1 shipped (package manager + dev-command + port detection — see `src/agent/stack.py`). Phase 2 (per-framework routing + the `page.route()` mock rewrite) is still pending; the remaining work is the **Route/mock model** bullet below.

**Problem:** Discovery is hardcoded to npm + Next.js + port 3000. On a non-npm or non-Next repo, RenderPR doesn't degrade — it *hard-fails* in three independent places, so every unsupported stack is silent churn:
- **Install:** `_start_dev_server` runs a literal `npm ci`, and the S3 cache key is hashed only from `package-lock.json`. `npm ci` requires that lockfile, so pnpm/yarn repos fail before anything else and never hit the cache.
- **Boot/readiness:** the ready-check polls `localhost:3000` for a 200, but Vite (5173), Astro (4321), and SvelteKit (5173) never answer there, so even a good install dead-ends in a 60s timeout. `dev_command` is the hardcoded string `"npm run dev"`.
- **Route/mock model:** inference is Next App-Router-specific (`app/**/page.tsx` → URL, mocks written as `app/api/**/route.ts`). None of it maps to Vite SPAs, Remix, Astro, or SvelteKit.

**Approach:**
- **Package manager:** detect by lockfile (`pnpm-lock.yaml`/`yarn.lock`/`package-lock.json`/`bun.lockb`) → pick the install command and generalize the cache key. Watch the pnpm subtlety: its `node_modules` is symlinks into a global store, so the existing tar-to-S3 approach captures dangling links — cache the pnpm store dir or force `node-linker=hoisted`.
- **Boot:** derive the dev script from `package.json`; detect framework from deps to pass the right host flag (Vite/Astro `--host`, CRA `DANGEROUSLY_DISABLE_HOST_CHECK`, Next `HOST=0.0.0.0`); replace the fixed-port poll by sniffing the dev server's stdout for the printed "Local: http://…:PORT" line. Note `write_next_allowed_origin` is one instance of a general "dev-origin allowlist" quirk each framework has (Vite `server.allowedHosts`, CRA host check).
- **Route/mock model:** make routing a per-framework strategy; degrade SPAs to home + LLM-found routes. Higher-leverage: stop writing Next route-handler files and intercept mocks at the browser layer with Playwright `page.route()` — removes the Next coupling, works everywhere, and covers third-party API domains.

Sequencing: package manager + dev-command/port first (unblocks the most repos), then per-framework routing + the `page.route()` mock rewrite.

### 4. Auth-Gated Apps & Env/Secret Injection

**Problem:** The dev server launches with `{**os.environ, "HOST": "0.0.0.0"}` — it injects *nothing* from the repo. No `.env` reading, no credentials. The mock system only fakes outbound calls that already exist in source. So the moment an app needs a `NEXT_PUBLIC_*`/`VITE_*` var or sits behind a login, it renders blank or throws — and the AI then confidently reviews a broken page, which is worse than not running. This is the single biggest "it doesn't work on my app" wall.

**Approach** (three needs, increasing difficulty, each unblocks a class of apps):
- **Env var injection (highest ROI):** read `.env.example` to learn required vars, and inject user-provided secrets stored per-installation/repo (encrypted in SSM/Secrets Manager). Public/build-time vars must be present *before* dev/build starts. Security gate: never inject secrets on fork PRs (ties to untrusted-code isolation).
- **Auth bypass (medium):** ship Playwright `storageState` injection first — user records cookies+localStorage from a logged-in session once, loaded into the browser context before navigating (`newContext({ storageState })`). The 80/20 for skipping login walls without the app cooperating. Config-driven login recipe (visit /login, fill, submit) as fallback; provider-specific session forging (NextAuth/Clerk/Auth0) is the brittle long tail to avoid early.
- **Real backend + seeded data (hardest):** lean into mocks rather than booting databases — user-declared fixtures in config, POST/PUT support, `page.route()` interception for external domains. "Bring-your-own staging API URL" is the escape hatch for teams that need a real backend (later, security-sensitive).

### 5. Repo Config File (`.renderpr.yml`)

**Problem:** There is zero repo-level configuration today — everything is auto-inferred or a constant in `config.py`. Beyond being an escape hatch, a config file is the *delivery vehicle* for features 3 and 4: env var declarations, the login recipe / storageState ref, framework and dev-command overrides, custom viewports, and explicit fixtures all need somewhere to live. Build the config loader first/alongside, not last.

**Approach:**
- Fields map to current hardcodes they override: `install`/`dev`/`build` + `packageManager` (over discovery), `port` (over `DEV_SERVER_PORT`), `framework` (over detection), `viewports` (over `config.py` `VIEWPORTS`), `routes` + per-route actions, `env`/`.env` selection, `auth`, `mocks`/`fixtures`, `paths`/`runOn`.
- **Layered override, not replacement:** schema-validate, then merge over auto-detected defaults so zero-config keeps working and config only overrides what's set.
- **Split where it's consumed:** most is read by the agent after clone, but `runOn`/`paths.ignore` ("only run when `src/**` changes") should be read in the Lambda webhook handler via the GitHub contents API *before* paying to boot a Fargate task — a direct unit-economics win.
- **Surface errors through the progress comment:** an invalid `.renderpr.yml` edits the comment to "config error on line N" instead of failing silently. Version the schema so it can evolve.
