<script lang="ts">
  import { onMount } from 'svelte';
  import { replace } from 'svelte-spa-router';

  // replace, not push: the unknown route must not stay in history, or Back
  // would land on it and bounce forward again.
  onMount(() => {
    replace('/');
  });
</script>

<article aria-busy="true">
  <p>Redirecting…</p>
</article>
