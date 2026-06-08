# Future Features

Ideas scoped out but not yet implemented. Ordered by priority.

## 1. Route-Aware Screenshot Navigation

**Problem:** Currently screenshots only capture the homepage (`localhost:3000`). Real PRs change specific pages.

**Approach:**
- Parse git diff to find changed file paths
- Strip framework conventions to guess routes (e.g., `app/profile/page.tsx` → `/profile`, `pages/about.tsx` → `/about`)
- Framework-specific inference: Next.js app dir, Next.js pages router, React Router `routes.tsx`
- Navigate to each unique route and screenshot at all viewports
- If route can't be inferred (e.g., shared component), screenshot from parent route or homepage

**Priority:** High — core value prop

## 2. Live Refresh / Hot Reload Mode

**Problem:** After the initial review, the agent exits. For rapid iteration, the user wants to make a change, push, and see new screenshots without a new webhook.

**Approach:**
- Keep the dev server and browser alive in the Fargate container
- Poll for new commits on the PR branch
- When a new commit is detected, re-run Playwright screenshots + LLM review
- Post incremental review as a new comment
- Maintain conversation context across refreshes

**Priority:** Medium — significant infra cost to keep Fargate running

## 3. Monorepo / Package.json Discovery

**Problem:** Not all PRs have `package.json` at the repo root. Some use monorepos (`packages/web/package.json`), others are backend-only.

**Approach:**
- Scan repo for `package.json` files (up to a depth limit)
- Filter by heuristics: presence of `next`, `react`, `vite`, `@angular/core` in dependencies
- If multiple candidates found, pick the one with the most frontend-indicative deps
- If none found, post diagnostic comment and exit

**Priority:** Medium — needed for broader repo compatibility

## 4. Conversational Polling (`@renderpr`)

**Problem:** After posting the review, the agent currently exits. Users can't ask follow-ups.

**Approach:**
- Enter a polling loop after the initial review
- Fetch new comments every 10s
- On `@renderpr <command>`, execute:
  - `review` — full re-review
  - `render [viewports]` — specific viewport screenshots
  - `dark mode` — enable `prefers-color-scheme: dark`
  - `accessibility` / `a11y` — Playwright accessibility scan
  - `help` — list commands
- After 15 min idle, post closing summary and exit

**Priority:** Medium — good UX, but the core review comes first

## 5. Fixture Generation

**Problem:** `.renderpr/fixtures/` is empty. Without mock data, screenshots show loading spinners or empty states.

**Approach:**
- Before running the app, start a proxy/mitm server
- Record API responses during initial navigation
- Save them as fixture JSON files
- On subsequent navigations, replay fixtures via Playwright route interception
- Optionally commit fixtures to the PR's repo

**Priority:** Low — manual fixtures work for demos
