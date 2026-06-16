# renderpr-e2e-remix

RenderPR **Phase-3 end-to-end test fixture** — a minimal but real Remix
(Vite-based) app (Remix v2).

## What it does

The homepage (`/`) SSR-fetches `/api/items`, whose resource route
(`app/routes/api.items.ts`) calls a **dead backend on `http://localhost:9999`**.
Unmocked, that fetch fails and the loader **500s**. The fixture only renders a
healthy homepage once RenderPR's mock writer replaces the `/api/items` resource
route with the mocked payload.

- `/api/items` — **mocked** by the spec (see `spec.py`).
- `/api/other` — left **unmocked**, exercising the blind-stub fallback
  (`[]` + the RenderPR fallback header) and the unmocked banner injected into
  `app/root.tsx`.

`ItemList` (`app/components/ItemList.tsx`) is shared by the home page and the
`/about` page.

Assigned package manager: **bun** (`bun install --frozen-lockfile`).
