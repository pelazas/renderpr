import { headers } from "next/headers";
import ItemList from "../components/ItemList";

// Force per-request SSR so the page is never prerendered at build time (the
// real backend is dead until RenderPR mocks /api/items).
export const dynamic = "force-dynamic";

export default async function Home() {
  const h = await headers();
  const host = h.get("host");
  const res = await fetch(`http://${host}/api/items`, { cache: "no-store" });
  const items = await res.json();

  return (
    <main>
      <h1>Items</h1>
      <ItemList items={items} />
    </main>
  );
}
