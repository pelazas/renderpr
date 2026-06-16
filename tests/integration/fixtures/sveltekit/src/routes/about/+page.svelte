<script>
  import ItemList from '$lib/ItemList.svelte';

  // Static array proves routing + shared-component reuse without a backend.
  const items = [
    { id: 1, name: 'About A' },
    { id: 2, name: 'About B' }
  ];
</script>

<main>
  <h1>About</h1>
  <ItemList items={items} />
</main>
