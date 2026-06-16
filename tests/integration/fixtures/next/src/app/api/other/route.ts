export async function GET() {
  const res = await fetch("http://localhost:9999/other");
  return Response.json(await res.json());
}
