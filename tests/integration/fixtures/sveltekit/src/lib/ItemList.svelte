<script>
  // ItemList component — shared across pages, renders item names in a list.
  export let items = [];
</script>

<section data-testid="item-list">
  <p>ItemList component</p>
  <ul>
    {#each items as item}
      <li>{item.name}</li>
    {/each}
  </ul>
</section>
