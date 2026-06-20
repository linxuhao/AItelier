"use strict";

(function () {
  /**
   * AItelier.Chat — Meta Agent conversation view with SSE streaming,
   * Markdown rendering, tool call indicators, slash commands, and
   * session management.
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

    var sendBtn = document.createElement("button");
    sendBtn.id = "chat-send-btn";
    sendBtn.textContent = "Send";
    sendBtn.addEventListener("click", function () {
      var text = input.value.trim();
      if (text) {
        input.value = "";
        _sendMessage(text);
      }
    });
    inputArea.appendChild(sendBtn);

    // Enter key submits (without Shift)
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        var text = input.value.trim();
        if (text) {
          input.value = "";
          _sendMessage(text);
        }
      }
    });

    chatView.appendChild(inputArea);

    return input;
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
    var helpLines = [
      "Available commands:",
      "",
      "  /help      \u2014 show this help message",
      "  /clear     \u2014 clear the chat history and visible messages",
      "  /projects  \u2014 navigate to the project dashboard",
      "  /project <id> \u2014 set current project context and navigate",
      "",
      "Any other text will be sent to the Meta Agent.",
    ];
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

      // Add user message to display and history
      _addMessage("user", text);
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
   * Builds the messages container and input area.
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
     * Renders the chat UI (messages container + input area), ensures
     * a session has been created, and starts monitoring connection state.
     * Focuses the input field.
     */
    show: function () {
      // Show the container
      var chatView = document.getElementById("view-chat");
      if (chatView) chatView.classList.add("active");

      // Render the UI
      _renderUI();

      // Initialize session (lazy: created once on first show)
      if (!_sessionInitiated) {
        _initSession().catch(function (/* err */) {
          // Session creation failure is non-critical; chat works without it
        });
      }

      // Start monitoring connection state
      _startConnectionMonitor();

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
     */
    hide: function () {
      // Abort any in-flight stream
      _abortStream();

      // Stop connection monitor
      _stopConnectionMonitor();

      // Reset sending guard
      _sending = false;

      // Hide and clear the DOM
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
