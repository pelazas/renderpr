# renderpr-e2e-sveltekit

RenderPR Phase-3 end-to-end test fixture.

A minimal but real SvelteKit app (adapter-node, SSR) used by the integration
harness in `tests/integration/`. Its homepage SSR-loads `/api/items`, whose
`+server.ts` fetches a **dead** backend (`http://localhost:9999`). Without
RenderPR's mock writer the homepage 500s; the harness runs the real
`src.agent` mock-writer code against this fixture and asserts the page renders
with mock data, that `/api/other` (left unmocked) degrades to `[]` + a fallback
header, and that the shared `ItemList` component renders on `/about` too.

Not meant to be run standalone — see the fixture spec in `spec.py`.
