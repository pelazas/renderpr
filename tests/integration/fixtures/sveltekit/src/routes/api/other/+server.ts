import { json } from '@sveltejs/kit';

// Left UNMOCKED on purpose: validates the writer's unmocked-fallback path (the
// dead backend means this only resolves once stubbed to return [] + a header).
export const GET = async () => {
  const res = await fetch('http://localhost:9999/other');
  return json(await res.json());
};
