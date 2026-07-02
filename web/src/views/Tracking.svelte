<script lang="ts">
  // Logged-USER tracking (who accessed the app, when, with what rights) —
  // ported from the vanilla tracking view. The migration run repurposed
  // this page as "active run tracking", losing the original feature.
  import { onMount } from 'svelte';
  import { authStore } from '../stores/auth';
  import { getLoggedUsers, deleteUser } from '../lib/api';
  import { formatTime } from '../lib/format';

  // ── State (runes) ──────────────────────────────────────────────────

  let users = $state<Record<string, unknown>[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let pendingDelete = $state<string | null>(null);

  // Auth-derived
  let canWrite = $derived($authStore?.canWrite ?? false);
  let permissionResolved = $derived($authStore?.permissionResolved ?? false);
  let denied = $derived(permissionResolved && !canWrite);
  let waitingAuth = $derived(!permissionResolved);
  let empty = $derived(!loading && !error && users.length === 0);

  // ── Data ───────────────────────────────────────────────────────────

  async function fetchUsers(): Promise<void> {
    loading = users.length === 0;
    error = null;
    try {
      const data = await getLoggedUsers();
      users = Array.isArray(data) ? data : [];
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to load users.';
    } finally {
      loading = false;
    }
  }

  async function handleDelete(email: string): Promise<void> {
    if (!window.confirm('Delete user ' + email + '?')) return;
    pendingDelete = email;
    try {
      await deleteUser(email);
      users = users.filter((u) => u.email !== email);
    } catch (err: unknown) {
      error = err instanceof Error ? err.message : 'Failed to delete user.';
    } finally {
      pendingDelete = null;
    }
  }

  function lastSeen(u: Record<string, unknown>): string {
    const v = u.last_seen_at;
    if (typeof v === 'number') return formatTime(new Date(v * 1000).toISOString());
    return v ? formatTime(String(v)) : '';
  }

  onMount(() => {
    fetchUsers();
  });
</script>

<section id="view-tracking">
  {#if waitingAuth}
    <p class="tracking-loading">Checking permissions&hellip;</p>
  {:else if denied}
    <div class="tracking-denied">
      <h2>Access Denied</h2>
      <p>You need write access to view user tracking.</p>
      <a href="#/">Go to Dashboard</a>
    </div>
  {:else}
    <div class="tracking-header">
      <h2>Logged Users</h2>
      <button class="outline" onclick={fetchUsers} disabled={loading}>
        {loading ? 'Refreshing…' : 'Refresh'}
      </button>
    </div>

    {#if loading}
      <p class="tracking-loading">Loading users&hellip;</p>
    {:else if error}
      <p class="tracking-error">{error}</p>
    {:else if empty}
      <p class="tracking-empty">No users tracked yet.</p>
    {:else}
      <table id="tracking-table">
        <thead>
          <tr>
            <th>Email</th>
            <th>Latest Access</th>
            <th>Access Rights</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each users as u (u.email as string)}
            <tr>
              <td>{u.email as string}</td>
              <td>{lastSeen(u)}</td>
              <td>
                <span class="rights-badge" class:writer={u.access_rights === 'writer'}>
                  {(u.access_rights as string) || 'reader'}
                </span>
              </td>
              <td class="td-actions">
                <button
                  class="outline btn-del"
                  disabled={pendingDelete === u.email}
                  onclick={() => handleDelete(u.email as string)}
                >
                  {pendingDelete === u.email ? 'Deleting…' : 'Delete'}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {/if}
</section>

<style>
  .tracking-loading,
  .tracking-empty {
    padding: 2rem;
    text-align: center;
    color: #666;
  }
  .tracking-error {
    padding: 2rem;
    text-align: center;
    color: #b00;
  }
  .tracking-denied {
    padding: 2rem;
    text-align: center;
  }
  .tracking-denied h2 {
    color: #b00;
    margin-bottom: 0.5rem;
  }
  .tracking-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
  }
  .tracking-header h2 {
    margin: 0;
  }
  .rights-badge {
    font-size: 0.8rem;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    background: #eee;
    color: #555;
  }
  .rights-badge.writer {
    background: #d4edda;
    color: #155724;
  }
  .td-actions {
    text-align: right;
  }
  .btn-del {
    font-size: 0.8rem;
    padding: 0.15rem 0.6rem;
  }
</style>
