# Phase 3 — End-to-end integration validation

Runtime validation of the multi-framework work (Phases 0–2) against **real apps**: each
framework's dev server is actually booted and HTTP-probed, exercising the REAL `src/agent`
detection + mock-writer code (no mocks, no AWS). Harness: [`tests/integration/`](../tests/integration).

## Matrix

| Framework | Package manager | install | boot | host-bind | server-mock-renders | unmocked-fallback | banner | routing |
|-----------|-----------------|---------|------|-----------|---------------------|-------------------|--------|---------|
| SvelteKit | pnpm (`--frozen-lockfile`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Astro | **Yarn Berry v4** (`--immutable`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Remix | bun (`--frozen-lockfile`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Next | npm (`ci`) — control | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**4/4 frameworks pass every cell.** Run inside the project's Docker image (Node 22 + npm/pnpm/yarn/bun + the branch's `src/agent`), fixtures executed serially.

## What each cell proves
- **install** — `discover_frontend` → `build_launch_profile` picked the right package manager + install command (asserted equal to the expected, e.g. Astro → `yarn install --immutable`, Remix → `bun install --frozen-lockfile`), and the frozen/immutable install actually succeeded.
- **boot** — the dev server started and answered HTTP within the timeout, via the real `main._start_dev_process` + `_resolve_ready_port`.
- **host-bind** — the server is reachable on `0.0.0.0` (the `--host`/HOST env binding), not just loopback.
- **server-mock-renders** — a page that **SSR-fetches** an internal `/api/items` renders WITH the 5 mocked items in the returned HTML, and `/api/items` itself returns the 5-item JSON. This is the core Phase-2 claim: SvelteKit `+page.server.ts` `event.fetch`, Astro frontmatter `fetch`, Remix `loader`, Next server component — all served by the writer's on-disk endpoint.
- **unmocked-fallback** — a second `/api/other` (hitting a dead backend) left unmocked degrades to `[]` + the `x-renderpr-unmocked` header instead of a 500.
- **banner** — the served HTML references `/__renderpr-unmocked.js` and the script serves with its init guard.
- **routing** — a second page sharing a component renders (file-system routing + shared-component resolution).

## Negative control (proves the mock is load-bearing)
SvelteKit fixture booted with **no RenderPR writers at all**:

```
[negctl:sveltekit] profile pm=pnpm install=['pnpm', 'install', '--frozen-lockfile'] dev=['pnpm', 'run', 'dev', '--host']
[negctl:sveltekit] install rc=0
[negctl:sveltekit] booted port=5173
[negctl:sveltekit] GET /api/items -> 500 body[:120]='{"message":"Internal Error"}'
[negctl:sveltekit] GET / -> 200 contains 'Item 1'=False
[negctl:sveltekit] NEGATIVE-CONTROL OK (breaks without mock)
```

Without the writer, the internal `/api/items` endpoint hits its dead backend → **500**, and the homepage does **not** render the items. The green `server-mock-renders` above is therefore entirely attributable to the mock writer, not a coincidence.

## Private fixture repos
One private repo per framework (also committed under `tests/integration/fixtures/<fw>/` for a self-contained PR):

- https://github.com/pelazas/renderpr-e2e-sveltekit
- https://github.com/pelazas/renderpr-e2e-astro
- https://github.com/pelazas/renderpr-e2e-remix
- https://github.com/pelazas/renderpr-e2e-next

## How to re-run
```bash
# from the worktree root
docker build -t renderpr:harness .

# against the committed fixtures (deterministic):
docker run --rm -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
  -v "$PWD":/work -w /work renderpr:harness \
  python -m tests.integration.entrypoint --frameworks all --use-local --out /work/tests/integration/_out

# against the private repos (full clone→detect→install→boot path):
docker run --rm -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 -e GH_TOKEN="$(gh auth token)" \
  -v "$PWD":/work -w /work renderpr:harness \
  python -m tests.integration.entrypoint --frameworks all --out /work/tests/integration/_out
```

Both paths were run; the table above is the result. A genuine failure surfaces as an explicit `FAIL`
with captured install/dev logs — never a silent skip.

## Scope
This phase adds **no production `src/agent` changes** — validation only. Each fixture's app code
exercises the same surfaces RenderPR sees in the wild (file-system routes, a shared component, an
internal `/api` the page fetches server-side). Direct DB access not routed through an `/api` fetch
remains out of scope (the same limitation Next has).
