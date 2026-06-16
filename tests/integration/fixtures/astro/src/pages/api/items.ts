export const GET = async () => {
  const res = await fetch('http://localhost:9999/items');
  return new Response(JSON.stringify(await res.json()), {
    headers: { 'content-type': 'application/json' },
  });
};
