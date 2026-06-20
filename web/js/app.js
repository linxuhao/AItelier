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
  };


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
    return "";
  }


  // ════════════════════════════════════════════════════════════════════
  //  Flash notification from SSE events (legacy support)
  //  — a small panel for non-error notifications from pipeline events.
  //  Creates a simple floating list; avoids the full NotificationPanel
  //  which is designed as a sidebar.
  // ════════════════════════════════════════════════════════════════════

  function _flashNotification(message) {
    showFlash(message);
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

    // ── Meta conversation checkpoints — skip system modal ───────────
    var isMeta = (graphName === "meta_conversation" || step === "gather");
    if (isMeta) {
      // Meta conversation checkpoints are handled by the chat view.
      // Show a flash notification so the user knows.
      _flashNotification("\uD83D\uDCAC Meta: " + label + " \u2014 answer in chat (" + pid + ")");
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

    // Validate required views
    if (!dashboard || !projectDetail || !chat) {
      console.error("[AItelier] One or more views are not available. App cannot start.");
      return;
    }

    router.init([
      { pattern: "#/", view: dashboard },
      { pattern: "#/projects", view: dashboard },
      { pattern: "#/projects/{id}", view: projectDetail },
      { pattern: "#/chat", view: chat },
    ]);

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
