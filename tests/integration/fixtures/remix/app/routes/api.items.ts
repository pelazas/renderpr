import { json } from '@remix-run/node';

// DEAD backend on port 9999. Unmocked, this fetch fails and the loader 500s,
// which is exactly what RenderPR's mock writer replaces.
export const loader = async () => {
  const res = await fetch('http://localhost:9999/items');
  return json(await res.json());
};
