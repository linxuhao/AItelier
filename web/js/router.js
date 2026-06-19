"use strict";

(function () {
  /**
   * AItelier.Router — lightweight hash-based SPA router.
   *
   * Zero framework dependencies.  Routes are registered via `init(routes)`
   * as an array of `{pattern: string, view: object}` where each view has
   * `show(params)` and `hide()` methods.  Named parameters in patterns
   * (e.g. `#/projects/{id}`) are extracted and passed to `show()`.
   *
   * Usage:
   *   AItelier.Router.init([
   *     { pattern: '#/',              view: AItelier.Dashboard },
   *     { pattern: '#/projects',      view: AItelier.Dashboard },
   *     { pattern: '#/projects/{id}', view: AItelier.ProjectDetail },
   *   ]);
   *   AItelier.Router.navigate('#/projects/42');
   *   AItelier.Router.currentRoute;  // { view: ..., params: { id: '42' } }
   */

  // ── Internal state ────────────────────────────────────────────────

  /** @type {Array<{regex: RegExp, paramNames: string[], view: object}>} */
  var _routes = [];

  /** @type {object|null} — currently active view object */
  var _currentView = null;

  /** @type {object|null} — params for the currently active route */
  var _currentParams = null;


  // ── Core functions ────────────────────────────────────────────────

  /**
   * Normalise a hash string by removing the leading "#", stripping
   * trailing slashes, and treating an empty string as "/".
   *
   * @param {string} hash — raw window.location.hash value
   * @returns {string} normalised path, e.g. "/projects/42"
   */
  function _normalizeHash(hash) {
    if (hash == null) {
      return "/";
    }
    var str = String(hash);
    // Remove leading "#" if present
    if (str.charAt(0) === "#") {
      str = str.slice(1);
    }
    // Strip trailing slash (but not the root "/")
    if (str.length > 1 && str.charAt(str.length - 1) === "/") {
      str = str.slice(0, -1);
    }
    // Empty → root
    if (str === "") {
      str = "/";
    }
    return str;
  }

  /**
   * Compile a route pattern string (e.g. "#/projects/{id}") into a
   * regular expression and an array of named parameter keys.
   *
   * @param {string} pattern — route pattern with optional {name} params
   * @returns {{regex: RegExp, paramNames: string[]}}
   */
  function _compilePattern(pattern) {
    var str = String(pattern);
    // Normalise: strip leading "#", remove trailing slash
    if (str.charAt(0) === "#") {
      str = str.slice(1);
    }
    if (str.length > 1 && str.charAt(str.length - 1) === "/") {
      str = str.slice(0, -1);
    }
    if (str === "") {
      str = "/";
    }

    var paramNames = [];
    // Replace {name} placeholders with named capture groups
    // and collect parameter names in order
    var regexStr = str.replace(/\{(\w+)\}/g, function (_, name) {
      paramNames.push(name);
      return "([^/]+)";
    });

    // Anchor to start and end
    regexStr = "^" + regexStr + "$";

    return {
      regex: new RegExp(regexStr),
      paramNames: paramNames,
    };
  }

  /**
   * Match a normalised hash path against the registered route table.
   *
   * @param {string} hash — normalised hash path (e.g. "/projects/42")
   * @returns {{view: object, params: object}|null}
   */
  function _matchRoute(hash) {
    for (var i = 0; i < _routes.length; i++) {
      var route = _routes[i];
      var match = route.regex.exec(hash);
      if (match !== null) {
        var params = {};
        for (var j = 0; j < route.paramNames.length; j++) {
          params[route.paramNames[j]] = match[j + 1];
        }
        return {
          view: route.view,
          params: params,
        };
      }
    }
    return null;
  }

  /**
   * Switch the active view.  Calls `hide()` on the current view (if any),
   * then `show(params)` on the new view.  If the same view is being shown
   * with different parameters, still calls `hide()` then `show()`.
   *
   * @param {object} newView — view object with show(params) and hide()
   * @param {object} params — parameters to pass to show()
   */
  function _switchView(newView, params) {
    // Hide current view if it exists and is different
    if (_currentView !== null) {
      if (typeof _currentView.hide === "function") {
        _currentView.hide();
      }
    }

    // Show the new view
    if (typeof newView.show === "function") {
      newView.show(params);
    }

    _currentView = newView;
    _currentParams = params;
  }

  /**
   * Handle hashchange events.  Parses the current URL hash, matches it
   * against registered routes, and switches the view accordingly.
   * Unmatched routes redirect to "#/".
   */
  function _onHashChange() {
    var rawHash = window.location.hash;
    var normalised = _normalizeHash(rawHash);
    var match = _matchRoute(normalised);

    if (match === null) {
      // Fallback: unmatched route → redirect to root
      window.location.hash = "#/";
      return;
    }

    _switchView(match.view, match.params);
  }


  // ── Public API ────────────────────────────────────────────────────

  var Router = {

    /**
     * Initialise the router with a route table and start listening for
     * hashchange events.  Immediately resolves the current hash to
     * render the initial view.
     *
     * @param {Array<{pattern: string, view: object}>} routes —
     *   Array of route definitions. Each must have a `pattern` string
     *   (e.g. "#/projects/{id}") and a `view` object with `show(params)`
     *   and `hide()` methods.
     * @throws {Error} if routes is not a valid array or any entry is
     *   missing required fields.
     */
    init: function (routes) {
      // Validate input
      if (!Array.isArray(routes)) {
        throw new Error("Router.init(): routes must be an array");
      }

      // Reset internal state
      _routes = [];
      _currentView = null;
      _currentParams = null;

      for (var i = 0; i < routes.length; i++) {
        var entry = routes[i];

        if (!entry || typeof entry.pattern !== "string" || !entry.view) {
          throw new Error(
            "Router.init(): each route must have a 'pattern' (string) " +
            "and a 'view' object.  Invalid entry at index " + i + "."
          );
        }

        if (typeof entry.view.show !== "function") {
          throw new Error(
            "Router.init(): view at index " + i +
            " must implement show(params)."
          );
        }

        if (typeof entry.view.hide !== "function") {
          throw new Error(
            "Router.init(): view at index " + i +
            " must implement hide()."
          );
        }

        var compiled = _compilePattern(entry.pattern);
        _routes.push({
          regex: compiled.regex,
          paramNames: compiled.paramNames,
          view: entry.view,
        });
      }

      // Bind hashchange listener
      window.addEventListener("hashchange", _onHashChange);

      // Resolve the current hash immediately
      _onHashChange();
    },

    /**
     * Programmatically navigate to a hash route.
     * Updates `window.location.hash`, which triggers `hashchange`
     * and view switching.
     *
     * @param {string} hash — target hash (with or without leading "#")
     */
    navigate: function (hash) {
      var normalised = _normalizeHash(hash);
      window.location.hash = "#" + normalised;
    },

    /**
     * Get the currently active route information.
     *
     * @returns {{view: object, params: object}|null} —
     *   The active view object and its parameters, or null if no
     *   route has been matched yet.
     */
    get currentRoute() {
      if (_currentView === null) {
        return null;
      }
      return {
        view: _currentView,
        params: _currentParams,
      };
    },
  };

  // Expose the namespace globally
  window.AItelier = window.AItelier || {};
  window.AItelier.Router = Router;
})();
