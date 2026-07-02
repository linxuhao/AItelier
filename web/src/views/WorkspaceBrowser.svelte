<script lang="ts">
  // File browser for a project workspace root — "dps" (pipeline artifacts)
  // or "code" (the generated project repository). Ported from the vanilla
  // workspace <details> sections; lazy-loads on first expand and opens file
  // contents in a dialog.
  import { workspaceTree, workspaceFile } from '../lib/api';

  let {
    projectId,
    root = 'dps',
    title = 'Files',
    startOpen = false,
  }: {
    projectId: string;
    root?: 'dps' | 'code';
    title?: string;
    startOpen?: boolean;
  } = $props();

  let files = $state<string[]>([]);
  let loaded = $state(false);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // File dialog
  let dialogOpen = $state(false);
  let filePath = $state('');
  let fileContent = $state('');
  let fileTruncated = $state(false);
  let fileTotal = $state(0);

  async function loadTree(): Promise<void> {
    if (loaded || loading || !projectId) return;
    loading = true;
    error = null;
    try {
      const data = await workspaceTree(projectId, root);
      files = (data && data.tree) || [];
      loaded = true;
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to load files.';
    } finally {
      loading = false;
    }
  }

  function onToggle(e: Event): void {
    if ((e.target as HTMLDetailsElement).open) loadTree();
  }

  async function openFile(path: string): Promise<void> {
    filePath = path;
    fileContent = '';
    fileTruncated = false;
    dialogOpen = true;
    try {
      const data = await workspaceFile(projectId, path, root);
      fileContent = (data.content as string) ?? '';
      fileTruncated = !!data.truncated;
      fileTotal = (data.total_lines as number) || 0;
    } catch (err: unknown) {
      fileContent = 'Failed to load file: ' +
        (err instanceof Error ? err.message : String(err));
    }
  }

  // ── Nested foldable tree (ported from the vanilla _buildTreeIndices /
  //    _renderTreeLevel) — folders start collapsed, click to expand. ──
  interface DirNode {
    name: string;
    path: string;
    dirs: DirNode[];
    files: { name: string; path: string }[];
  }

  let expandedDirs = $state<Set<string>>(new Set());

  const tree = $derived(buildTree(files));

  function buildTree(paths: string[]): DirNode {
    const root: DirNode = { name: '', path: '', dirs: [], files: [] };
    const dirIndex = new Map<string, DirNode>([['', root]]);
    for (const p of paths) {
      const parts = p.split('/');
      let node = root;
      let prefix = '';
      for (let i = 0; i < parts.length - 1; i++) {
        prefix = prefix ? prefix + '/' + parts[i] : parts[i];
        let child = dirIndex.get(prefix);
        if (!child) {
          child = { name: parts[i], path: prefix, dirs: [], files: [] };
          dirIndex.set(prefix, child);
          node.dirs.push(child);
        }
        node = child;
      }
      node.files.push({ name: parts[parts.length - 1], path: p });
    }
    for (const node of dirIndex.values()) {
      node.files.sort((a, b) => a.name.localeCompare(b.name));
      node.dirs.sort((a, b) => a.name.localeCompare(b.name));
    }
    return root;
  }

  function toggleDir(path: string): void {
    const next = new Set(expandedDirs);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    expandedDirs = next;
  }
</script>

<details class="workspace-section" open={startOpen} ontoggle={onToggle}>
  <summary>
    <strong>{title}</strong>
    {#if loaded}<span class="ws-count">{files.length} file(s)</span>{/if}
  </summary>

  {#if loading}
    <p class="ws-muted">Loading files…</p>
  {:else if error}
    <p class="ws-error">{error}</p>
  {:else if loaded && files.length === 0}
    <p class="ws-muted">No files.</p>
  {:else if loaded}
    <div class="ws-file-list">
      {@render treeLevel(tree)}
    </div>
  {/if}
</details>

{#snippet treeLevel(node: DirNode)}
  <ul class="ws-tree">
    {#each node.files as f (f.path)}
      <li>
        <button class="ws-file" onclick={() => openFile(f.path)} title={f.path}>
          <span class="ws-icon">📄</span>{f.name}
        </button>
      </li>
    {/each}
    {#each node.dirs as d (d.path)}
      <li>
        <button class="ws-dir" onclick={() => toggleDir(d.path)} title={d.path}>
          <span class="ws-icon">{expandedDirs.has(d.path) ? '📂' : '📁'}</span>{d.name}
        </button>
        {#if expandedDirs.has(d.path)}
          {@render treeLevel(d)}
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

{#if dialogOpen}
  <dialog open class="ws-file-dialog" onclose={() => (dialogOpen = false)}>
    <article>
      <header>
        <code>{filePath}</code>
        <button class="ws-close" onclick={() => (dialogOpen = false)} aria-label="Close">&times;</button>
      </header>
      <pre class="ws-file-content">{fileContent}</pre>
      {#if fileTruncated}
        <footer class="ws-muted">Truncated — {fileTotal} lines total.</footer>
      {/if}
    </article>
  </dialog>
{/if}

<style>
  .workspace-section {
    margin-top: 1rem;
    border: 1px solid var(--pico-muted-border-color, #e0e0e0);
    border-radius: 0.4rem;
    padding: 0.5rem 0.75rem;
    background: var(--pico-card-background-color, #fff);
  }
  .workspace-section summary {
    cursor: pointer;
  }
  .ws-count {
    margin-left: 0.5rem;
    font-size: 0.8rem;
    color: var(--pico-muted-color, #888);
  }
  .ws-file-list {
    margin: 0.5rem 0 0;
    max-height: 320px;
    overflow-y: auto;
    font-family: monospace;
    font-size: 0.8rem;
  }
  .ws-tree {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .ws-tree .ws-tree {
    padding-left: 1.2rem;
  }
  .ws-tree li {
    padding-top: 0.05rem;
    padding-bottom: 0.05rem;
  }
  .ws-file,
  .ws-dir {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.1rem 0;
    width: auto;
    font: inherit;
    text-align: left;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .ws-file {
    color: var(--pico-primary, #0066cc);
  }
  .ws-file:hover {
    text-decoration: underline;
  }
  .ws-dir {
    color: inherit;
    font-weight: 600;
  }
  .ws-dir:hover {
    text-decoration: underline;
  }
  .ws-icon {
    font-size: 0.85rem;
  }
  .ws-muted {
    color: var(--pico-muted-color, #888);
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }
  .ws-error {
    color: #b00;
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }
  .ws-file-dialog article {
    max-width: 900px;
    width: 90vw;
  }
  .ws-file-dialog header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }
  .ws-close {
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    width: auto;
    padding: 0 0.25rem;
    line-height: 1;
    color: inherit;
  }
  .ws-file-content {
    max-height: 60vh;
    overflow: auto;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
