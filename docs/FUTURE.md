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
