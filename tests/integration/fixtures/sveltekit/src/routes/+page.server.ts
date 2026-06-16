export const load = async ({ fetch }) => {
  const res = await fetch('/api/items');
  const items = await res.json();
  return { items };
};
