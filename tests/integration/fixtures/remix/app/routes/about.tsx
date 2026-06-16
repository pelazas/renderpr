import ItemList, { type Item } from '../components/ItemList';

const STATIC_ITEMS: Item[] = [
  { id: 1, name: 'About 1' },
  { id: 2, name: 'About 2' },
  { id: 3, name: 'About 3' },
];

export default function About() {
  return (
    <main>
      <h1>About</h1>
      <ItemList items={STATIC_ITEMS} />
    </main>
  );
}
