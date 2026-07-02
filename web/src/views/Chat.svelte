<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { push } from 'svelte-spa-router';
  import { authStore } from '../stores/auth';
  import { connectionStore } from '../stores/connection';
  import { projectStore, setCurrentProject } from '../stores/project';
  import {
    createSession,
    getChatHistory,
    listSessions,
  } from '../lib/api';
  import { renderMarkdown } from '../lib/markdown';
  import { formatTime, escapeHtml, truncate } from '../lib/format';

  // ── Route params ──

  let { params = {} as Record<string, string> } = $props();

  // ── Constants ──

  const _SESSION_KEY = 'aitelier.chat.sessionId';

  const _SLASH_COMMAND_DEFS = [
    { cmd: '/help', desc: 'Show available commands', arg: false },
    { cmd: '/clear', desc: 'Clear the chat history and visible messages', arg: false },
    { cmd: '/projects', desc: 'Go to the project dashboard', arg: false },
    { cmd: '/project', desc: 'Open a project by id (e.g. /project my-app)', arg: true },
  ];

  // ── Types ──

  interface ChatMessage {
    role: 'user' | 'assistant' | 'agent' | 'system' | 'tool' | 'error';
    content: string;
  }

  interface SlashCommand {
    cmd: string;
    desc: string;
    arg: boolean;
  }

  // ── State (Svelte 5 runes) ──

  let messages = $state<ChatMessage[]>([]);
  let draft = $state('');
  let sending = $state(false);
  let agentStreaming = $state(false);
  let error = $state<string | null>(null);
  let loading = $state(true);
  let history: ChatMessage[] = $state([]);
  let sessionId = $state<string | null>(null);
  let sessionInitiated = $state(false);
  let sessionList = $state<Record<string, unknown>[]>([]);
  let selectedSessionId = $state<string | null>(null);

  // Streaming state
  let abortController: AbortController | null = null;
  let currentAgentText = $state('');

  // Slash completion state
  let completionMatches = $state<SlashCommand[]>([]);
  let completionIndex = $state(0);
  let completionVisible = $state(false);

  let messagesContainerEl: HTMLDivElement | undefined = $state();

  // ── Derived ──

  let canWrite = $derived($authStore.permissionResolved && $authStore.canWrite);
  let connected = $derived($connectionStore.connectionOk);
  let projectId = $derived(params.id || '');

  let inputDisabled = $derived(!connected || sending || agentStreaming);
  let canSend = $derived(!sending && !agentStreaming && draft.trim().length > 0 && connected);

  // ── LocalStorage helpers ──

  function _readStoredSessionId(): string | null {
    try {
      return window.localStorage.getItem(_SESSION_KEY) || null;
    } catch {
      return null;
    }
  }

  function _storeSessionId(id: string | null): void {
    try {
      if (id) {
        window.localStorage.setItem(_SESSION_KEY, id);
      } else {
        window.localStorage.removeItem(_SESSION_KEY);
      }
    } catch {
      // Storage unavailable — chat still works.
    }
  }

  // ── Session management ──

  async function _initSession(forceNew: boolean = false): Promise<string | null> {
    if (sessionInitiated && sessionId && !forceNew) {
      return sessionId;
    }

    // Re-attach to stored session unless forced new. This runs BEFORE the
    // permission check: re-attaching is a read, and canWrite below may still
    // be the unresolved fail-closed default.
    if (!forceNew) {
      const stored = _readStoredSessionId();
      if (stored) {
        sessionId = stored;
        sessionInitiated = true;
        return stored;
      }
    }

    // Read-only users skip session creation (a write). Only latch this
    // decision once /api/me has actually resolved: latching the fail-closed
    // default dropped the session id for the whole page lifetime (silently
    // unsaved history + the butler re-starting a pipeline run per message).
    if (!$authStore.canWrite) {
      if ($authStore.permissionResolved) {
        sessionInitiated = true;
      }
      return null;
    }

    try {
      const data = await createSession();
      const sid = (data && data.session_id) || null;
      sessionId = sid;
      sessionInitiated = true;
      _storeSessionId(sid);
      return sid;
    } catch {
      sessionInitiated = true;
      return null;
    }
  }

  async function _restoreHistory(): Promise<void> {
    if (!sessionId) return;

    try {
      const response = await getChatHistory(sessionId);
      if (!response || !response.messages || !Array.isArray(response.messages)) {
        return;
      }

      // Build dedup key set from existing history
      const existingKeys = new Set<string>();
      for (const msg of history) {
        const key = msg.role + '|' + (msg.content || '').slice(0, 100);
        existingKeys.add(key);
      }

      let added = false;
      for (const m of response.messages) {
        const dedupKey = (m.role as string) + '|' + (m.content as string || '').slice(0, 100);
        if (existingKeys.has(dedupKey)) continue;

        const displayRole = m.role === 'assistant' ? 'agent' : (m.role as string);
        messages = [...messages, { role: displayRole as ChatMessage['role'], content: m.content as string }];
        history = [...history, { role: m.role as ChatMessage['role'], content: m.content as string }];
        existingKeys.add(dedupKey);
        added = true;
      }

      if (added && messagesContainerEl) {
        requestAnimationFrame(() => {
          if (messagesContainerEl) messagesContainerEl.scrollTop = messagesContainerEl.scrollHeight;
        });
      }
    } catch {
      // Silently skip — chat still works without history
    }
  }

  async function _loadSessionList(): Promise<void> {
    try {
      // List ALL sessions (no project filter) for the global session switcher
      const response = await listSessions(null);
      if (response && response.sessions && Array.isArray(response.sessions)) {
        sessionList = response.sessions;
      }
    } catch {
      // Silently skip
    }
  }

  async function _switchSession(newSessionId: string): Promise<void> {
    if (newSessionId === sessionId) return;

    _abortStream();
    messages = [];
    history = [];
    sessionId = newSessionId;
    _storeSessionId(newSessionId);
    selectedSessionId = newSessionId;
    await _restoreHistory();
  }

  async function _handleNewSession(): Promise<void> {
    _abortStream();
    messages = [];
    history = [];
    sessionId = null;
    sessionInitiated = false;
    await _initSession(true);
    selectedSessionId = sessionId;
    await _loadSessionList();
  }

  // ── Slash commands ──

  function _updateCompletion(text: string): void {
    if (text.charAt(0) !== '/' || /\s/.test(text)) {
      completionMatches = [];
      completionVisible = false;
      return;
    }

    const partial = text.toLowerCase();
    const matches = _SLASH_COMMAND_DEFS.filter(c => c.cmd.indexOf(partial) === 0);

    if (matches.length === 0 || (matches.length === 1 && matches[0].cmd === partial)) {
      completionMatches = [];
      completionVisible = false;
      return;
    }

    completionMatches = matches;
    completionIndex = 0;
    completionVisible = true;
  }

  function _applyCompletion(): void {
    if (!completionMatches.length) return;
    const chosen = completionMatches[completionIndex] || completionMatches[0];
    draft = chosen.cmd + (chosen.arg ? ' ' : '');
    completionMatches = [];
    completionVisible = false;
  }

  function _handleCompletionKeydown(e: KeyboardEvent): boolean {
    if (!completionVisible || !completionMatches.length) return false;

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        completionIndex = (completionIndex + 1) % completionMatches.length;
        return true;
      case 'ArrowUp':
        e.preventDefault();
        completionIndex = (completionIndex - 1 + completionMatches.length) % completionMatches.length;
        return true;
      case 'Tab':
      case 'Enter':
        e.preventDefault();
        _applyCompletion();
        return true;
      case 'Escape':
        e.preventDefault();
        completionMatches = [];
        completionVisible = false;
        return true;
    }
    return false;
  }

  function _handleSlash(text: string): boolean {
    const parts = text.split(/\s+/);
    const cmd = (parts[0] || '').toLowerCase();
    const args = parts.slice(1).join(' ');

    switch (cmd) {
      case '/help':
        _showHelp();
        return true;
      case '/clear':
        messages = [];
        history = [];
        currentAgentText = '';
        messages = [...messages, { role: 'system', content: 'Chat history cleared.' }];
        return true;
      case '/projects':
        push('#/');
        return true;
      case '/project':
        if (!args) {
          messages = [...messages, { role: 'system', content: 'Usage: /project <project_id> \u2014 e.g. /project my-todo-app' }];
        } else {
          setCurrentProject(args);
          push('#/projects/' + encodeURIComponent(args));
          messages = [...messages, { role: 'system', content: 'Switched to project: ' + args }];
        }
        return true;
      default:
        return false;
    }
  }

  function _showHelp(): void {
    const lines = ['Available commands:', ''];
    for (const c of _SLASH_COMMAND_DEFS) {
      let name = c.cmd + (c.arg ? ' <arg>' : '');
      while (name.length < 16) name += ' ';
      lines.push('  ' + name + '\u2014 ' + c.desc);
    }
    lines.push('');
    lines.push('Any other text will be sent to the Meta Agent.');
    messages = [...messages, { role: 'system', content: lines.join('\n') }];
  }

  // ── Streaming (fetch + ReadableStream) ──

  function _formatToolResult(name: string, result: Record<string, unknown> | null | undefined): string {
    if (!result || typeof result !== 'object') {
      return '\uD83D\uDD27 ' + name + ' done';
    }

    const status = (result.status as string) || '';

    if (name === 'list_projects') {
      const count = (Array.isArray(result.projects) ? result.projects.length : 0);
      return '\uD83D\uDD27 ' + name + ': ' + count + ' project(s)';
    }
    if (name === 'get_project') {
      const p = (result.project as Record<string, unknown>) || {};
      const pname = (p.name as string) || (p.project_id as string) || '';
      return '\uD83D\uDD27 ' + name + ': ' + pname + ' (' + (p.status as string || '?') + ')';
    }
    if (name === 'create_project') {
      return '\uD83D\uDD27 ' + name + ': created \u201C' + (result.project_id as string || '') + '\u201D';
    }
    if (name === 'list_tasks') {
      const count = (Array.isArray(result.tasks) ? result.tasks.length : 0);
      return '\uD83D\uDD27 ' + name + ': ' + count + ' task(s)';
    }
    if (name === 'list_code_tree' || name === 'list_workspace_tree') {
      const count = (Array.isArray(result.tree) ? result.tree.length : 0);
      return '\uD83D\uDD27 ' + name + ': ' + count + ' file(s)';
    }
    if (name === 'read_code_file' || name === 'read_workspace_file') {
      const path = (result.path as string) || '';
      const len = (result.content as string || '').length;
      return '\uD83D\uDD27 ' + name + ': ' + path + ' (' + len + ' chars)';
    }
    if (name === 'detect_intent') {
      return '\uD83D\uDD27 ' + name + ': ' + (result.intent as string || '');
    }

    if (status) {
      return '\uD83D\uDD27 ' + name + ': ' + status;
    }

    return '\uD83D\uDD27 ' + name + ' done';
  }

  function _processEvent(event: Record<string, unknown>): void {
    const etype = event.type as string;

    switch (etype) {
      case 'session': {
        // Server-minted session id (our request carried none — e.g. the
        // /api/me race dropped it). Adopt it so this and later turns keep
        // their history and the butler can resume the conversation's runs.
        const sid = event.session_id as string | undefined;
        if (sid) {
          sessionId = sid;
          sessionInitiated = true;
          _storeSessionId(sid);
        }
        break;
      }

      case 'text_delta': {
        currentAgentText += (event.content as string || '');
        const lastMsg = messages[messages.length - 1];
        if (lastMsg && lastMsg.role === 'agent') {
          messages = [...messages.slice(0, -1), { role: 'agent', content: currentAgentText }];
        } else {
          messages = [...messages, { role: 'agent', content: currentAgentText }];
        }
        break;
      }

      case 'tool_call': {
        // A tool line closes the current agent bubble: without this reset,
        // the next text_delta re-renders the FULL accumulated prose into a
        // fresh bubble below the tool line — duplicating everything the
        // agent said before the call.
        currentAgentText = '';
        const toolName = (event.name as string) || '?';
        messages = [...messages, { role: 'tool', content: '\uD83D\uDD27 Calling ' + toolName + '...' }];
        break;
      }

      case 'tool_result': {
        const toolName = (event.name as string) || '?';
        const result = (event.result as Record<string, unknown>) || {};
        const summary = _formatToolResult(toolName, result);
        messages = [...messages, { role: 'tool', content: summary }];
        // Surface conversation-driving artifacts the model may not narrate:
        // the gather step's question and the finalized brief arrive INSIDE
        // tool results — without this the user can face a silent stall.
        const surfaced = (result.question as string) || (result.brief_markdown as string) || '';
        if (surfaced) {
          currentAgentText = '';
          messages = [...messages, { role: 'agent', content: surfaced }];
        }
        break;
      }

      case 'done': {
        const msg = (event.message as Record<string, unknown>) || {};
        const content = (msg.content as string) || currentAgentText || '';

        if (content) {
          const lastMsg = messages[messages.length - 1];
          if (lastMsg && lastMsg.role === 'agent') {
            messages = [...messages.slice(0, -1), { role: 'agent', content }];
          } else {
            messages = [...messages, { role: 'agent', content }];
          }
          history = [...history, { role: 'assistant', content }];
        }

        currentAgentText = '';
        _loadSessionList();
        break;
      }

      case 'error': {
        messages = [...messages, { role: 'error', content: (event.message as string) || 'Unknown agent error' }];
        break;
      }
    }
  }

  async function _sendMessage(text: string): Promise<void> {
    if (sending || agentStreaming) return;
    if (!text || typeof text !== 'string') return;

    if (!connected) {
      messages = [...messages, { role: 'error', content: 'Cannot send message while disconnected.' }];
      return;
    }

    if (text.charAt(0) === '/') {
      const handled = _handleSlash(text);
      if (handled) return;
    }

    sending = true;
    error = null;

    if (!sessionInitiated) {
      await _initSession();
    }

    // Snapshot history BEFORE appending the new message: the backend
    // appends `message` to the prompt itself, so including it in `history`
    // too made every user turn reach the LLM twice.
    const outHistory = history.map(
      h => ({ role: h.role === 'agent' ? 'assistant' : h.role, content: h.content }));

    messages = [...messages, { role: 'user', content: text }];
    history = [...history, { role: 'user', content: text }];

    const currentProject = projectId || $projectStore.currentProjectId || undefined;

    const body = {
      message: text,
      history: outHistory,
      current_project: currentProject,
      session_id: sessionId || undefined,
    };

    const controller = new AbortController();
    abortController = controller;

    agentStreaming = true;
    currentAgentText = '';

    try {
      const response = await fetch('/api/agent/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error('HTTP ' + response.status + ': ' + response.statusText);
      }

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const result = await reader.read();
        if (result.done) {
          // Finalise if no done event was processed
          if (currentAgentText.trim()) {
            const lastMsg = messages[messages.length - 1];
            if (lastMsg && lastMsg.role === 'agent') {
              messages = [...messages.slice(0, -1), { role: 'agent', content: currentAgentText }];
            }
            history = [...history, { role: 'assistant', content: currentAgentText }];
          }
          break;
        }

        buffer += decoder.decode(result.value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line || line.indexOf('data: ') !== 0) continue;
          const jsonStr = line.slice(6);
          try {
            const event = JSON.parse(jsonStr);
            _processEvent(event);
          } catch {
            // Skip unparseable events
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name === 'AbortError') {
        return;
      }
      messages = [...messages, { role: 'error', content: 'Connection error: ' + ((err as Error).message || 'Failed to reach agent') }];
    } finally {
      sending = false;
      agentStreaming = false;
      abortController = null;
    }
  }

  function _abortStream(): void {
    if (abortController) {
      try {
        abortController.abort();
      } catch {
        // ignore
      }
      abortController = null;
    }
    agentStreaming = false;
    currentAgentText = '';
  }

  // ── Event handlers ──

  function handleSend(): void {
    const text = draft.trim();
    if (!text || !canSend) return;
    draft = '';
    completionMatches = [];
    completionVisible = false;
    _sendMessage(text);
  }

  function handleKeydown(e: KeyboardEvent): void {
    if (_handleCompletionKeydown(e)) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleInput(): void {
    _updateCompletion(draft);
  }

  function handleCompletionClick(index: number): void {
    completionIndex = index;
    _applyCompletion();
  }

  // ── Lifecycle ──

  onMount(async () => {
    loading = true;
    await _initSession();
    await _restoreHistory();
    await _loadSessionList();
    selectedSessionId = sessionId;
    loading = false;
  });

  onDestroy(() => {
    _abortStream();
  });

  // Watch for route param changes
  $effect(() => {
    const pid = params.id;
    if (pid && pid !== $projectStore.currentProjectId) {
      setCurrentProject(pid);
    }
  });

  // Auto-scroll to bottom when messages change
  $effect(() => {
    void messages.length;
    if (messagesContainerEl) {
      requestAnimationFrame(() => {
        if (messagesContainerEl) {
          messagesContainerEl.scrollTop = messagesContainerEl.scrollHeight;
        }
      });
    }
  });
</script>

<section id="view-chat" class="chat-view">
  <!-- Breadcrumb -->
  <nav class="breadcrumb" aria-label="breadcrumb">
    <ul>
      <li><a href="#/" on:click|preventDefault={() => push('#/')}>Dashboard</a></li>
      {#if params.id}
        <li><a href="#/projects/{params.id}" on:click|preventDefault={() => push('#/projects/' + encodeURIComponent(params.id))}>{truncate(params.id, 30)}</a></li>
      {/if}
      <li>Chat</li>
    </ul>
  </nav>

  <!-- Session header -->
  <div class="chat-header">
    <select
      class="session-selector"
      value={selectedSessionId || ''}
      on:change={(e) => {
        const val = (e.target as HTMLSelectElement).value;
        if (val) {
          _switchSession(val);
        }
      }}
    >
      <option value="">Current session</option>
      {#each sessionList as s (s.session_id as string)}
        {@const sid = s.session_id as string}
        {@const pid = (s.project_id as string) || ''}
        {@const titleMsg = (s.first_message as string) || (s.last_message as string) || ''}
        {@const count = (s.message_count as number) || 0}
        {@const preview = titleMsg.length > 40 ? titleMsg.slice(0, 40) + '\u2026' : titleMsg}
        <option value={sid} selected={sid === sessionId}>
          {pid ? pid + ': ' : ''}{preview} ({count} msgs)
        </option>
      {/each}
    </select>
    <button class="outline btn-new-session" on:click={_handleNewSession} disabled={!connected}>
      + New
    </button>
  </div>

  <!-- Loading state -->
  {#if loading}
    <div class="chat-loading">
      <p class="text-muted">Loading chat...</p>
    </div>
  {:else}
    <!-- Messages container -->
    <div class="chat-messages" bind:this={messagesContainerEl}>
      <!-- Welcome message -->
      {#if messages.length === 0}
        <div class="chat-msg chat-system">
          <div class="msg-content">Chat with the Meta Agent. Type /help for commands.</div>
        </div>
      {/if}

      <!-- Message bubbles -->
      {#each messages as msg, i (i)}
        <div class="chat-msg chat-{msg.role}">
          <div class="msg-content">
            {#if msg.role === 'agent'}
              {@const html = renderMarkdown(msg.content)}
              {#if html}
                {@html html}
              {:else}
                {msg.content}
              {/if}
            {:else if msg.role === 'user'}
              {msg.content}
            {:else}
              {msg.content}
            {/if}
          </div>
        </div>
      {/each}

      <!-- Connection lost placeholder -->
      {#if !connected && messages.length > 0}
        <div class="chat-msg chat-system chat-reconnect-placeholder">
          <div class="msg-content">Chat unavailable — reconnecting…</div>
        </div>
      {/if}
    </div>

    <!-- Input area -->
    <div class="chat-input-area">
      {#if completionVisible && completionMatches.length > 0}
        <ul class="slash-completion">
          {#each completionMatches as c, i}
            <li
              class="slash-completion-item"
              class:is-active={i === completionIndex}
              on:mousedown|preventDefault={() => handleCompletionClick(i)}
            >
              <span class="slash-cmd">{c.cmd}</span>
              <span class="slash-desc">{c.desc}</span>
            </li>
          {/each}
        </ul>
      {/if}

      <textarea
        id="chat-input-field"
        class="chat-input"
        bind:value={draft}
        on:input={handleInput}
        on:keydown={handleKeydown}
        placeholder={inputDisabled ? 'Chat unavailable \u2014 reconnecting\u2026' : 'Message the agent... (/ to see commands)'}
        disabled={inputDisabled}
        rows="2"
        autocomplete="off"
      ></textarea>
      <button
        id="chat-send-btn"
        class="chat-send-btn"
        on:click={handleSend}
        disabled={!canSend}
      >
        Send
      </button>

      {#if agentStreaming}
        <span class="streaming-indicator">Streaming\u2026</span>
      {:else if sending}
        <span class="streaming-indicator">Sending\u2026</span>
      {/if}
    </div>
  {/if}

  <!-- Error banner -->
  {#if error}
    <div class="chat-error-banner">
      {error}
      <button class="close-btn" on:click={() => error = null}>&times;</button>
    </div>
  {/if}
</section>

<style>
  .chat-view {
    display: flex;
    flex-direction: column;
    height: 100%;
    position: relative;
  }

  .breadcrumb {
    flex-shrink: 0;
    padding: 0.5rem 0;
  }

  .chat-header {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0;
    width: 100%;
    flex-shrink: 0;
  }

  .session-selector {
    flex: 1;
  }

  .btn-new-session {
    flex-shrink: 0;
    font-size: 0.85rem;
    padding: 0.2rem 0.6rem;
  }

  .chat-loading {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .chat-msg {
    max-width: 80%;
    padding: 0.5rem 0.75rem;
    border-radius: 0.5rem;
    line-height: 1.4;
    word-wrap: break-word;
  }

  .chat-msg .msg-content :global(p) {
    margin: 0.25rem 0;
  }

  .chat-msg .msg-content :global(pre) {
    overflow-x: auto;
    padding: 0.5rem;
    border-radius: 0.25rem;
    font-size: 0.85rem;
  }

  .chat-msg .msg-content :global(code) {
    font-size: 0.85rem;
  }

  .chat-user {
    align-self: flex-end;
    background: var(--pico-primary-background, #0066cc);
    color: var(--pico-primary-inverse, #fff);
  }

  .chat-agent {
    align-self: flex-start;
    background: var(--pico-card-background-color, #f0f0f0);
    color: var(--pico-color, #000);
  }

  .chat-system {
    align-self: center;
    text-align: center;
    font-style: italic;
    opacity: 0.7;
    max-width: 100%;
    font-size: 0.9rem;
  }

  .chat-tool {
    align-self: flex-start;
    font-size: 0.85rem;
    opacity: 0.8;
    font-family: monospace;
    background: var(--pico-card-background-color, #f5f5f5);
    max-width: 100%;
  }

  .chat-error {
    align-self: center;
    background: var(--pico-form-element-invalid-active-border-color, #c62828);
    color: #fff;
    text-align: center;
  }

  .chat-reconnect-placeholder {
    opacity: 0.5;
    font-style: italic;
  }

  .chat-input-area {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.5rem;
    padding: 0.5rem 0;
    flex-shrink: 0;
    position: relative;
  }

  .chat-input {
    flex: 1;
    min-width: 200px;
    resize: none;
  }

  .chat-send-btn {
    flex-shrink: 0;
  }

  .streaming-indicator {
    flex-shrink: 0;
    font-size: 0.8rem;
    opacity: 0.7;
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.7; }
    50% { opacity: 0.3; }
  }

  .chat-error-banner {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    background: var(--pico-form-element-invalid-active-border-color, #c62828);
    color: #fff;
    padding: 0.5rem 1rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-radius: 0.25rem;
    margin-bottom: 0.25rem;
  }

  .close-btn {
    background: none;
    border: none;
    color: inherit;
    font-size: 1.2rem;
    cursor: pointer;
    padding: 0 0.25rem;
  }

  .slash-completion {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    list-style: none;
    margin: 0;
    padding: 0;
    background: var(--pico-card-background-color, #fff);
    border: 1px solid var(--pico-muted-border-color, #ccc);
    border-radius: 0.25rem;
    box-shadow: 0 -2px 8px rgba(0,0,0,0.1);
    z-index: 10;
    max-height: 200px;
    overflow-y: auto;
  }

  .slash-completion-item {
    padding: 0.4rem 0.75rem;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    gap: 1rem;
  }

  .slash-completion-item.is-active {
    background: var(--pico-primary-background, #0066cc);
    color: var(--pico-primary-inverse, #fff);
  }

  .slash-cmd {
    font-weight: 600;
    font-family: monospace;
  }

  .slash-desc {
    opacity: 0.7;
    font-size: 0.85rem;
  }
</style>
