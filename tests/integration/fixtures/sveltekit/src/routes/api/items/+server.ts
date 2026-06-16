import { json } from '@sveltejs/kit';

// Hits a DEAD backend (port 9999) on purpose: this 500s/errors unless RenderPR's
// mock writer replaces this endpoint with mock data. This is the crux of the test.
export const GET = async () => {
  const res = await fetch('http://localhost:9999/items');
  return json(await res.json());
};
