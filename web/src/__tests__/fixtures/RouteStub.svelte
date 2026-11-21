<script lang="ts">
  // Stands in for a routed view so a router test renders no real page.
  let { params = {} }: { params?: Record<string, string> } = $props();
</script>

<p data-testid="route-stub">{JSON.stringify(params)}</p>
