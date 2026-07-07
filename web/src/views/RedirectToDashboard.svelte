<script lang="ts">
  import { onMount } from 'svelte';
  import { push } from 'svelte-spa-router';

  onMount(() => {
    push('#/');
  });
</script>

<article aria-busy="true">
  <p>Redirecting…</p>
</article>
