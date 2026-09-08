<script lang="ts">
  import UnifiedDashboard from './UnifiedDashboard.svelte';
  import { nt } from '../lib/navigation.svelte';
</script>
<section class="repository-tools">
  <nav aria-label={nt('repositories')}><a href="#/runs">← {nt('runs')}</a><a href="#/">{nt('projects')}</a></nav>
  <UnifiedDashboard repositoryOnly={true}/>
</section>
<style>
  nav{display:flex;justify-content:flex-start;gap:1rem;flex-wrap:wrap;font-size:.8rem;margin-bottom:.8rem;}
</style>
