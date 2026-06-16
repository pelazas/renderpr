import { json } from '@remix-run/node';

// DEAD backend on port 9999. Left UNMOCKED so the blind-stub fallback applies.
export const loader = async () => {
  const res = await fetch('http://localhost:9999/other');
  return json(await res.json());
};
