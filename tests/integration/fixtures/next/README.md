# renderpr-e2e-next

RenderPR **Phase 3** end-to-end test fixture — the **CONTROL** column (npm).

A minimal but real [Next.js](https://nextjs.org) **App Router** app. The homepage
(`src/app/page.tsx`) is an async server component that SSR-fetches `/api/items`.
That route handler (`src/app/api/items/route.ts`) proxies a dead backend on
`localhost:9999`, so the page **500s unless** RenderPR's `NextMockWriter`
replaces the route with the mock payload.

Next mocking already works in production, so a green Next column validates the
**harness itself**.

- Package manager: **npm** — installed with `npm ci`.
- `/api/other` is left **unmocked** to verify the graceful-degradation fallback.
- `/about` exercises routing + the shared `ItemList` component (`src/components/ItemList.tsx`).
- The homepage is `export const dynamic = "force-dynamic"` so it SSRs per request
  (never prerendered at build, where the dead backend would fail).

Do not run this app standalone; it is driven by the integration harness.
