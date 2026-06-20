"use strict";

(function () {
  /**
   * AItelier.SSE — EventSource lifecycle manager and event dispatcher for the
   * global pipeline SSE stream at GET /api/events/stream.
   *
   * Handles:
   *   - Connection lifecycle (connect / disconnect)
   *   - Two-layer event parsing (outer {"log": "<json>"} → inner event object)
   *   - Out-of-order detection via monotonic _ts counter
   *   - Handler dispatch by event type
   *   - Connection-state signalling via AItelier.App (lazy access)
   *
   * Script loading order: utils → router → api → sse → views → app
   * AItelier.App is NOT available when this module initialises, so all
   * references use the lazy-access pattern `window.AItelier && window.AItelier.App`
   * inside method bodies (exactly as api.js does).
   *
   * Usage:
   *   AItelier.SSE.connect();
   *   AItelier.SSE.on("checkpoint_reached", function (event) { ... });
   *   AItelier.SSE.off("checkpoint_reached", handler);
   *   AItelier.SSE.disconnect();
   */

  // ── Private state ──────────────────────────────────────────────────

  /** @type {EventSource|null} active EventSource instance */
  var _eventSource = null;

  /**
   * Map of event type → array of handler callbacks.
   * @type {Object<string, Function[]>}
   */
  var _handlers = {};

  /**
   * Monotonic sequence number for out-of-order detection.
   * Events with _ts <= _lastTs are discarded.
   * @type {number}
   */
  var _lastTs = 0;

  /** @type {boolean} true while a full-state refresh is in-flight */
  var _refreshing = false;


  // ── Lazy App helpers (guarded against missing App) ────────────────

  /** Set connectionOk = true, hide reconnect banner, trigger refresh. */
  function _onConnected() {
    _lastTs = 0;
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app) {
        app.state.connectionOk = true;
        if (typeof app.hideReconnectBanner === "function") {
          app.hideReconnectBanner();
        }
      }
    } catch (_e) {
      // Silently guard against App not being initialised yet.
    }
  }

  /** Set connectionOk = false, show reconnect banner. */
  function _onDisconnected() {
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app) {
        app.state.connectionOk = false;
        if (typeof app.showReconnectBanner === "function") {
          app.showReconnectBanner();
        }
      }
    } catch (_e) {
      // Silently guard against App not being initialised yet.
    }
  }


  // ── Event parsing ─────────────────────────────────────────────────

  /**
   * Parse the two-layer SSE event format.
   *
   * Wire format from the backend:
   *   data: {"log": "<json-encoded-event-string>"}\n\n
   *
   * Where <json-encoded-event-string> is a JSON-serialized object with
   * at minimum {type, _ts, ...}.
   *
   * @param {string} rawData — the "data:" line content from EventSource
   * @returns {object|null} parsed event object, or null on parse failure
   */
  function _parseEvent(rawData) {
    if (!rawData || typeof rawData !== "string") {
      return null;
    }

    var outer;
    try {
      outer = JSON.parse(rawData);
    } catch (_e) {
      return null;
    }

    // Extract the inner log string
    var logStr = outer && outer.log;
    if (!logStr || typeof logStr !== "string") {
      return null;
    }

    // Parse the inner JSON string
    var eventObj;
    try {
      eventObj = JSON.parse(logStr);
    } catch (_e) {
      return null;
    }

    return eventObj && typeof eventObj === "object" ? eventObj : null;
  }


  // ── Out-of-order detection ───────────────────────────────────────

  /**
   * Check whether an event should be processed based on its _ts field.
   * Events with _ts <= _lastTs are considered stale.
   *
   * @param {object} event — parsed event object
   * @returns {boolean} true if the event is fresh and should be dispatched
   */
  function _isFresh(event) {
    if (!event || typeof event._ts !== "number") {
      // No _ts field — always process (legacy events)
      return true;
    }
    if (event._ts <= _lastTs) {
      return false;
    }
    _lastTs = event._ts;
    return true;
  }


  // ── Handler management ────────────────────────────────────────────

  /**
   * Register a handler for a specific event type.
   *
   * @param {string} eventType — event type key (e.g. "checkpoint_reached")
   * @param {Function} handler — callback receiving the parsed event object
   */
  function _on(eventType, handler) {
    if (!eventType || typeof handler !== "function") {
      return;
    }
    if (!_handlers[eventType]) {
      _handlers[eventType] = [];
    }
    // Avoid duplicate registrations
    if (_handlers[eventType].indexOf(handler) === -1) {
      _handlers[eventType].push(handler);
    }
  }

  /**
   * Remove a previously registered handler.
   *
   * @param {string} eventType — event type key
   * @param {Function} handler — the handler to remove
   */
  function _off(eventType, handler) {
    if (!eventType || typeof handler !== "function") {
      return;
    }
    var list = _handlers[eventType];
    if (!list) {
      return;
    }
    var idx = list.indexOf(handler);
    if (idx !== -1) {
      list.splice(idx, 1);
    }
    // Clean up empty arrays
    if (list.length === 0) {
      delete _handlers[eventType];
    }
  }

  /**
   * Dispatch a parsed event object to all registered handlers for its type.
   *
   * @param {object} event — parsed event object with at least {type, ...}
   */
  function _dispatch(event) {
    if (!event || !event.type) {
      return;
    }

    var etype = event.type;
    var list = _handlers[etype];
    if (!list) {
      return;
    }

    // Iterate over a copy so handler removal during iteration is safe
    var copy = list.slice();
    for (var i = 0; i < copy.length; i++) {
      try {
        copy[i](event);
      } catch (_e) {
        // A bad handler must not break other handlers or the SSE stream
      }
    }
  }


  // ── Connection lifecycle ──────────────────────────────────────────

  /**
   * Create an EventSource connection to /api/events/stream.
   *
   * If a connection already exists, it is closed first.
   * Auto-reconnect is handled by the browser's native EventSource
   * implementation — no manual retry logic.
   */
  function _connect() {
    // Close any existing connection first
    if (_eventSource !== null) {
      _eventSource.close();
      _eventSource = null;
    }

    var url = window.location.origin + "/api/events/stream";

    var es = new EventSource(url);
    _eventSource = es;

    // ── onopen ──────────────────────────────────────────────────────
    es.onopen = function () {
      _onConnected();

      // Trigger a one-time full state refresh so the dashboard re-fetches
      // data after a reconnect.  Dispatch a synthetic "sse_connected" event
      // that views can subscribe to.
      if (!_refreshing) {
        _refreshing = true;
        _dispatch({ type: "sse_connected" });
        // Reset the guard after a short delay to allow re-fetches on
        // subsequent reconnects.
        setTimeout(function () {
          _refreshing = false;
        }, 5000);
      }
    };

    // ── onerror ─────────────────────────────────────────────────────
    es.onerror = function () {
      _onDisconnected();
      // EventSource auto-reconnects natively.  Do NOT manually reconnect.
    };

    // ── onmessage ───────────────────────────────────────────────────
    es.onmessage = function (event) {
      if (!event || !event.data) {
        return;
      }

      var parsed = _parseEvent(event.data);
      if (parsed === null) {
        return;
      }

      // Out-of-order detection
      if (!_isFresh(parsed)) {
        return;
      }

      _dispatch(parsed);
    };
  }

  /**
   * Close the active EventSource connection and clean up.
   */
  function _disconnect() {
    if (_eventSource !== null) {
      _eventSource.close();
      _eventSource = null;
    }
    // Reset state but keep registered handlers (so they work on reconnect)
    _lastTs = 0;
  }


  // ── Public API ────────────────────────────────────────────────────

  var SSE = {
    /**
     * Establish an EventSource connection to the global pipeline SSE stream.
     * If already connected, the previous connection is closed first.
     */
    connect: function () {
      _connect();
    },

    /**
     * Close the active EventSource connection.
     * Registered handlers are preserved for the next connect() call.
     */
    disconnect: function () {
      _disconnect();
    },

    /**
     * Register an event handler for a specific event type.
     *
     * @param {string} eventType — event type key (e.g. "checkpoint_reached")
     * @param {Function} handler — callback receiving the parsed event object
     */
    on: function (eventType, handler) {
      _on(eventType, handler);
    },

    /**
     * Remove a previously registered event handler.
     *
     * @param {string} eventType — event type key
     * @param {Function} handler — the handler to remove
     */
    off: function (eventType, handler) {
      _off(eventType, handler);
    },
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.SSE = SSE;
})();
