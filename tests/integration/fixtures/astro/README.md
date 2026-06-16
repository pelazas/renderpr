# renderpr-e2e-astro

RenderPR **Phase 3** end-to-end test fixture.

A minimal but real [Astro](https://astro.build) app in **SSR mode**
(`output: 'server'` + `@astrojs/node` standalone). The homepage SSR-fetches
`/api/items`, whose endpoint proxies a dead backend on `localhost:9999`. The
page therefore **500s unless** RenderPR's Astro mock writer replaces
`src/pages/api/items.ts` with the mock payload.

- Package manager: **Yarn Berry (v4)** — `packageManager: "yarn@4.5.0"`,
  `nodeLinker: node-modules`. Exercises Phase-1 yarn-berry `--immutable`
  install detection.
- `/api/other` is left unmocked to verify the graceful-degradation fallback.
- `/about` exercises routing + the shared `ItemList` component.

Do not run this app standalone; it is driven by the integration harness.
