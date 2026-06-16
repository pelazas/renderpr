<script>
  import ItemList from '$lib/ItemList.svelte';

  export let data;
</script>

<main>
  <h1>RenderPR SvelteKit fixture</h1>
  <ItemList items={data.items} />
</main>
