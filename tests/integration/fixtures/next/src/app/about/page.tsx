import ItemList from "../../components/ItemList";

const items = [
  { id: 1, name: "About 1" },
  { id: 2, name: "About 2" },
  { id: 3, name: "About 3" },
];

export default function About() {
  return (
    <main>
      <h1>About</h1>
      <ItemList items={items} />
    </main>
  );
}
