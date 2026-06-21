"use strict";

(function () {
  /**
   * AItelier.Chat — Meta Agent conversation view with SSE streaming,
   * Markdown rendering, tool call indicators, slash commands, and
   * session management.
   *
   * Enhanced with:
   *   - History restoration (rehydrate messages on re-entry)
   *   - Session selector dropdown to switch between past conversations
   *   - Immediate user message save (fire-and-forget POST)
   *
   * DOM target: #view-chat
   * Dependencies: AItelier.API, AItelier.Router, AItelier.Utils, AItelier.App (optional)
   *
   * Uses fetch() + ReadableStream (NOT EventSource) for the chat SSE
   * endpoint so the stream can be aborted when the user navigates away.
   *
   * Usage:
   *   AItelier.Chat.show();
   *   AItelier.Chat.hide();
   *   AItelier.Chat.sendMessage("hello");
   */

  // ── Private state ──────────────────────────────────────────────────

  /** @type {Array<{role: string, content: string}>} */
  var _history = [];

  /** @type {string|null} session ID from API.createSession() */
  var _sessionId = null;

  /** @type {boolean} true while the agent is streaming a response */
  var _agentStreaming = false;

  /** @type {AbortController|null} used to abort in-flight fetch on hide() */
  var _abortController = null;

  /** @type {HTMLElement|null} the current agent message bubble being filled */
  var _currentAgentBubble = null;

  /** @type {string} accumulated text for the current agent message */
  var _currentAgentText = "";

  /** @type {boolean} true once createSession has been called */
  var _sessionInitiated = false;

  /** @type {boolean} true while a sendMessage is in-flight */
  var _sending = false;


  // ── Lazy-access helpers ───────────────────────────────────────────

  /**
   * Get the current project ID from App state if available.
   * @returns {string|null}
   */
  function _getCurrentProject() {
    try {
      var app = window.AItelier && window.AItelier.App;
      return app ? (app.state.currentProjectId || null) : null;
    } catch (_e) {
      return null;
    }
  }

  /**
   * Check if the App layer reports a connection issue.
   * @returns {boolean}
   */
  function _isConnectionOk() {
    try {
      var app = window.AItelier && window.AItelier.App;
      return app ? !!app.state.connectionOk : true;
    } catch (_e) {
      return true;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Initialisation / Session management
  // ════════════════════════════════════════════════════════════════════

  /**
   * Create a new chat session via API.createSession().
   * Called once on first show().  Stores the returned session_id.
   *
   * @returns {Promise<string|null>} session_id or null on failure
   */
  function _initSession() {
    if (_sessionInitiated && _sessionId) {
      return Promise.resolve(_sessionId);
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.createSession !== "function") {
      _sessionInitiated = true;
      return Promise.resolve(null);
    }

    return api.createSession().then(function (data) {
      _sessionId = (data && data.session_id) || null;
      _sessionInitiated = true;
      return _sessionId;
    }).catch(function (/* err */) {
      _sessionInitiated = true;
      return null;
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  DOM rendering helpers
  // ════════════════════════════════════════════════════════════════════

  /**
   * Get or create the chat messages container element within #view-chat.
   * @returns {HTMLElement|null}
   */
  function _getMessagesContainer() {
    var chatView = document.getElementById("view-chat");
    if (!chatView) {
      return null;
    }

    var container = chatView.querySelector(".chat-messages");
    if (!container) {
      container = document.createElement("div");
      container.className = "chat-messages";
      chatView.insertBefore(container, chatView.firstChild);
    }
    return container;
  }

  /**
   * Build the chat input area (input field + send button).
   * Returns the input field element for focus management.
   *
   * @returns {HTMLInputElement|null}
   */
  function _buildInputArea() {
    var chatView = document.getElementById("view-chat");
    if (!chatView) {
      return null;
    }

    // Remove any existing input area
    var existing = chatView.querySelector(".chat-input-area");
    if (existing) {
      existing.parentElement.removeChild(existing);
    }

    var inputArea = document.createElement("div");
    inputArea.className = "chat-input-area";

    var input = document.createElement("input");
    input.type = "text";
    input.id = "chat-input-field";
    input.placeholder = "Message the agent... (/ to see commands)";
    input.autocomplete = "off";
    inputArea.appendChild(input);

    // Slash-command completion dropdown (hidden until "/" is typed). Anchored
    // above the input via CSS (.slash-completion { bottom: 100% }).
    var completion = document.createElement("ul");
    completion.id = "chat-slash-completion";
    completion.className = "slash-completion";
    completion.style.display = "none";
    inputArea.appendChild(completion);

    var sendBtn = document.createElement("button");
    sendBtn.id = "chat-send-btn";
    sendBtn.textContent = "Send";
    sendBtn.addEventListener("click", function () {
      var text = input.value.trim();
      if (text) {
        input.value = "";
        _hideCompletion(completion);
        _sendMessage(text);
      }
    });
    inputArea.appendChild(sendBtn);

    // Recompute completion candidates as the user types.
    input.addEventListener("input", function () {
      _updateCompletion(input, completion);
    });

    input.addEventListener("keydown", function (e) {
      var open = completion.style.display !== "none" && _completionMatches.length > 0;

      // ── Completion navigation (only when the dropdown is open) ──
      if (open) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          _completionIndex = (_completionIndex + 1) % _completionMatches.length;
          _renderCompletion(completion);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          _completionIndex = (_completionIndex - 1 + _completionMatches.length) % _completionMatches.length;
          _renderCompletion(completion);
          return;
        }
        if (e.key === "Tab") {
          e.preventDefault();
          _applyCompletion(input, completion);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          _hideCompletion(completion);
          return;
        }
        if (e.key === "Enter") {
          // Enter accepts the highlighted command instead of submitting.
          e.preventDefault();
          _applyCompletion(input, completion);
          return;
        }
      }

      // ── Default: Enter submits (without Shift) ──
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        var text = input.value.trim();
        if (text) {
          input.value = "";
          _hideCompletion(completion);
          _sendMessage(text);
        }
      }
    });

    chatView.appendChild(inputArea);

    return input;
  }

  // ════════════════════════════════════════════════════════════════════
  //  Slash command autocompletion
  // ════════════════════════════════════════════════════════════════════

  /**
   * Registry of slash commands the web chat understands. Mirrors the CLI's
   * completion list (cli/tui/chat.py) but limited to commands the web actually
   * handles in _handleSlash(), so the dropdown never offers a dead command.
   * @type {Array<{cmd: string, desc: string, arg: boolean}>}
   */
  var _SLASH_COMMAND_DEFS = [
    { cmd: "/help", desc: "Show available commands", arg: false },
    { cmd: "/clear", desc: "Clear the chat history and visible messages", arg: false },
    { cmd: "/projects", desc: "Go to the project dashboard", arg: false },
    { cmd: "/project", desc: "Open a project by id (e.g. /project my-app)", arg: true },
  ];

  /** @type {Array<{cmd, desc, arg}>} current filtered completion candidates. */
  var _completionMatches = [];

  /** @type {number} index of the highlighted candidate. */
  var _completionIndex = 0;

  /** Hide and clear the completion dropdown. */
  function _hideCompletion(box) {
    if (box) { box.style.display = "none"; }
    _completionMatches = [];
    _completionIndex = 0;
  }

  /**
   * Recompute completion candidates from the input value and show/hide the box.
   *
   * @param {HTMLInputElement} input
   * @param {HTMLElement} box — the <ul> dropdown
   */
  function _updateCompletion(input, box) {
    var text = input.value;

    // Only complete a single leading "/word" token (no spaces yet).
    if (text.charAt(0) !== "/" || /\s/.test(text)) {
      _hideCompletion(box);
      return;
    }

    var partial = text.toLowerCase();
    _completionMatches = _SLASH_COMMAND_DEFS.filter(function (c) {
      return c.cmd.indexOf(partial) === 0;
    });

    // Nothing to suggest, or already fully typed — hide.
    if (_completionMatches.length === 0 ||
        (_completionMatches.length === 1 && _completionMatches[0].cmd === partial)) {
      _hideCompletion(box);
      return;
    }

    _completionIndex = 0;
    _renderCompletion(box);
  }

  /** Render the current candidates into the dropdown. */
  function _renderCompletion(box) {
    if (!box) { return; }
    box.innerHTML = "";
    _completionMatches.forEach(function (c, i) {
      var li = document.createElement("li");
      li.className = "slash-completion-item" + (i === _completionIndex ? " is-active" : "");

      var cmdSpan = document.createElement("span");
      cmdSpan.className = "slash-cmd";
      cmdSpan.textContent = c.cmd;
      li.appendChild(cmdSpan);

      var descSpan = document.createElement("span");
      descSpan.className = "slash-desc";
      descSpan.textContent = c.desc;
      li.appendChild(descSpan);

      li.addEventListener("mousedown", function (e) {
        // mousedown (not click) so the input doesn't blur first.
        e.preventDefault();
        _completionIndex = i;
        var input = document.getElementById("chat-input-field");
        _applyCompletion(input, box);
        if (input) { input.focus(); }
      });

      box.appendChild(li);
    });
    box.style.display = "block";
  }

  /**
   * Apply the highlighted candidate to the input. Commands that take an
   * argument get a trailing space and stay in the box; argument-less commands
   * are completed exactly.
   *
   * @param {HTMLInputElement} input
   * @param {HTMLElement} box
   */
  function _applyCompletion(input, box) {
    if (!input || !_completionMatches.length) { return; }
    var chosen = _completionMatches[_completionIndex] || _completionMatches[0];
    input.value = chosen.cmd + (chosen.arg ? " " : "");
    _hideCompletion(box);
    // Re-open if the command takes an argument? No — once a space is typed the
    // single-token filter hides it anyway. Just leave the cursor at the end.
    input.focus();
  }

  /**
   * Build a placeholder shown when the connection is down.
   */
  function _buildConnectionPlaceholder() {
    var container = _getMessagesContainer();
    if (!container) {
      return;
    }

    // Remove any existing placeholder
    var existing = container.querySelector(".chat-reconnect-placeholder");
    if (existing) {
      existing.parentElement.removeChild(existing);
    }

    var placeholder = document.createElement("div");
    placeholder.className = "chat-msg chat-system chat-reconnect-placeholder";

    var content = document.createElement("div");
    content.className = "msg-content";
    content.textContent = "Chat unavailable \u2014 reconnecting\u2026";
    placeholder.appendChild(content);

    container.appendChild(placeholder);
    _scrollToBottom();
  }

  /**
   * Remove the connection placeholder from the messages area.
   */
  function _removeConnectionPlaceholder() {
    var container = _getMessagesContainer();
    if (!container) {
      return;
    }

    var placeholder = container.querySelector(".chat-reconnect-placeholder");
    if (placeholder) {
      placeholder.parentElement.removeChild(placeholder);
    }
  }

  /**
   * Enable or disable the chat input and send button.
   *
   * @param {boolean} disabled
   */
  function _setInputDisabled(disabled) {
    var input = document.getElementById("chat-input-field");
    var btn = document.getElementById("chat-send-btn");

    if (input) {
      input.disabled = disabled;
      if (disabled) {
        input.placeholder = "Chat unavailable \u2014 reconnecting\u2026";
      } else {
        input.placeholder = "Message the agent... (/ to see commands)";
      }
    }

    if (btn) {
      btn.disabled = disabled;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Message rendering
  // ════════════════════════════════════════════════════════════════════

  /**
   * Clone the #tpl-chat-msg template and append a message bubble to the
   * messages container.  Sets CSS classes based on role.
   *
   * @param {string} role — "user", "agent", "system", "tool", "error"
   * @param {string} text — message content
   * @returns {HTMLElement|null} the cloned message element's .msg-content child
   */
  function _addMessage(role, text) {
    var container = _getMessagesContainer();
    if (!container) {
      return null;
    }

    var template = document.getElementById("tpl-chat-msg");
    if (!template) {
      return null;
    }

    var msgEl = template.content.cloneNode(true).firstElementChild;
    if (!msgEl) {
      return null;
    }

    // Set role-specific CSS class
    msgEl.className = "chat-msg";
    if (role === "user") {
      msgEl.classList.add("chat-user");
    } else if (role === "agent") {
      msgEl.classList.add("chat-agent");
    } else if (role === "error") {
      msgEl.classList.add("chat-error");
    } else if (role === "tool") {
      msgEl.classList.add("chat-tool");
    } else {
      msgEl.classList.add("chat-system");
    }

    // Set content
    var contentEl = msgEl.querySelector(".msg-content");
    if (!contentEl) {
      return null;
    }

    if (role === "agent") {
      // Agent messages: render Markdown safely
      contentEl.innerHTML = _renderMarkdown(text);
    } else {
      // Other messages: textContent to avoid XSS
      contentEl.textContent = text;
    }

    container.appendChild(msgEl);
    _scrollToBottom();

    return contentEl;
  }

  /**
   * Render Markdown text safely.
   * Uses Utils.renderMarkdown() if available, falls back to textContent.
   *
   * @param {string} text — Markdown input
   * @returns {string} safe HTML string
   */
  function _renderMarkdown(text) {
    try {
      var utils = window.AItelier && window.AItelier.Utils;
      if (utils && typeof utils.renderMarkdown === "function") {
        return utils.renderMarkdown(text);
      }
    } catch (_e) {
      // fallthrough
    }
    // Fallback: escape HTML
    return _escapeHtml(text);
  }

  /**
   * Escape HTML special characters.
   *
   * @param {string} text
   * @returns {string}
   */
  function _escapeHtml(text) {
    try {
      var utils = window.AItelier && window.AItelier.Utils;
      if (utils && typeof utils.escapeHtml === "function") {
        return utils.escapeHtml(text);
      }
    } catch (_e) {
      // fallthrough
    }
    var entityMap = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return String(text).replace(/[&<>"']/g, function (m) {
      return entityMap[m];
    });
  }

  /**
   * Append a tool call indicator (dim text) to the messages container.
   *
   * @param {string} text — e.g. "\uD83D\uDD27 Calling list_projects..."
   */
  function _addTool(text) {
    _addMessage("tool", text);
  }

  /**
   * Append a system message to the messages container.
   *
   * @param {string} text — system message
   */
  function _addSystem(text) {
    _addMessage("system", text);
  }

  /**
   * Append an error message bubble.
   *
   * @param {string} text — error description
   */
  function _addError(text) {
    _addMessage("error", text);
  }

  /**
   * Scroll the message container to the bottom.
   */
  function _scrollToBottom() {
    var container = _getMessagesContainer();
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Slash command handling
  // ════════════════════════════════════════════════════════════════════

  /**
   * Handle a slash command typed in the input.
   *
   * @param {string} text — the full input text starting with "/"
   * @returns {boolean} true if the command was handled (no agent call needed)
   */
  function _handleSlash(text) {
    var parts = text.split(/\s+/);
    var cmd = (parts[0] || "").toLowerCase();
    var args = parts.slice(1).join(" ");

    switch (cmd) {
      case "/help":
        _showHelp();
        return true;

      case "/clear":
        _clearChat();
        return true;

      case "/projects":
        _navigateToDashboard();
        return true;

      case "/project":
        _handleProjectCommand(args);
        return true;

      default:
        // Unknown command — let the agent handle it as a regular message
        return false;
    }
  }

  /**
   * Show available commands as a system message.
   */
  function _showHelp() {
    var helpLines = ["Available commands:", ""];
    _SLASH_COMMAND_DEFS.forEach(function (c) {
      var name = c.cmd + (c.arg ? " <arg>" : "");
      // pad to align descriptions
      while (name.length < 16) { name += " "; }
      helpLines.push("  " + name + "\u2014 " + c.desc);
    });
    helpLines.push("");
    helpLines.push("Any other text will be sent to the Meta Agent.");
    _addSystem(helpLines.join("\n"));
  }

  /**
   * Clear the chat history array and all visible messages.
   */
  function _clearChat() {
    _history = [];
    _currentAgentBubble = null;
    _currentAgentText = "";

    var container = _getMessagesContainer();
    if (container) {
      container.innerHTML = "";
    }

    _addSystem("Chat history cleared.");
  }

  /**
   * Navigate to the project dashboard (#/).
   */
  function _navigateToDashboard() {
    try {
      var router = window.AItelier && window.AItelier.Router;
      if (router && typeof router.navigate === "function") {
        router.navigate("#/");
      }
    } catch (_e) {
      window.location.hash = "#/";
    }
  }

  /**
   * Handle /project command: set current project and navigate.
   *
   * @param {string} arg — project ID or empty
   */
  function _handleProjectCommand(arg) {
    if (!arg) {
      _addSystem("Usage: /project <project_id> \u2014 e.g. /project my-todo-app");
      return;
    }

    // Update App state
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app) {
        app.state.currentProjectId = arg;
      }
    } catch (_e) {
      // fallthrough
    }

    // Navigate to project detail
    try {
      var router = window.AItelier && window.AItelier.Router;
      if (router && typeof router.navigate === "function") {
        router.navigate("#/projects/" + encodeURIComponent(arg));
      }
    } catch (_e) {
      window.location.hash = "#/projects/" + encodeURIComponent(arg);
    }

    _addSystem("Switched to project: " + arg);
  }


  // ════════════════════════════════════════════════════════════════════
  //  History Restoration
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch persisted messages for the current session from the backend
   * and render non-duplicate bubbles.
   *
   * Called from show() after _initSession() resolves.
   * Must not clear existing _history — only append non-duplicate messages.
   *
   * @returns {Promise<void>}
   */
  function _restoreHistory() {
    // No session to restore
    if (!_sessionId) {
      return Promise.resolve();
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getChatHistory !== "function") {
      return Promise.resolve();
    }

    return api.getChatHistory(_sessionId).then(function (response) {
      if (!response || !response.messages || !Array.isArray(response.messages)) {
        return;
      }

      // Build dedup key set from existing _history
      var existingKeys = {};
      for (var i = 0; i < _history.length; i++) {
        var msg = _history[i];
        var key = msg.role + "|" + (msg.content || "").slice(0, 100);
        existingKeys[key] = true;
      }

      var added = false;
      for (var j = 0; j < response.messages.length; j++) {
        var m = response.messages[j];
        var dedupKey = m.role + "|" + (m.content || "").slice(0, 100);

        // Skip duplicates
        if (existingKeys[dedupKey]) {
          continue;
        }

        // Render the bubble
        var displayRole = m.role;
        if (displayRole === "assistant") {
          displayRole = "agent";
        }
        _addMessage(displayRole, m.content);
        _history.push({ role: m.role, content: m.content });
        existingKeys[dedupKey] = true;
        added = true;
      }

      if (added) {
        _scrollToBottom();
      }
    }).catch(function (/* err */) {
      // Silently skip — chat still works without history
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  Session Selector
  // ════════════════════════════════════════════════════════════════════

  /**
   * Build and insert the session selector header at the top of #view-chat,
   * above the .chat-messages container.
   *
   * Creates:
   *   <div class="chat-header">
   *     <select id="session-selector">...</select>
   *     <button id="btn-new-session">+ New</button>
   *   </div>
   */
  function _buildSessionSelector() {
    var chatView = document.getElementById("view-chat");
    if (!chatView) {
      return;
    }

    // Remove any existing chat-header
    var existing = chatView.querySelector(".chat-header");
    if (existing) {
      existing.parentElement.removeChild(existing);
    }

    // Find .chat-messages to insert before it
    var messagesContainer = chatView.querySelector(".chat-messages");

    var header = document.createElement("div");
    header.className = "chat-header";
    header.style.display = "inline-flex";
    header.style.alignItems = "center";
    header.style.gap = "0.5rem";
    header.style.padding = "0.5rem 0";
    header.style.width = "100%";

    // ── <select id="session-selector"> ──
    var select = document.createElement("select");
    select.id = "session-selector";
    select.style.flex = "1";

    var defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "Current session";
    select.appendChild(defaultOption);

    // ── <button id="btn-new-session">+ New</button> ──
    var newBtn = document.createElement("button");
    newBtn.id = "btn-new-session";
    newBtn.textContent = "+ New";
    newBtn.className = "outline";
    newBtn.style.flexShrink = "0";
    newBtn.style.fontSize = "0.85rem";
    newBtn.style.padding = "0.2rem 0.6rem";

    // ── Change event: load selected session ──
    select.addEventListener("change", function () {
      var selectedVal = select.value;
      if (selectedVal && selectedVal !== _sessionId) {
        _loadSession(selectedVal);
      }
      // Reset the select to show the current session visually
      // (the _loadSession call will update it to the correct value)
    });

    // ── Click event: create new session ──
    newBtn.addEventListener("click", function () {
      // Reset session state
      _sessionId = null;
      _sessionInitiated = false;

      // Clear messages container and history
      var container = _getMessagesContainer();
      if (container) {
        container.innerHTML = "";
      }
      _history = [];

      // Create a new session
      _initSession().then(function () {
        // Refresh the session selector list
        _loadSessionList();
        // Reset the select to default
        var sel = document.getElementById("session-selector");
        if (sel) {
          sel.value = "";
        }
      });
    });

    header.appendChild(select);
    header.appendChild(newBtn);

    // Insert BEFORE .chat-messages
    if (messagesContainer) {
      chatView.insertBefore(header, messagesContainer);
    } else {
      chatView.appendChild(header);
    }
  }

  /**
   * Fetch the session list from the backend and populate the
   * #session-selector dropdown.
   *
   * Called from show() after _restoreHistory() completes.
   *
   * @returns {Promise<void>}
   */
  function _loadSessionList() {
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.listSessions !== "function") {
      return Promise.resolve();
    }

    var currentProject = _getCurrentProject();

    return api.listSessions(currentProject).then(function (response) {
      if (!response || !response.sessions || !Array.isArray(response.sessions)) {
        return;
      }

      var select = document.getElementById("session-selector");
      if (!select) {
        return;
      }

      // Remember the current value before clearing
      var currentVal = select.value;

      // Clear all options except the first "Current session" option
      while (select.options.length > 1) {
        select.remove(1);
      }

      // Add each session as an option
      for (var i = 0; i < response.sessions.length; i++) {
        var session = response.sessions[i];
        var sid = session.session_id || "";
        var pid = session.project_id || "";
        var lastMsg = session.last_message || "";
        var count = session.message_count || 0;

        // Truncate last message to 40 chars
        var preview = lastMsg.length > 40 ? lastMsg.slice(0, 40) + "\u2026" : lastMsg;

        var label = pid + ": " + preview + " (" + count + " msgs)";

        var option = document.createElement("option");
        option.value = sid;
        option.textContent = label;

        // Mark current session as selected
        if (sid === _sessionId) {
          option.selected = true;
        }

        select.appendChild(option);
      }
    }).catch(function (/* err */) {
      // Silently skip — dropdown stays with just the default option
    });
  }

  /**
   * Switch the chat view to display a different session's messages.
   *
   * @param {string} sessionId — the session ID to load
   */
  function _loadSession(sessionId) {
    if (sessionId === _sessionId) {
      return; // Already loaded
    }

    // Abort any in-flight SSE stream
    _abortStream();

    // Clear the messages container
    var container = _getMessagesContainer();
    if (container) {
      container.innerHTML = "";
    }

    // Reset in-memory history
    _history = [];

    // Set the session ID (do NOT call _initSession() — session already exists)
    _sessionId = sessionId;

    // Update the select element's value
    var select = document.getElementById("session-selector");
    if (select) {
      select.value = sessionId;
    }

    // Restore history for the new session
    _restoreHistory();
  }


  // ════════════════════════════════════════════════════════════════════
  //  User Message Persistence
  // ════════════════════════════════════════════════════════════════════

  /**
   * Save a user message to the backend immediately (fire-and-forget).
   *
   * Called from _sendMessage() after _addMessage("user", text) and
   * BEFORE the _history.push() call.
   *
   * @param {string} text — the user message text
   */
  function _saveUserMessage(text) {
    // No session — nothing to save to
    if (!_sessionId) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.saveChatMessage !== "function") {
      return;
    }

    var currentProject = _getCurrentProject();

    // Fire-and-forget: call the API but do NOT await or chain
    api.saveChatMessage({
      session_id: _sessionId,
      project_id: currentProject || "",
      role: "user",
      content: text,
    }).catch(function (/* err */) {
      // Silently ignore — best-effort save
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  SSE streaming (fetch + ReadableStream)
  // ════════════════════════════════════════════════════════════════════

  /**
   * Send a user message and stream the agent's response via SSE.
   * POST to /api/agent/chat, parse the ReadableStream line by line.
   *
   * @param {string} text — the user's message
   */
  function _sendMessage(text) {
    if (_sending || _agentStreaming) {
      return;
    }

    if (!text || typeof text !== "string") {
      return;
    }

    // Check connection state
    if (!_isConnectionOk()) {
      _addError("Cannot send message while disconnected.");
      return;
    }

    // Handle slash commands
    if (text.charAt(0) === "/") {
      var handled = _handleSlash(text);
      if (handled) {
        return;
      }
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api) {
      _addError("API client not available.");
      return;
    }

    _sending = true;

    // Ensure session is initialised before sending
    var sessionPromise = _sessionInitiated
      ? Promise.resolve(_sessionId)
      : _initSession();

    sessionPromise.then(function (sid) {
      _sessionId = sid;

      // Add user message to display
      _addMessage("user", text);

      // Save user message immediately (fire-and-forget) BEFORE history push
      _saveUserMessage(text);

      // Push to history
      _history.push({ role: "user", content: text });

      // Prepare the request
      var currentProject = _getCurrentProject();

      var body = {
        message: text,
        history: _history,
        current_project: currentProject,
        session_id: _sessionId || undefined,
      };

      // Create AbortController for this request
      var controller = new AbortController();
      _abortController = controller;

      var url = window.location.origin + "/api/agent/chat";

      _agentStreaming = true;
      _currentAgentBubble = null;
      _currentAgentText = "";

      // ── Start the fetch with ReadableStream ──
      fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "text/event-stream",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status + ": " + response.statusText);
        }

        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";

        function readChunk() {
          reader.read().then(function (result) {
            if (result.done) {
              // Finalise the current agent message
              _finaliseAgentMessage();
              _sending = false;
              _agentStreaming = false;
              _abortController = null;
              return;
            }

            buffer += decoder.decode(result.value, { stream: true });

            // Parse line by line: each line is "data: {...}\n"
            var lines = buffer.split("\n");
            // Keep the last incomplete line in the buffer
            buffer = lines.pop() || "";

            for (var i = 0; i < lines.length; i++) {
              var line = lines[i];
              if (!line || line.indexOf("data: ") !== 0) {
                continue;
              }

              var jsonStr = line.slice(6); // Remove "data: " prefix
              try {
                var event = JSON.parse(jsonStr);
                _processEvent(event);
              } catch (_e) {
                // Skip unparseable events
              }
            }

            // Read next chunk
            readChunk();
          }).catch(function (err) {
            // AbortError is expected when navigating away
            if (err.name === "AbortError") {
              _sending = false;
              _agentStreaming = false;
              _abortController = null;
              return;
            }
            // Other read errors
            _addError("Stream error: " + (err.message || "Unknown error"));
            _sending = false;
            _agentStreaming = false;
            _abortController = null;
          });
        }

        readChunk();
      }).catch(function (err) {
        // AbortError is expected when navigating away
        if (err.name === "AbortError") {
          _sending = false;
          _agentStreaming = false;
          _abortController = null;
          return;
        }

        _addError("Connection error: " + (err.message || "Failed to reach agent"));
        _sending = false;
        _agentStreaming = false;
        _abortController = null;
      });
    }).catch(function (err) {
      _addError("Failed to initialise session: " + (err.message || "Unknown error"));
      _sending = false;
    });
  }

  /**
   * Process a single SSE event from the agent stream.
   *
   * @param {object} event — parsed event object with at least {type, ...}
   */
  function _processEvent(event) {
    var etype = event && event.type;

    switch (etype) {

      case "text_delta":
        // Append to the current agent message bubble
        if (_currentAgentBubble === null) {
          // First delta — create the bubble
          var contentEl = _addMessage("agent", "");
          _currentAgentBubble = contentEl;
          _currentAgentText = "";
        }
        _currentAgentText += (event.content || "");
        _currentAgentBubble.innerHTML = _renderMarkdown(_currentAgentText);
        _scrollToBottom();
        break;

      case "tool_call":
        // Insert a tool call indicator before the agent message text
        var toolName = event.name || "?";
        _addTool("\uD83D\uDD27 Calling " + toolName + "...");
        _scrollToBottom();
        break;

      case "tool_result":
        // Replace the indicator with a result summary
        var toolName = event.name || "?";
        var result = event.result || {};
        var summary = _formatToolResult(toolName, result);
        _addTool(summary);
        _scrollToBottom();
        break;

      case "done":
        // Finalise the current agent message
        var msg = event.message || {};
        var content = msg.content || _currentAgentText || "";

        if (_currentAgentBubble === null && content) {
          // No delta events were streamed, but we have content
          _addMessage("agent", content);
        } else if (_currentAgentBubble && content) {
          _currentAgentBubble.innerHTML = _renderMarkdown(content);
        }

        // Append to history
        if (content) {
          _history.push({ role: "assistant", content: content });
        }

        _currentAgentBubble = null;
        _currentAgentText = "";
        _scrollToBottom();
        break;

      case "error":
        // Show red error bubble
        _addError(event.message || "Unknown agent error");
        _scrollToBottom();
        break;

      default:
        // Unknown event types are silently ignored
        break;
    }
  }

  /**
   * Finalise the current agent message when the stream ends without
   * an explicit "done" event (e.g. connection close).
   */
  function _finaliseAgentMessage() {
    if (_currentAgentBubble !== null) {
      var finalText = _currentAgentText.trim();
      if (finalText) {
        _currentAgentBubble.innerHTML = _renderMarkdown(finalText);
        _history.push({ role: "assistant", content: finalText });
      }
      _currentAgentBubble = null;
      _currentAgentText = "";
      _scrollToBottom();
    }
  }

  /**
   * Format a tool result object into a compact one-line summary string.
   *
   * @param {string} name — tool name
   * @param {object} result — tool result object
   * @returns {string} formatted summary
   */
  function _formatToolResult(name, result) {
    if (!result || typeof result !== "object") {
      return "\uD83D\uDD27 " + name + " done";
    }

    // Try to produce a compact one-line summary
    var status = result.status || "";

    if (name === "list_projects") {
      var count = (result.projects && result.projects.length) || 0;
      return "\uD83D\uDD27 " + name + ": " + count + " project(s)";
    }

    if (name === "get_project") {
      var p = result.project || {};
      var pname = p.name || p.project_id || "";
      return "\uD83D\uDD27 " + name + ": " + pname + " (" + (p.status || "?") + ")";
    }

    if (name === "create_project") {
      return "\uD83D\uDD27 " + name + ": created \u201C" + (result.project_id || "") + "\u201D";
    }

    if (name === "list_tasks") {
      var count = (result.tasks && result.tasks.length) || 0;
      return "\uD83D\uDD27 " + name + ": " + count + " task(s)";
    }

    if (name === "list_code_tree" || name === "list_workspace_tree") {
      var count = (result.tree && result.tree.length) || 0;
      return "\uD83D\uDD27 " + name + ": " + count + " file(s)";
    }

    if (name === "read_code_file" || name === "read_workspace_file") {
      var path = result.path || "";
      var len = (result.content && result.content.length) || 0;
      return "\uD83D\uDD27 " + name + ": " + path + " (" + len + " chars)";
    }

    if (name === "detect_intent") {
      var intent = result.intent || "";
      return "\uD83D\uDD27 " + name + ": " + intent;
    }

    // Generic fallback: use status or truncate the result
    if (status) {
      return "\uD83D\uDD27 " + name + ": " + status;
    }

    return "\uD83D\uDD27 " + name + " done";
  }


  // ════════════════════════════════════════════════════════════════════
  //  Reconnect handling
  // ════════════════════════════════════════════════════════════════════

  /**
   * Update the input area state based on connection status.
   * Called periodically while the view is active.
   */
  function _updateConnectionState() {
    var connected = _isConnectionOk();

    if (connected) {
      _removeConnectionPlaceholder();
      _setInputDisabled(false);
    } else {
      _setInputDisabled(true);
      _buildConnectionPlaceholder();
    }
  }

  /**
   * Start monitoring connection state changes.
   * Polls every 2 seconds while the view is active.
   */
  var _connectionTimer = null;

  function _startConnectionMonitor() {
    _stopConnectionMonitor();
    _connectionTimer = setInterval(function () {
      _updateConnectionState();
    }, 2000);
  }

  function _stopConnectionMonitor() {
    if (_connectionTimer !== null) {
      clearInterval(_connectionTimer);
      _connectionTimer = null;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Cleanup
  // ════════════════════════════════════════════════════════════════════

  /**
   * Abort any in-flight SSE stream and reset streaming state.
   */
  function _abortStream() {
    if (_abortController !== null) {
      try {
        _abortController.abort();
      } catch (_e) {
        // ignore
      }
      _abortController = null;
    }

    // Finalise any partial agent message
    _finaliseAgentMessage();

    _agentStreaming = false;
    _currentAgentBubble = null;
    _currentAgentText = "";
  }

  /**
   * Render the initial chat UI into #view-chat.
   * Builds the session selector header, messages container, and input area.
   */
  function _renderUI() {
    var chatView = document.getElementById("view-chat");
    if (!chatView) {
      return;
    }

    // Clear any previous content
    chatView.innerHTML = "";

    // Messages container
    var container = document.createElement("div");
    container.className = "chat-messages";
    chatView.appendChild(container);

    // Session selector header (inserted before .chat-messages)
    _buildSessionSelector();

    // Welcome message
    var welcome = "Chat with the Meta Agent. Type /help for commands.";
    (function () {
      var template = document.getElementById("tpl-chat-msg");
      if (template) {
        var msgEl = template.content.cloneNode(true).firstElementChild;
        if (msgEl) {
          msgEl.className = "chat-msg chat-system";
          var contentEl = msgEl.querySelector(".msg-content");
          if (contentEl) {
            contentEl.textContent = welcome;
          }
          container.appendChild(msgEl);
        }
      }
    })();

    // Input area
    _buildInputArea();

    // Initial connection state
    _updateConnectionState();
  }


  // ════════════════════════════════════════════════════════════════════
  //  Public API
  // ════════════════════════════════════════════════════════════════════

  var Chat = {

    /**
     * Show the chat view.
     *
     * Renders the chat UI (session selector, messages container, input area),
     * ensures a session has been created, restores persisted history,
     * loads the session list, and starts monitoring connection state.
     * Focuses the input field.
     */
    show: function () {
      // Show the container
      var chatView = document.getElementById("view-chat");
      if (chatView) chatView.classList.add("active");

      // Render the UI
      _renderUI();

      // Start monitoring connection state
      _startConnectionMonitor();

      // New flow: init session → restore history → load session list
      var initPromise = _sessionInitiated
        ? Promise.resolve(_sessionId)
        : _initSession();

      initPromise.then(function () {
        return _restoreHistory();
      }).then(function () {
        return _loadSessionList();
      }).catch(function (/* err */) {
        // Silently skip — chat still works without history
      });

      // Focus the input field
      var input = document.getElementById("chat-input-field");
      if (input) {
        setTimeout(function () {
          input.focus();
        }, 100);
      }
    },

    /**
     * Hide the chat view.
     *
     * Aborts any in-flight SSE stream, stops timers, and cleans up
     * streaming state.  The DOM is cleared to prevent stale state
     * on subsequent show() calls.
     *
     * The _history array is PRESERVED so it can be used for dedup
     * when the user returns to the chat page.
     */
    hide: function () {
      // Abort any in-flight stream
      _abortStream();

      // Stop connection monitor
      _stopConnectionMonitor();

      // Reset sending guard
      _sending = false;

      // Hide and clear the DOM (but preserve _history for dedup on re-entry)
      var chatView = document.getElementById("view-chat");
      if (chatView) {
        chatView.classList.remove("active");
        chatView.innerHTML = "";
      }
    },

    /**
     * Send a message to the Meta Agent.
     *
     * If a message is currently being streamed, the new message is
     * ignored.  Slash commands are handled locally; other text is
     * sent via POST /api/agent/chat with SSE streaming.
     *
     * @param {string} text — message text
     */
    sendMessage: function (text) {
      _sendMessage(text);
    },
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.Chat = Chat;
})();
