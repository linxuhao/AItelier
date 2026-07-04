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
  import { t } from '../lib/i18n.svelte';

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
    toolName?: string;
    toolArgs?: Record<string, unknown>;
    toolResult?: Record<string, unknown>;
    _argDisplay?: string;
  }

  interface MessageGroup {
    kind: 'message' | 'tool-block';
    message?: ChatMessage;
    messageIndex?: number;
    tools?: ChatMessage[];
    toolStartIndex?: number;
    toolEndIndex?: number;
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

  // Coding mode (user-toggled; sent with each request, persisted per session
  // server-side). budgetPaused surfaces the loop's budget_exhausted pause.
  let codingMode = $state(false);
  let budgetPaused = $state(false);

  // Slash completion state
  let completionMatches = $state<SlashCommand[]>([]);
  let completionIndex = $state(0);
  let completionVisible = $state(false);

  let messagesContainerEl: HTMLDivElement | undefined = $state();

  // ── Derived ──

  let canWrite = $derived($authStore.permissionResolved && $authStore.canWrite);
  let connected = $derived($connectionStore.connectionOk);
  let projectId = $derived(params.id || '');

  let inputDisabled = $derived(sending || agentStreaming);
  let canSend = $derived(!sending && !agentStreaming && draft.trim().length > 0 && connected);
  let inputPlaceholder = $derived(
    !connected ? t('chat.unavailable')
      : agentStreaming ? t('chat.agentResponding')
      : sending ? t('chat.sending')
      : t('chat.placeholder')
  );

  // ── Message groups: consecutive tool messages merged into one collapsible block ──
  let messageGroups = $derived.by(() => {
    const groups: MessageGroup[] = [];
    let i = 0;
    while (i < messages.length) {
      if (messages[i].role === 'tool') {
        const tools: ChatMessage[] = [];
        const start = i;
        while (i < messages.length && messages[i].role === 'tool') {
          tools.push(messages[i]);
          i++;
        }
        groups.push({ kind: 'tool-block', tools, toolStartIndex: start, toolEndIndex: i - 1 });
      } else {
        groups.push({ kind: 'message', message: messages[i], messageIndex: i });
        i++;
      }
    }
    return groups;
  });

  // Tool blocks start collapsed by default. Click toggles membership.
  let expandedToolBlocks = $state(new Set<number>());

  // Token usage from the last turn
  let tokenCount = $state(0);
  let totalTokens = $state(0);
  let tokenLimit = $state(0);
  let tokenMode = $state('butler');
  // Cumulative real API usage (provider-reported): cache hit ratio and
  // billed-equivalent tokens (cache_miss + cache_hit/10 + completion).
  let hitRatio = $state(0);
  let billedTokens = $state(0);

  function _formatTokens(n: number): string {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

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

      // Adopt the session's stored mode so the toggle reflects reality.
      codingMode = (response.mode as string) === 'coding';

      // Restore token usage from API
      if (typeof response.token_count === 'number') {
        tokenCount = response.token_count;
        tokenLimit = (response.token_limit as number) || 0;
        tokenMode = (response.mode as string) || 'butler';
        if (typeof response.total_tokens === 'number') {
          totalTokens = response.total_tokens;
        }
      }
      if (typeof response.hit_ratio === 'number') hitRatio = response.hit_ratio;
      if (typeof response.billed_tokens === 'number') billedTokens = response.billed_tokens;

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
        const msg: ChatMessage = { role: displayRole as ChatMessage['role'], content: m.content as string };

        // Recover tool details from message_json (coding-mode persisted transcripts)
        if (displayRole === 'tool' && m.message_json) {
          try {
            const parsed = JSON.parse(m.message_json as string);
            const tName = parsed.tool_name as string;
            if (tName) {
              msg.toolName = tName;
              const tArgs = parsed.tool_args as Record<string, unknown> | undefined;
              if (tArgs) {
                msg.toolArgs = tArgs;
                msg._argDisplay = _formatToolArgs(tName, tArgs);
              }
              // Parse the result from the persisted content JSON
              if (parsed.content && typeof parsed.content === 'string') {
                try {
                  msg.toolResult = JSON.parse(parsed.content) as Record<string, unknown>;
                } catch { /* not JSON */ }
              }
              // Replace raw-JSON content with a human summary
              msg.content = msg.toolResult
                ? _formatToolResult(tName, msg.toolResult)
                : '🔧 ' + tName;
            }
          } catch { /* not JSON — ignore */ }
        }

        messages = [...messages, msg];
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
    tokenCount = 0;
    totalTokens = 0;
    tokenLimit = 0;
    hitRatio = 0;
    billedTokens = 0;
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
    tokenCount = 0;
    totalTokens = 0;
    tokenLimit = 0;
    hitRatio = 0;
    billedTokens = 0;
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
        e.preventDefault();
        _applyCompletion();
        return true;
      case 'Enter':
        if (e.shiftKey) return false;
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
          messages = [...messages, { role: 'system', content: 'Usage: /project <project_id> — e.g. /project my-todo-app' }];
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
      lines.push('  ' + name + '— ' + c.desc);
    }
    lines.push('');
    lines.push('Any other text will be sent to the Meta Agent.');
    messages = [...messages, { role: 'system', content: lines.join('\n') }];
  }

  // ── Streaming (fetch + ReadableStream) ──

  function _formatToolResult(name: string, result: Record<string, unknown> | null | undefined): string {
    if (!result || typeof result !== 'object') {
      return '🔧 ' + name + ' done';
    }

    const status = (result.status as string) || '';

    if (name === 'list_projects') {
      const count = (Array.isArray(result.projects) ? result.projects.length : 0);
      return '🔧 ' + name + ': ' + count + ' project(s)';
    }
    if (name === 'get_project') {
      const p = (result.project as Record<string, unknown>) || {};
      const pname = (p.name as string) || (p.project_id as string) || '';
      return '🔧 ' + name + ': ' + pname + ' (' + (p.status as string || '?') + ')';
    }
    if (name === 'create_project') {
      return '🔧 ' + name + ': created “' + (result.project_id as string || '') + '”';
    }
    if (name === 'list_tasks') {
      const count = (Array.isArray(result.tasks) ? result.tasks.length : 0);
      return '🔧 ' + name + ': ' + count + ' task(s)';
    }
    if (name === 'list_code_tree' || name === 'list_workspace_tree') {
      const count = (Array.isArray(result.tree) ? result.tree.length : 0);
      return '🔧 ' + name + ': ' + count + ' file(s)';
    }
    if (name === 'read_code_file' || name === 'read_workspace_file') {
      const path = (result.path as string) || '';
      const len = (result.content as string || '').length;
      return '🔧 ' + name + ': ' + path + ' (' + len + ' chars)';
    }
    if (name === 'detect_intent') {
      return '🔧 ' + name + ': ' + (result.intent as string || '');
    }

    if (status) {
      return '🔧 ' + name + ': ' + status;
    }

    return '🔧 ' + name + ' done';
  }

  function _formatToolArgs(name: string, args: Record<string, unknown>): string {
    // Tool-specific key arguments for a compact one-line summary.
    const keyMap: Record<string, string> = {
      bash: 'command',
      read_code_file: 'path',
      edit_file: 'path',
      create_file: 'path',
      search_code: 'pattern',
      list_code_tree: 'subdir',
      read_workspace_file: 'path',
      list_workspace_tree: 'subdir',
      get_project: 'project_id',
      list_tasks: 'project_id',
      get_task: 'task_id',
      retry_task: 'task_id',
      get_step_output: 'task_id',
      delete_file: 'path',
    };
    const key = keyMap[name];
    if (key && typeof args[key] === 'string') {
      let val = args[key] as string;
      if (val.length > 80) val = val.slice(0, 80) + '…';
      return val;
    }
    if (key && args[key] !== undefined) {
      return String(args[key]).slice(0, 80);
    }
    // Generic fallback: first meaningful string arg
    for (const k of ['path', 'pattern', 'command', 'name', 'task_id', 'project_id']) {
      const v = args[k];
      if (typeof v === 'string' && v.length > 0) {
        return v.length > 80 ? v.slice(0, 80) + '…' : v;
      }
      if (v !== undefined && v !== null) {
        return String(v).slice(0, 80);
      }
    }
    return '';
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
        const args = (event.args as Record<string, unknown>) || {};
        const argSummary = _formatToolArgs(toolName, args);
        const label = '🔧 ' + toolName + (argSummary ? ': ' + argSummary : '...');
        messages = [...messages, {
          role: 'tool',
          content: label,
          toolName,
          toolArgs: args,
          _argDisplay: argSummary,
        }];
        break;
      }

      case 'tool_result': {
        const toolName = (event.name as string) || '?';
        const result = (event.result as Record<string, unknown>) || {};
        const summary = _formatToolResult(toolName, result);
        // Attach the result to the matching pending tool_call message so
        // call + result are a single collapsible block.
        let attached = false;
        for (let i = messages.length - 1; i >= 0; i--) {
          const m = messages[i];
          if (m.role === 'tool' && m.toolName === toolName && !m.toolResult) {
            messages = [
              ...messages.slice(0, i),
              { ...m, content: summary, toolResult: result },
              ...messages.slice(i + 1),
            ];
            attached = true;
            break;
          }
        }
        if (!attached) {
          // No matching tool_call (butler-mode tools, or out-of-order
          // events) — create a standalone result message.
          messages = [...messages, {
            role: 'tool',
            content: summary,
            toolName,
            toolResult: result,
          }];
        }
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

      case 'token_usage': {
        const tokens = event.tokens as number | undefined;
        const total = event.total_tokens as number | undefined;
        const limit = event.limit as number | undefined;
        const mode = event.mode as string | undefined;
        const ratio = event.hit_ratio as number | undefined;
        const billed = event.billed_tokens as number | undefined;
        if (tokens !== undefined) tokenCount = tokens;
        if (total !== undefined) totalTokens = total;
        if (limit !== undefined) tokenLimit = limit;
        if (mode !== undefined) tokenMode = mode;
        if (ratio !== undefined) hitRatio = ratio;
        if (billed !== undefined) billedTokens = billed;
        break;
      }

      case 'budget_exhausted':
      case 'llm_interrupted': {
        // Not an error: the coding loop paused — either at its tool-turn budget
        // or on a transient model-connection interruption. The transcript is
        // persisted server-side, so "continue" resumes it.
        budgetPaused = true;
        messages = [...messages, {
          role: 'system',
          content: (event.message as string) || 'Paused. Continue?',
        }];
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
      mode: codingMode ? 'coding' : 'butler',
    };
    budgetPaused = false;

    agentStreaming = true;
    currentAgentText = '';

    let lastError: unknown = null;

    for (let attempt = 0; attempt < 2; attempt++) {
      const controller = new AbortController();
      abortController = controller;

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

        lastError = null; // success
        break;
      } catch (err: unknown) {
        lastError = err;
        if (err instanceof Error && err.name === 'AbortError') {
          break; // user-initiated abort — don't retry
        }
        // Retry once after a short delay for transient network errors
        if (attempt === 0 && connected) {
          messages = [...messages, { role: 'system', content: 'Stream interrupted — retrying…' }];
          await new Promise(r => setTimeout(r, 1500));
          continue;
        }
        break; // second failure, or disconnected — stop
      }
    }

    if (lastError && !((lastError instanceof Error) && (lastError as Error).name === 'AbortError')) {
      messages = [...messages, { role: 'error', content: 'Connection error: ' + ((lastError as Error).message || 'Failed to reach agent') }];
    }

    sending = false;
    agentStreaming = false;
    abortController = null;
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

  function toggleToolBlock(groupIndex: number): void {
    const next = new Set(expandedToolBlocks);
    if (next.has(groupIndex)) {
      next.delete(groupIndex);
    } else {
      next.add(groupIndex);
    }
    expandedToolBlocks = next;
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
    if (e.key === 'Enter') {
      if (e.shiftKey) {
        // Shift+Enter: let browser insert newline — do nothing
        return;
      }
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
      <li><a href="#/" onclick={(e) => { e.preventDefault(); push('#/'); }}>{t('chat.dashboard')}</a></li>
      {#if params.id}
        <li><a href="#/projects/{params.id}" onclick={(e) => { e.preventDefault(); push('#/projects/' + encodeURIComponent(params.id)); }}>{truncate(params.id, 30)}</a></li>
      {/if}
      <li>{t('chat.breadcrumbChat')}</li>
    </ul>
  </nav>

  <!-- Session header -->
  <div class="chat-header">
    <select
      class="session-selector"
      value={selectedSessionId || ''}
      onchange={(e) => {
        const val = (e.target as HTMLSelectElement).value;
        if (val) {
          _switchSession(val);
        }
      }}
    >
      <option value="">{t('chat.currentSession')}</option>
      {#each sessionList as s (s.session_id)}
        {@const sid = s.session_id as string}
        {@const pid = (s.project_id as string) || ''}
        {@const titleMsg = (s.first_message as string) || (s.last_message as string) || ''}
        {@const count = (s.message_count as number) || 0}
        {@const preview = titleMsg.length > 40 ? titleMsg.slice(0, 40) + '…' : titleMsg}
        <option value={sid} selected={sid === sessionId}>
          {pid ? pid + ': ' : ''}{preview} ({count} msgs)
        </option>
      {/each}
    </select>
    <button class="outline btn-new-session" onclick={_handleNewSession} disabled={!connected}>
      {t('chat.newSession')}
    </button>
  </div>

  <!-- Loading state -->
  {#if loading}
    <div class="chat-loading">
      <p class="text-muted">{t('chat.loading')}</p>
    </div>
  {:else}
    <!-- Messages container -->
    <div class="chat-messages" bind:this={messagesContainerEl}>
      <!-- Welcome message -->
      {#if messages.length === 0}
        <div class="chat-msg chat-system">
          <div class="msg-content">{t('chat.welcome')}</div>
        </div>
      {/if}

      <!-- Message bubbles (grouped: consecutive tools become one block) -->
      {#each messageGroups as group, gi (gi)}
        {#if group.kind === 'tool-block' && group.tools}
          {@const isExpanded = expandedToolBlocks.has(gi)}
          {@const toolCount = group.tools.length}
          <div class="chat-msg chat-tool-block" class:tool-expanded={isExpanded}>
            <button
              class="tool-block-header"
              onclick={() => toggleToolBlock(gi)}
              aria-expanded={isExpanded}
            >
              <span class="tool-toggle">{isExpanded ? '▼' : '▶'}</span>
              <span class="tool-summary">🔧 {toolCount} {t('chat.tools')}</span>
              <span class="tool-names">
                {#each group.tools as tool, ti}
                  {#if ti > 0}, {/if}
                  {tool.toolName || '?'}{#if tool._argDisplay}:{tool._argDisplay}{/if}
                {/each}
              </span>
            </button>
            {#if isExpanded}
              <div class="tool-details">
                {#each group.tools as tool}
                  <details class="tool-entry-details">
                    <summary class="tool-entry-header">
                      🔧 {tool.toolName || '?'}{#if tool._argDisplay}: {tool._argDisplay}{/if}
                    </summary>
                    <div class="tool-entry-body">
                      {#if tool.toolArgs && Object.keys(tool.toolArgs).length > 0}
                        <details class="tool-detail-section" open>
                          <summary class="tool-section-label">{t('chat.arguments').replace('{n}', String(Object.keys(tool.toolArgs).length))}</summary>
                          <pre class="tool-pre">{JSON.stringify(tool.toolArgs, null, 2)}</pre>
                        </details>
                      {/if}
                      {#if tool.toolResult}
                        <details class="tool-detail-section" open>
                          <summary class="tool-section-label">{t('chat.result')}</summary>
                          <pre class="tool-pre">{JSON.stringify(tool.toolResult, null, 2)}</pre>
                        </details>
                      {/if}
                    </div>
                  </details>
                {/each}
              </div>
            {/if}
          </div>
        {:else if group.kind === 'message' && group.message}
          {@const msg = group.message}
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
        {/if}
      {/each}

      <!-- Connection lost placeholder -->
      {#if !connected && messages.length > 0}
        <div class="chat-msg chat-system chat-reconnect-placeholder">
          <div class="msg-content">{t('chat.unavailable')}</div>
        </div>
      {/if}
    </div>

    <!-- Input area -->
    <div class="chat-input-area">
      <label class="coding-mode-toggle" title={t('chat.codingModeTitle')}>
        <input type="checkbox" role="switch" bind:checked={codingMode} disabled={agentStreaming || sending} />
        {t('chat.codingMode')}
      </label>
      {#if completionVisible && completionMatches.length > 0}
        <ul class="slash-completion">
          {#each completionMatches as c, i}
            <li
              class="slash-completion-item"
              class:is-active={i === completionIndex}
              onmousedown={(e) => { e.preventDefault(); handleCompletionClick(i); }}
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
        oninput={handleInput}
        onkeydown={handleKeydown}
        placeholder={inputPlaceholder}
        disabled={inputDisabled}
        rows="2"
        autocomplete="off"
      ></textarea>
      <button
        id="chat-send-btn"
        class="chat-send-btn"
        onclick={handleSend}
        disabled={!canSend}
      >
        {t('chat.send')}
      </button>

      {#if agentStreaming}
        <span class="streaming-indicator">{t('chat.streaming')}</span>
        <button class="outline btn-stop" onclick={_abortStream}>{t('chat.stop')}</button>
      {:else if sending}
        <span class="streaming-indicator">{t('chat.sending')}</span>
      {/if}

      {#if budgetPaused && !agentStreaming && !sending}
        <button class="outline btn-continue" onclick={() => _sendMessage('continue')} disabled={!connected}>
          {t('chat.continue')}
        </button>
      {/if}
    </div>
  {/if}

  <!-- Token usage bar -->
  {#if totalTokens > 0 || tokenCount > 0 || billedTokens > 0}
    {@const pct = tokenCount > 0 && tokenLimit > 0 ? Math.min(100, Math.round((tokenCount / tokenLimit) * 100)) : 0}
    <div class="token-bar">
      {#if tokenCount > 0}
        <div class="token-bar-fill" style="width: {pct}%"></div>
      {/if}
      <span class="token-bar-label">
        {#if totalTokens > 0}
          <span class="token-stat">cumulated {_formatTokens(totalTokens)}</span>
        {/if}
        {#if tokenCount > 0}
          {#if totalTokens > 0}
            <span class="token-sep">·</span>
          {/if}
          <span class="token-stat">window {_formatTokens(tokenCount)}{tokenLimit > 0 ? ' / ' + _formatTokens(tokenLimit) : ''}</span>
          {#if pct > 0}
            <span class="token-sep">·</span>
            <span class="token-stat">{pct}%</span>
          {/if}
        {/if}
        {#if billedTokens > 0}
          <span class="token-sep">·</span>
          <span class="token-stat">cache {Math.round(hitRatio * 100)}%</span>
          <span class="token-sep">·</span>
          <span class="token-stat">billed {_formatTokens(billedTokens)}</span>
        {/if}
        {#if tokenMode === 'coding'}
          <span class="token-sep">·</span>
          <span class="token-stat">coding</span>
        {/if}
      </span>
    </div>
  {/if}

  <!-- Error banner -->
  {#if error}
    <div class="chat-error-banner">
      {error}
      <button class="close-btn" onclick={() => error = null}>&times;</button>
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

  .coding-mode-toggle {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.85rem;
    margin-bottom: 0;
    white-space: nowrap;
    cursor: pointer;
  }

  .btn-continue {
    flex-shrink: 0;
    font-size: 0.85rem;
    padding: 0.2rem 0.6rem;
  }

  .btn-stop {
    flex-shrink: 0;
    font-size: 0.85rem;
    padding: 0.2rem 0.6rem;
    background: var(--pico-form-element-invalid-active-border-color, #c62828);
    border-color: var(--pico-form-element-invalid-active-border-color, #c62828);
    color: #fff;
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

  .chat-tool-block {
    align-self: flex-start;
    font-size: 0.85rem;
    font-family: monospace;
    max-width: 100%;
    border-radius: 0.35rem;
    background: var(--pico-card-background-color, #f5f5f5);
    border: 1px solid var(--pico-muted-border-color, #ddd);
  }

  .chat-tool-block .tool-block-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    padding: 0.35rem 0.5rem;
    border: none;
    background: none;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
    color: inherit;
    text-align: left;
    opacity: 0.85;
  }

  .chat-tool-block .tool-block-header:hover {
    opacity: 1;
    background: var(--pico-muted-border-color, #00000010);
    border-radius: 0.35rem;
  }

  .chat-tool-block .tool-toggle {
    flex-shrink: 0;
    font-size: 0.7rem;
    width: 1em;
  }

  .chat-tool-block .tool-summary {
    flex-shrink: 0;
    font-weight: 600;
  }

  .chat-tool-block .tool-names {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    opacity: 0.6;
    font-size: 0.8rem;
  }

  .chat-tool-block .tool-details {
    border-top: 1px solid var(--pico-muted-border-color, #ddd);
    display: flex;
    flex-direction: column;
    padding: 0.4rem 0.5rem;
  }

  /* Level 2: each tool is a collapsible <details> */
  .chat-tool-block .tool-entry-details {
    border-bottom: 1px dashed var(--pico-muted-border-color, #ddd);
    padding: 0.25rem 0;
  }
  .chat-tool-block .tool-entry-details:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }

  .chat-tool-block .tool-entry-details > summary {
    font-weight: 600;
    cursor: pointer;
    user-select: none;
    padding: 0.15rem 0;
    list-style: none;
    font-size: 0.8rem;
  }
  .chat-tool-block .tool-entry-details > summary::-webkit-details-marker {
    display: none;
  }
  .chat-tool-block .tool-entry-details > summary::before {
    content: '▶ ';
    font-size: 0.6rem;
    display: inline-block;
    width: 1.1em;
    transition: transform 0.15s;
  }
  .chat-tool-block .tool-entry-details[open] > summary::before {
    content: '▼ ';
  }

  /* Level 3: args / result inside each tool */
  .chat-tool-block .tool-entry-body {
    margin-left: 0.8rem;
    padding: 0.15rem 0;
  }

  .chat-tool-block .tool-detail-section {
    margin: 0.2rem 0;
  }

  .chat-tool-block .tool-detail-section summary {
    font-size: 0.75rem;
    font-weight: 600;
    opacity: 0.7;
    cursor: pointer;
    user-select: none;
  }

  .chat-tool-block .tool-detail-section summary:hover {
    opacity: 1;
  }

  .chat-tool-block .tool-pre {
    margin: 0;
    padding: 0.35rem 0.5rem;
    font-size: 0.78rem;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 20em;
    overflow-y: auto;
    background: var(--pico-background-color, #fff);
    border-radius: 0.2rem;
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

  .token-bar {
    position: relative;
    width: 100%;
    height: 1.1rem;
    background: var(--pico-muted-border-color, #e0e0e0);
    border-radius: 0.2rem;
    overflow: hidden;
    flex-shrink: 0;
  }

  .token-bar-fill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    background: var(--pico-primary-background, #0066cc);
    opacity: 0.2;
    border-radius: 0.2rem;
    transition: width 0.3s ease;
  }

  .token-bar-label {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    font-size: 0.7rem;
    font-family: monospace;
    opacity: 0.7;
    white-space: nowrap;
  }

  .token-bar-label .token-sep {
    opacity: 0.5;
  }

  .token-bar-label .token-stat {
    white-space: nowrap;
  }
</style>
