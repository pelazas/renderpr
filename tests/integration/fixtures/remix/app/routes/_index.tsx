import { json } from '@remix-run/node';
import { useLoaderData } from '@remix-run/react';
import ItemList, { type Item } from '../components/ItemList';

export const loader = async ({ request }: { request: Request }) => {
  const res = await fetch(new URL('/api/items', request.url));
  return json(await res.json());
};

export default function Index() {
  const data = useLoaderData<typeof loader>() as Item[];
  return (
    <main>
      <h1>Home</h1>
      <ItemList items={data} />
    </main>
  );
}
