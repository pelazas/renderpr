export async function GET() {
  const res = await fetch("http://localhost:9999/items");
  return Response.json(await res.json());
}
