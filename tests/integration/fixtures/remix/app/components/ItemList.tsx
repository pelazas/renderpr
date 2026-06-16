// ItemList component — shared between the home page and the about page.

export interface Item {
  id: number;
  name: string;
}

export default function ItemList({ items }: { items: Item[] }) {
  return (
    <section data-testid="item-list">
      <p>ItemList component</p>
      <ul>
        {items.map((item) => (
          <li key={item.id}>{item.name}</li>
        ))}
      </ul>
    </section>
  );
}
