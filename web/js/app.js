"use strict";

(function () {
  /**
   * AItelier.App — application entry point that bootstraps all modules,
   * manages global state, wires routes to views, and handles top-level
   * error recovery.
   *
   * This script is loaded LAST (after utils → router → api → sse → views)
   * so all AItelier.* namespaces are already available.
   *
   * Usage:
   *   // Called once on DOMContentLoaded
   *   AItelier.App.init();
   *
   *   // Public API
   *   AItelier.App.state;                    // global state object
   *   AItelier.App.showError("msg");         // top-level error toast
   *   AItelier.App.setProject("project-id"); // set current project context
   *   AItelier.App.showReconnectBanner();    // show yellow reconnect banner
   *   AItelier.App.hideReconnectBanner();    // remove reconnect banner
   */

  // ════════════════════════════════════════════════════════════════════
  //  Global State
  // ════════════════════════════════════════════════════════════════════

  /**
   * Application-wide mutable state.  Views and modules read from and
   * write to this object.  Direct mutation is allowed (no reactive
   * framework — vanilla JS).
   *
   * @type {{
   *   currentView: string,
   *   currentProjectId: string|null,
   *   connectionOk: boolean,
   *   reconnectAttempt: number
   * }}
   */
  var state = {
    /** @type {string} — currently active view name ("dashboard", "project", "chat") */
    currentView: "",

    /** @type {string|null} — the currently selected project ID */
    currentProjectId: null,

    /** @type {boolean} — backend reachability flag (default true) */
    connectionOk: true,

    /** @type {number} — count of reconnection attempts */
    reconnectAttempt: 0,

    /** @type {boolean} — whether the current user may perform writes. Defaults
     * FALSE (fail closed) and is flipped true only when /api/me confirms it, so
     * write controls never flash before permission is known. UX only — the
     * backend write_gate enforces the actual policy. */
    canWrite: false,

    /** @type {boolean} — true once /api/me has resolved write permission.
     * Write affordances gate on this so they fail closed until resolution, then
     * the active view re-renders to reflect the confirmed permission. */
    permissionResolved: false,
  };


  // ════════════════════════════════════════════════════════════════════
  //  Navigation Bar (dynamic — reader safe)
  // ════════════════════════════════════════════════════════════════════

  /**
   * Render the navigation bar links dynamically based on write permission.
   * The Tracking link is ONLY added when state.canWrite is true — it is
   * absent from the DOM for readers.
   */
  function _renderNav() {
    var navUl = document.getElementById("nav-links");
    if (!navUl) { return; }

    var links = [
      { href: "#/", label: "Dashboard" },
      { href: "#/chat", label: "Chat" },
    ];

    // Only writers see the Tracking link
    if (state.canWrite) {
      links.push({ href: "#/tracking", label: "Tracking" });
    }

    navUl.innerHTML = links.map(function (l) {
      return '<li><a href="' + l.href + '">' + l.label + '</a></li>';
    }).join("");
  }


  // ════════════════════════════════════════════════════════════════════
  //  Read-only mode
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch identity from /api/me and apply read-only mode when the user
   * lacks write permission: short-circuit writes in the API client, mark
   * <body> for CSS, and show a persistent banner.
   */
  function _applyReadOnlyMode() {
    var API = window.AItelier && window.AItelier.API;
    if (!API || typeof API.me !== "function") {
      return;
    }
    API.me().then(function (me) {
      var canWrite = !me || me.can_write !== false;
      state.canWrite = canWrite;
      state.permissionResolved = true;
      API.setCanWrite(canWrite);
      if (!canWrite) {
        document.body.classList.add("readonly");
        _showReadOnlyBanner(me && me.email);
      }
      // Re-render nav to conditionally include Tracking link
      _renderNav();
      // Re-render the active view so any controls rendered during the
      // pre-resolution window now reflect the confirmed permission.
      _refreshActiveViewPermissions();
    }).catch(function () {
      // /api/me unreachable (e.g. local dev without gate) — leave writes on,
      // but mark resolved so affordances stop failing closed.
      state.permissionResolved = true;
      _renderNav();
      _refreshActiveViewPermissions();
    });
  }

  /** Insert a one-time persistent read-only banner at the top of the page. */
  function _showReadOnlyBanner(email) {
    if (document.getElementById("readonly-banner")) {
      return;
    }
    var bar = document.createElement("div");
    bar.id = "readonly-banner";
    bar.className = "readonly-banner";
    bar.textContent = email
      ? "Read-only — signed in as " + email + " (not authorized to make changes)."
      : "Read-only — sign in as an authorized user to make changes.";
    document.body.insertBefore(bar, document.body.firstChild);
  }

  /**
   * Re-render the currently active view so write affordances pick up the
   * confirmed permission once /api/me resolves. Best-effort: a missing view
   * module is simply skipped.
   */
  function _refreshActiveViewPermissions() {
    try {
      var A = window.AItelier || {};
      if (state.currentView === "project" && A.ProjectDetail &&
          typeof A.ProjectDetail.refresh === "function") {
        A.ProjectDetail.refresh();
      } else if (state.currentView === "dashboard" && A.Dashboard &&
          typeof A.Dashboard.refresh === "function") {
        A.Dashboard.refresh();
      } else if (state.currentView === "tracking" && A.UserTracking &&
          typeof A.UserTracking.show === "function") {
        A.UserTracking.show();
      }
    } catch (_e) {
      // Permission re-render is best-effort; views also self-correct on poll.
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Error Toast
  // ════════════════════════════════════════════════════════════════════

  /** @type {number|null} handle for the auto-dismiss timeout */
  var _toastTimer = null;

  /**
   * Create a temporary red error toast at the top of the page.
   * The toast auto-dismisses after 5 seconds.
   *
   * @param {string} message — the error message to display
   */
  function showError(message) {
    // Normalise input
    var msg = (typeof message === "string" && message) || "An unknown error occurred";

    // Remove any existing toast
    var existing = document.getElementById("app-error-toast");
    if (existing) {
      existing.parentElement.removeChild(existing);
    }

    // Create the toast element
    var toast = document.createElement("div");
    toast.id = "app-error-toast";
    toast.style.position = "fixed";
    toast.style.top = "0";
    toast.style.left = "0";
    toast.style.right = "0";
    toast.style.zIndex = "9999";
    toast.style.backgroundColor = "#d04040";
    toast.style.color = "#ffffff";
    toast.style.textAlign = "center";
    toast.style.padding = "0.75rem 1rem";
    toast.style.fontSize = "0.9rem";
    toast.style.boxShadow = "0 2px 8px rgba(0, 0, 0, 0.2)";
    toast.style.animation = "slide-down 0.3s ease-out";
    toast.textContent = msg;

    // Insert at the top of body
    document.body.insertBefore(toast, document.body.firstChild);

    // Auto-dismiss after 5 seconds
    if (_toastTimer !== null) {
      clearTimeout(_toastTimer);
    }
    _toastTimer = setTimeout(function () {
      _removeToast();
    }, 5000);
  }

  /**
   * Remove the error toast if it exists.
   */
  function _removeToast() {
    if (_toastTimer !== null) {
      clearTimeout(_toastTimer);
      _toastTimer = null;
    }
    var toast = document.getElementById("app-error-toast");
    if (toast) {
      // Fade out
      toast.style.transition = "opacity 0.3s ease";
      toast.style.opacity = "0";
      setTimeout(function () {
        if (toast.parentElement) {
          toast.parentElement.removeChild(toast);
        }
      }, 300);
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Reconnect Banner
  // ════════════════════════════════════════════════════════════════════

  /**
   * Show a fixed yellow reconnect banner at the top of the page.
   * Only creates the banner if one is not already present.
   */
  function showReconnectBanner() {
    var existing = document.getElementById("reconnect-banner");
    if (existing) {
      // Banner already visible — update text if reconnection attempts increased
      var textEl = existing.querySelector(".reconnect-text");
      if (textEl) {
        if (state.reconnectAttempt >= 5) {
          textEl.textContent =
            "\u26A0\uFE0F Connection lost for " + state.reconnectAttempt +
            " attempts. Check if the server is running.";
          existing.style.backgroundColor = "#f8d7da";
          existing.style.color = "#721c24";
          existing.style.borderColor = "#f5c6cb";
        } else {
          textEl.textContent =
            "\u26A0\uFE0F Connection lost. Reconnecting\u2026";
          existing.style.backgroundColor = "#fff3cd";
          existing.style.color = "#856404";
          existing.style.borderColor = "#ffeeba";
        }
      }
      return;
    }

    var banner = document.createElement("div");
    banner.id = "reconnect-banner";
    banner.style.position = "fixed";
    banner.style.top = "0";
    banner.style.left = "0";
    banner.style.right = "0";
    banner.style.zIndex = "9998";
    banner.style.backgroundColor = "#fff3cd";
    banner.style.color = "#856404";
    banner.style.textAlign = "center";
    banner.style.padding = "0.5rem 1rem";
    banner.style.fontSize = "0.85rem";
    banner.style.borderBottom = "1px solid #ffeeba";
    banner.style.animation = "slide-down 0.3s ease-out";

    var textSpan = document.createElement("span");
    textSpan.className = "reconnect-text";
    textSpan.textContent = "\u26A0\uFE0F Connection lost. Reconnecting\u2026";
    banner.appendChild(textSpan);

    // Insert at the top of body (before any existing content)
    document.body.insertBefore(banner, document.body.firstChild);
  }

  /**
   * Remove the reconnect banner if it exists.
   */
  function hideReconnectBanner() {
    var banner = document.getElementById("reconnect-banner");
    if (banner) {
      banner.parentElement.removeChild(banner);
    }
    // Reset reconnect attempt counter when connection is restored
    state.reconnectAttempt = 0;
  }


  // ════════════════════════════════════════════════════════════════════
  //  Flash / Notification (lightweight, no CSS dependency)
  // ════════════════════════════════════════════════════════════════════

  /**
   * Show a brief flash notification (green-ish) that auto-clears.
   * Used for non-error events like "Checkpoint approved".
   *
   * @param {string} message — flash text
   */
  function showFlash(message) {
    if (!message) {
      return;
    }
    var flash = document.createElement("div");
    flash.style.position = "fixed";
    flash.style.top = "3.5rem";
    flash.style.right = "1rem";
    flash.style.zIndex = "9997";
    flash.style.backgroundColor = "#2ea44f";
    flash.style.color = "#ffffff";
    flash.style.padding = "0.5rem 1rem";
    flash.style.borderRadius = "0.4rem";
    flash.style.fontSize = "0.85rem";
    flash.style.boxShadow = "0 2px 8px rgba(0, 0, 0, 0.15)";
    flash.style.opacity = "0";
    flash.style.transition = "opacity 0.3s ease";
    flash.textContent = message;

    document.body.appendChild(flash);

    // Fade in
    requestAnimationFrame(function () {
      flash.style.opacity = "1";
    });

    // Auto-dismiss after 3 seconds
    setTimeout(function () {
      flash.style.opacity = "0";
      setTimeout(function () {
        if (flash.parentElement) {
          flash.parentElement.removeChild(flash);
        }
      }, 300);
    }, 3000);
  }


  // ════════════════════════════════════════════════════════════════════
  //  View Lifecycle Coordination
  // ════════════════════════════════════════════════════════════════════

  /**
   * Map view names to their AItelier.* module objects.
   * Used by SSE event handlers and the Router route table.
   *
   * @type {Object<string, {show: Function, hide: Function}>}
   */
  var _views = {};

  /**
   * Resolve the view name for a given view object.
   *
   * @param {object} viewObj — a view module (Dashboard, ProjectDetail, Chat)
   * @returns {string} view name (e.g. "dashboard", "project", "chat")
   */
  function _viewName(viewObj) {
    if (viewObj === window.AItelier.Dashboard) {
      return "dashboard";
    }
    if (viewObj === window.AItelier.ProjectDetail) {
      return "project";
    }
    if (viewObj === window.AItelier.Chat) {
      return "chat";
    }
    if (viewObj === window.AItelier.UserTracking) {
      return "tracking";
    }
    return "";
  }


  // ════════════════════════════════════════════════════════════════════
  //  Flash notification from SSE events (legacy support)
  //  — a small panel for non-error notifications from pipeline events.
  //  Creates a simple floating list; avoids the full NotificationPanel
  //  which is designed as a sidebar.

  /** @type {number} max notification items to keep in the sidebar. */
  var _MAX_NOTIF_ITEMS = 50;

  function _flashNotification(message) {
    showFlash(message);
  }

  /** Built-in DPE step labels; manifest labels (loaded by the dashboard view
   * into window.AItelier.configManifests) take precedence when present. */
  var _NOTIF_STEP_LABELS = {
    "1": "Researcher", "1_review": "Research Review",
    "2": "Architect", "2_review": "Architecture Review",
    "3": "PM", "3_review": "PM Review",
    "5": "Final Verifier", "5_review": "Final Review", "5_test": "Unit Tests",
    "t_plan": "Task Planner", "t_plan_review": "Plan Review",
    "t_impl": "Implementer", "t_impl_review": "Impl Review",
    "t_verify": "Task Verifier", "t_verify_review": "Verify Review",
    "task_loop": "Task Loop",
  };

  /** Resolve a step id to a human-readable label for the given config. */
  function _stepLabel(stepId, graphName) {
    if (!stepId) { return ""; }
    try {
      var cache = window.AItelier && window.AItelier.configManifests;
      var m = cache && graphName && cache[graphName];
      if (m && m.labels && m.labels[stepId]) { return m.labels[stepId]; }
    } catch (_e) { /* fall through */ }
    return _NOTIF_STEP_LABELS[stepId] || stepId;
  }

  /** Truncate a string for inline display. */
  function _truncate(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n) + "\u2026" : s;
  }

  /**
   * Map an SSE pipeline event to a {icon, text, detail} notification, or null
   * to skip it. Mirrors the detail shown by the CLI notification panel
   * (cli/tui/notifications.py): human step names, error text, checkpoint
   * labels, agent messages, and written-file lists — not just raw step ids.
   *
   * @param {object} event — SSE event payload
   * @returns {{icon: string, text: string, detail: string}|null}
   */
  function _formatNotification(event) {
    var type = event.type || "";
    var graph = event.graph_name || event._graph_name || "";
    var stepId = event._step_id || event.step_id || event.step || "";
    var step = _stepLabel(stepId, graph);
    var files = Array.isArray(event.files) ? event.files : [];
    var filePreview = files.length
      ? files.slice(0, 3).join(", ") + (files.length > 3 ? ", \u2026" : "")
      : "";

    switch (type) {
      case "run_started":
      case "pipeline_started":
        return { icon: "\u25B6", text: "Pipeline started", detail: "" };
      case "step_claimed":
      case "step_start":
        return { icon: "\u25B6", text: step || "step", detail: "" };
      case "step_completed":
      case "step_end":
        return { icon: "\u2713", text: (step || "step") + " completed", detail: "" };
      case "step_done":
        return { icon: "\u2713", text: (step || "step") + (filePreview ? " \u2192 " + filePreview : ""), detail: filePreview };
      case "files_written":
        return filePreview
          ? { icon: "\u270E", text: "Wrote " + filePreview, detail: files.join(", ") }
          : null;
      case "step_timeout":
        return { icon: "\u23F0", text: (step || "step") + " timed out", detail: "" };
      case "step_failed":
        var err = _truncate(event.error || event.reason || "", 100);
        return { icon: "\u2717", text: (step || "step") + (err ? ": " + err : " failed"), detail: event.error || "" };
      case "checkpoint_reached":
      case "checkpoint_paused":
        return { icon: "\u23F8", text: (event.label || "Checkpoint") + " \u2014 awaiting review", detail: "" };
      case "checkpoint_resolved":
      case "checkpoint_approved":
        return { icon: "\u2713", text: (event.label || "Checkpoint") + " " + (event.action || "approved"), detail: "" };
      case "checkpoint_rejected":
      case "step_checkpoint_rejected":
        return { icon: "\u21BA", text: (event.label || "Checkpoint") + " rejected \u2014 redo", detail: "" };
      case "agent_message":
        var content = _truncate(event.content || "", 140);
        if (!content) { return null; }
        var lvl = event.level || "info";
        var lvlIcon = lvl === "milestone" ? "\u2605" : (lvl === "warning" ? "\u26A0" : "\u2139");
        return { icon: lvlIcon, text: content, detail: event.content || "" };
      case "project_completed":
        return { icon: "\u2713", text: "Project completed", detail: "" };
      case "project_failed":
      case "run_failed":
        var reason = _truncate(event.reason || event.error || "", 100);
        return { icon: "\u2717", text: "Project failed" + (reason ? ": " + reason : ""), detail: event.reason || "" };
      default:
        // Unknown step-scoped event — show a minimal line rather than dropping it.
        if (stepId) { return { icon: "\u00B7", text: step, detail: "" }; }
        return null;
    }
  }

  /**
   * Append a pipeline event to the #notification-panel sidebar with timestamp,
   * project name, human-readable step name, and event-specific detail.
   *
   * @param {object} event — SSE event payload
   */
  function _addToNotificationPanel(event) {
    var list = document.getElementById("notif-list");
    if (!list) {
      return;
    }

    var fmt = _formatNotification(event);
    if (!fmt) {
      return;
    }

    var task = event._task_id || "";
    var project = event._project_name || event.project_id || "";

    // Timestamp: prefer the server-provided event time (_ts, seconds) so the
    // line reflects when the event happened, not when it rendered.
    var d = event._ts ? new Date(event._ts * 1000) : new Date();
    var ts = d.getHours().toString().padStart(2, "0") + ":" +
             d.getMinutes().toString().padStart(2, "0") + ":" +
             d.getSeconds().toString().padStart(2, "0");

    var item = document.createElement("div");
    item.style.fontSize = "0.8rem";
    item.style.padding = "0.25rem 0.4rem";
    item.style.borderBottom = "1px solid var(--muted-border-color, #eee)";
    item.style.lineHeight = "1.35";

    // Line 1: time · project · task
    var meta = document.createElement("div");
    meta.style.color = "var(--muted-color, #888)";
    meta.style.fontSize = "0.72rem";
    var metaParts = [ts];
    if (project) { metaParts.push(project); }
    if (task) { metaParts.push(task); }
    meta.textContent = metaParts.join(" \u00B7 ");
    item.appendChild(meta);

    // Line 2: icon + message
    var body = document.createElement("div");
    body.style.color = "var(--color, #444)";
    body.textContent = (fmt.icon ? fmt.icon + " " : "") + fmt.text;
    if (fmt.detail) { item.title = fmt.detail; }
    item.appendChild(body);

    // Insert at top
    if (list.firstChild) {
      list.insertBefore(item, list.firstChild);
    } else {
      list.appendChild(item);
    }

    // Trim old items
    while (list.children.length > _MAX_NOTIF_ITEMS) {
      list.removeChild(list.lastChild);
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  SSE Event Wiring
  // ════════════════════════════════════════════════════════════════════

  /**
   * Wire SSE event handlers to global behaviours.
   * Called once during init() after SSE.connect().
   */
  function _wireSSE() {
    var sse = window.AItelier && window.AItelier.SSE;
    if (!sse || typeof sse.on !== "function") {
      return;
    }

    // ── Notification panel: feed pipeline events (parity with CLI panel) ──
    var _notifTypes = [
      "run_started", "pipeline_started",
      "step_claimed", "step_start",
      "step_completed", "step_end", "step_done", "files_written",
      "step_timeout", "step_failed",
      "checkpoint_reached", "checkpoint_paused",
      "checkpoint_resolved", "checkpoint_approved",
      "checkpoint_rejected", "step_checkpoint_rejected",
      "agent_message",
      "project_completed", "project_failed", "run_failed"];
    for (var nt = 0; nt < _notifTypes.length; nt++) {
      sse.on(_notifTypes[nt], _addToNotificationPanel);
    }

    // ── Checkpoint events ───────────────────────────────────────────
    sse.on("checkpoint_reached", function (event) {
      _onCheckpointReached(event);
    });

    sse.on("checkpoint_paused", function (event) {
      _onCheckpointReached(event);
    });

    sse.on("checkpoint_resolved", function (event) {
      _onCheckpointResolved(event);
    });

    sse.on("checkpoint_approved", function (event) {
      _onCheckpointResolved(event);
    });

    // ── Project completion / failure ────────────────────────────────
    sse.on("project_completed", function (event) {
      var pid = event.project_id || "";
      var pname = event._project_name || pid;
      _flashNotification("\u2713 Project \u201C" + pname + "\u201D completed");
      _refreshDashboard();
    });

    sse.on("project_failed", function (event) {
      var pid = event.project_id || "";
      var pname = event._project_name || pid;
      var reason = event.reason || "";
      var msg = "\u2717 Project \u201C" + pname + "\u201D failed";
      if (reason) {
        msg += ": " + reason;
      }
      showError(msg);
      _refreshDashboard();
    });

    // ── SSE reconnected — refresh the current view ──────────────────
    sse.on("sse_connected", function () {
      // The SSE module dispatches this synthetic event after a reconnect.
      // Refresh the current view to show up-to-date data.
      _refreshCurrentView();
    });
  }

  /**
   * Handle checkpoint_reached / checkpoint_paused events.
   * If the checkpoint is for a "gather" (meta conversation) step, skip
   * the system modal — it is handled in the chat view.
   * Otherwise, route to the project view if not already there, then
   * call CheckpointModal.show().
   *
   * @param {object} event — the SSE event object
   */
  function _onCheckpointReached(event) {
    var pid = event.project_id || "";
    var step = event.step || event.step_id || "";
    var label = event.label || "Checkpoint";
    var graphName = event.graph_name || "";

    if (!pid) {
      return;
    }

    // ── Conversational checkpoints — skip the file-diff modal ───────
    // Derive the checkpoint kind from the config manifest; fall back to the
    // legacy meta_conversation/gather check so DPE behavior is unchanged.
    var isConversational = (graphName === "meta_conversation" || step === "gather");
    try {
      var manifests = window.AItelier && window.AItelier.configManifests;
      var m = manifests && manifests[graphName];
      if (m && m.checkpoints && m.checkpoints[step]
          && m.checkpoints[step].kind === "conversational") {
        isConversational = true;
      }
    } catch (_e) { /* fall back to legacy check */ }
    if (isConversational) {
      // Conversational checkpoints are handled by the chat view.
      _flashNotification("\uD83D\uDCAC " + label + " \u2014 answer in chat (" + pid + ")");
      return;
    }

    // ── Route to project view if not already there ──────────────────
    var targetView = "project";
    if (state.currentView !== targetView) {
      var router = window.AItelier && window.AItelier.Router;
      if (router && typeof router.navigate === "function") {
        router.navigate("#/projects/" + encodeURIComponent(pid));
      }
    }

    // ── Show the CheckpointModal ────────────────────────────────────
    var cpModal = window.AItelier && window.AItelier.CheckpointModal;
    if (cpModal && typeof cpModal.show === "function") {
      cpModal.show(pid, {
        checkpoint: step,
        label: label,
        step: step,
        config_name: graphName,
        step_output: event.step_output || null,
      });
    }
  }

  /**
   * Handle checkpoint_resolved / checkpoint_approved events.
   * Close any open CheckpointModal for this project and refresh
   * the dashboard.
   *
   * @param {object} event — the SSE event object
   */
  function _onCheckpointResolved(event) {
    var pid = event.project_id || "";

    // Close CheckpointModal
    var cpModal = window.AItelier && window.AItelier.CheckpointModal;
    if (cpModal && typeof cpModal.close === "function") {
      cpModal.close();
    }

    // Show flash notification
    var label = event.label || "Checkpoint";
    var action = event.action || "resolved";
    _flashNotification("\u2713 " + label + " " + action);

    // Refresh dashboard
    _refreshDashboard();
  }


  // ════════════════════════════════════════════════════════════════════
  //  View Refresh Helpers
  // ════════════════════════════════════════════════════════════════════

  /**
   * Refresh the currently active view's data.
   */
  function _refreshCurrentView() {
    var currentView = state.currentView;

    if (currentView === "dashboard") {
      _refreshDashboard();
    } else if (currentView === "project") {
      var detail = window.AItelier && window.AItelier.ProjectDetail;
      if (detail && typeof detail.refresh === "function") {
        detail.refresh();
      }
    } else if (currentView === "tracking") {
      var tracking = window.AItelier && window.AItelier.UserTracking;
      if (tracking && typeof tracking.show === "function") {
        tracking.show();
      }
    }
    // Chat view doesn't need automatic refresh from SSE — it's
    // conversation-driven.
  }

  /**
   * Call Dashboard.refresh() if available.
   */
  function _refreshDashboard() {
    try {
      var dashboard = window.AItelier && window.AItelier.Dashboard;
      if (dashboard && typeof dashboard.refresh === "function") {
        dashboard.refresh();
      }
    } catch (_e) {
      // Silently guard against missing Dashboard
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Connection Monitoring
  // ════════════════════════════════════════════════════════════════════

  /**
   * Wire SSE module's onopen/onerror callbacks to App's connection
   * monitoring calls.  The SSE module already calls our showReconnectBanner
   * / hideReconnectBanner via its lazy-access pattern.
   *
   * We additionally track reconnectAttempt and show an urgent message
   * after 5+ failed attempts.
   *
   * Called during init().
   */
  function _wireConnectionMonitoring() {
    // The SSE module's _onConnected/_onDisconnected already toggle
    // state.connectionOk and call showReconnectBanner/hideReconnectBanner.
    // We hook into onerror via a global wrapper to increment attempt count.

    // There's no direct EventSource access from here since it's inside
    // the SSE module. Instead, we register for a custom event.
    // But the SSE module dispatches "sse_connected" on reconnect.
    // For disconnection, we can subscribe to SSE events and detect
    // connectionOk going false via polling, or rely on the SSE module's
    // own handling.

    // Since SSE module already handles showReconnectBanner/hideReconnectBanner
    // through lazy-access, we just need to increment reconnectAttempt.
    // We'll do this by overriding the state.connectionOk setter? No —
    // it's a plain object.
    // Best approach: periodically check and detect transitions.

    var _lastKnownConnected = true;

    function _checkConnection() {
      var currentlyConnected = state.connectionOk;

      if (!currentlyConnected && _lastKnownConnected) {
        // Transition: connected → disconnected
        state.reconnectAttempt += 1;
      } else if (currentlyConnected && !_lastKnownConnected) {
        // Transition: disconnected → connected
        // hideReconnectBanner already resets reconnectAttempt to 0.
        // Refresh the current view.
        _refreshCurrentView();
      }

      _lastKnownConnected = currentlyConnected;
    }

    // Poll every 2 seconds for connection state changes
    setInterval(_checkConnection, 2000);
  }


  // ════════════════════════════════════════════════════════════════════
  //  Error Boundaries
  // ════════════════════════════════════════════════════════════════════

  /**
   * Register global error handlers for unhandled promise rejections
   * and uncaught exceptions.  Both are displayed via showError().
   *
   * Called during init().
   */
  function _registerErrorBoundaries() {
    // ── Unhandled promise rejections ──
    window.addEventListener("unhandledrejection", function (event) {
      var reason = event.reason;
      var message = "";

      if (typeof reason === "string") {
        message = reason;
      } else if (reason && reason.message) {
        message = reason.message;
      } else if (reason && typeof reason.toString === "function") {
        message = reason.toString();
      } else {
        message = "Unhandled Promise rejection";
      }

      showError("Error: " + message);

      // Prevent the default console error (but keep the rejection
      // so dev tools still show it)
      event.preventDefault();
    });

    // ── Uncaught exceptions ──
    window.onerror = function (msg, source, lineno, colno, error) {
      var message = msg || "Unknown error";
      if (source) {
        message += " at " + source;
        if (lineno !== undefined) {
          message += ":" + lineno;
          if (colno !== undefined) {
            message += ":" + colno;
          }
        }
      }
      showError("Error: " + message);
      // Return true to prevent the default browser handler
      return true;
    };
  }


  // ════════════════════════════════════════════════════════════════════
  //  Initialisation
  // ════════════════════════════════════════════════════════════════════

  /**
   * Bootstrap the application.  Called once on DOMContentLoaded.
   *
   * Sequence:
   *   1. Check that required CDN globals are present (marked, DOMPurify).
   *      Logs warnings if missing (views degrade gracefully).
   *   2. Build the route table and initialise Router.
   *   3. Connect the global SSE event stream.
   *   4. Wire SSE event handlers to global behaviours.
   *   5. Register global error boundaries.
   *   6. Wire connection monitoring.
   *   7. Wire Router's view switch tracking to update state.currentView.
   */
  function init() {
    // ── Phase 1: Check CDN globals ──
    if (typeof marked === "undefined") {
      console.warn(
        "[AItelier] marked.js is not loaded. " +
        "Markdown rendering will fall back to plain text. " +
        "Ensure the CDN script tag for marked is in index.html."
      );
    }
    if (typeof DOMPurify === "undefined") {
      console.warn(
        "[AItelier] DOMPurify is not loaded. " +
        "XSS sanitisation for rendered Markdown is disabled. " +
        "Ensure the CDN script tag for DOMPurify is in index.html."
      );
    }

    // ── Phase 2: Initialise Router ──
    var router = window.AItelier && window.AItelier.Router;
    if (!router || typeof router.init !== "function") {
      console.error("[AItelier] Router not available. App cannot start.");
      return;
    }

    var dashboard = window.AItelier && window.AItelier.Dashboard;
    var projectDetail = window.AItelier && window.AItelier.ProjectDetail;
    var chat = window.AItelier && window.AItelier.Chat;
    var trace = window.AItelier && window.AItelier.Trace;
    var userTracking = window.AItelier && window.AItelier.UserTracking;

    // Validate required views
    if (!dashboard || !projectDetail || !chat) {
      console.error("[AItelier] One or more views are not available. App cannot start.");
      return;
    }

    var routes = [
      { pattern: "#/", view: dashboard },
      { pattern: "#/projects", view: dashboard },
      { pattern: "#/projects/{id}/trace", view: trace },
      { pattern: "#/projects/{id}", view: projectDetail },
      { pattern: "#/chat", view: chat },
    ];
    // Trace view is optional — only register if it loaded.
    if (!trace) {
      routes = routes.filter(function (r) { return r.view !== trace; });
    }
    // UserTracking view is optional (writer-only) — register if loaded.
    if (userTracking) {
      routes.push({ pattern: "#/tracking", view: userTracking });
    }
    router.init(routes);

    // ── Track current view name in global state ──
    // The Router's hashchange handler calls show/hide on views.
    // We track which view is active by patching `show` on each view.
    // Since Router already handles view switching, we listen for
    // hashchange and derive the view name from the hash.

    function _trackView() {
      var hash = window.location.hash || "#/";
      if (hash.indexOf("#/chat") === 0) {
        state.currentView = "chat";
      } else if (hash.indexOf("#/projects/") === 0) {
        state.currentView = "project";
        var match = hash.match(/^#\/projects\/([^/]+)/);
        if (match && match[1]) {
          state.currentProjectId = decodeURIComponent(match[1]);
        }
      } else if (hash.indexOf("#/tracking") === 0) {
        state.currentView = "tracking";
      } else {
        state.currentView = "dashboard";
      }
    }

    // Track on hashchange
    window.addEventListener("hashchange", _trackView);
    // Track immediately for the initial route
    _trackView();

    // ── Phase 3: Initialise SSE ──
    var sse = window.AItelier && window.AItelier.SSE;
    if (sse && typeof sse.connect === "function") {
      sse.connect();
    } else {
      console.warn("[AItelier] SSE module not available. Real-time events disabled.");
    }

    // ── Phase 4: Wire SSE event handlers ──
    _wireSSE();

    // ── Phase 5: Register error boundaries ──
    _registerErrorBoundaries();

    // ── Phase 6: Wire connection monitoring ──
    _wireConnectionMonitoring();

    // ── Phase 7: Apply read-only mode from /api/me ──
    _applyReadOnlyMode();

    console.log("[AItelier] App initialised successfully.");
  }


  // ════════════════════════════════════════════════════════════════════
  //  setProject — update global project context
  // ════════════════════════════════════════════════════════════════════

  /**
   * Set the current project ID in global state.
   * Notifies views that the project context has changed.
   *
   * @param {string|null} projectId — the project ID to set, or null to clear
   */
  function setProject(projectId) {
    state.currentProjectId = (projectId && typeof projectId === "string")
      ? projectId : null;

    // If the current view is the dashboard and we're setting a project,
    // navigate to the project detail view.
    if (projectId && state.currentView === "dashboard") {
      var router = window.AItelier && window.AItelier.Router;
      if (router && typeof router.navigate === "function") {
        router.navigate("#/projects/" + encodeURIComponent(projectId));
      }
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Bootstrap
  // ════════════════════════════════════════════════════════════════════

  // Initialise on DOMContentLoaded.
  // Since this script is loaded via a regular <script> tag (not defer
  // or module), it executes synchronously after the DOM is parsed but
  // before DOMContentLoaded fires.  We use DOMContentLoaded to ensure
  // the DOM is fully rendered (elements like #view-dashboard exist).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    // DOM is already ready (e.g. script loaded dynamically)
    init();
  }


  // ════════════════════════════════════════════════════════════════════
  //  Public API
  // ════════════════════════════════════════════════════════════════════

  var App = {
    /**
     * Manually trigger initialisation (alternative to DOMContentLoaded).
     * Can be called from tests or dynamic loading scenarios.
     */
    init: init,

    /**
     * Application-wide mutable state.
     * @type {{
     *   currentView: string,
     *   currentProjectId: string|null,
     *   connectionOk: boolean,
     *   reconnectAttempt: number
     * }}
     */
    state: state,

    /**
     * Show a temporary red error toast at the top of the page.
     * Auto-dismisses after 5 seconds.
     *
     * @param {string} message — error description
     */
    showError: showError,

    /**
     * Show a temporary green-ish flash notification at the top-right.
     * Auto-dismisses after 3 seconds.
     *
     * @param {string} message — notification text
     */
    showFlash: showFlash,

    /**
     * Show a fixed yellow reconnect banner at the top of the page.
     * If already visible, updates the text to reflect reconnection
     * attempt count.  Shows urgent message after 5+ failed attempts.
     */
    showReconnectBanner: showReconnectBanner,

    /**
     * Remove the reconnect banner if present.
     * Resets the reconnectAttempt counter to 0.
     */
    hideReconnectBanner: hideReconnectBanner,

    /**
     * Set the current project ID in global state.
     * If currently on the dashboard view, navigates to the project
     * detail view for the given project.
     *
     * @param {string|null} projectId — project ID or null to clear
     */
    setProject: setProject,
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.App = App;
})();
