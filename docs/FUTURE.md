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

### 7. npm Cache

**Problem:** Every review re-ran a full dependency install (e.g. `npm ci` ~4–5 min) even when the lockfile was unchanged across PRs on the same repo.

**Approach:**
- Key an S3 tarball of `node_modules` by `{package-manager}-{lockfile-hash}` (in the screenshots bucket). On a hit, download + extract and skip install (~15s vs ~4–5 min); on a miss, install then upload in the background so later runs benefit.
- Works across npm/yarn/pnpm/bun: the store tolerates `tar`'s non-fatal exit 1 (files changing mid-archive) and excludes volatile/derived dirs (e.g. Vite's `node_modules/.vite`) so concurrent dev-server churn doesn't break it. (Generalises the per-PM cache key noted in #6.)

### 8. Auth-Gated Apps & Env/Secret Injection

**Problem:** The dev server injected nothing from the repo, so any app needing a `NEXT_PUBLIC_*`/`VITE_*` var or sitting behind a login rendered blank/threw — and the AI confidently reviewed a broken page.

**Approach:**
- **Env injection:** read `.env.example` (and `.renderpr.yml` `env`) to learn declared vars, then inject per-repo secrets (SSM, `get_parameters_by_path`) as an ephemeral `.env.local` + dev-server env *before* boot. Injection is scoped to the app's declared vars (provider/admin secrets used only by the auth layer stay out of the app env); secrets are never injected on fork PRs and never logged.
- **Synthetic-session auth:** mint a session for a *synthetic* user by forging from the app's own signing secret or calling the provider's admin API — NextAuth v4/v5 (JWE), generic JWT, Supabase (forge or GoTrue admin), Clerk, Firebase. OAuth (Google/GitHub/SSO) "just works" because the app/provider self-issues the minted session; the real login is never scripted. A login-wall guard degrades the progress comment with guidance when an unconfigured app still lands on a login page.

### 9. Repo Config File (`.renderpr.yml`)

**Problem:** There was zero repo-level configuration — everything was auto-inferred or a `config.py` constant — and no home for env/auth declarations.

**Approach:**
- Optional `.renderpr.yml`, **layered over auto-detection** (merge, not replace), so zero-config keeps working and config only overrides what's set: `env` (`from`/`vars`), `auth` (`type` + synthetic `user`).
- Schema-validated; an invalid file surfaces through the progress comment instead of failing silently. Secret *values* always live in SSM, never in the file.

## Pending

### 1. Launch

- Buy `renderpr.com` domain
- Build a landing page
- Demo video showcasing the full workflow
- Tweet / X thread
- Product Hunt launch

### 2. Hosted SaaS Offering

**Problem:** RenderPR is BYOC today — every user deploys the stack into their own AWS and stores secrets in their own SSM (written by a CLI, mirroring `setup-secrets.sh`). That's the right v1 for the auth/env-injection feature, but it's a high barrier for non-AWS users who'd rather pay for a managed product.

**Approach:**
- Standard SaaS funnel: landing page → payment → account → connect the RenderPR GitHub App → per-repo **settings UI** for env vars and the auth method.
- Secrets/auth material live in **our** encrypted store instead of the user's SSM; the Fargate agent reads from there.
- Crucially, because the auth strategy is **session forging / provider admin-API** (not `storageState` capture), the settings UI only needs to collect *secrets* — plain text fields (`NEXTAUTH_SECRET`, `service_role` key, etc.). There is **no cookie/session-capture step**, so the SaaS build stays small: no hosted remote-browser, no record-your-login flow.
- Inherits the same fork-PR secret gate and redaction guarantees as the BYOC path.
