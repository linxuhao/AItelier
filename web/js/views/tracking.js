"use strict";

(function () {
  /**
   * AItelier.UserTracking — Logged-in user tracking view.
   *
   * Renders a table of tracked users with email, latest access time,
   * and access rights.  Writer-only view — the nav link is absent from
   * the DOM for readers.
   *
   * DOM target: #view-tracking
   * Dependencies: AItelier.API, AItelier.Utils (optional)
   *
   * Usage:
   *   AItelier.UserTracking.show();
   *   AItelier.UserTracking.hide();
   */


  // ── Private helpers ──────────────────────────────────────────────

  /**
   * Escape a plain-text string for safe HTML insertion.
   * Uses the native DOM textContent trick — no library dependency.
   *
   * @param {*} text — raw value to escape
   * @returns {string} HTML-safe string (empty string for null/undefined)
   */
  function _escapeHtml(text) {
    if (text == null) { return ""; }
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(String(text)));
    return div.innerHTML;
  }


  // ── Public API ───────────────────────────────────────────────────

  var UserTracking = {

    /**
     * Show the tracking view.
     * Fetches logged users from the API and renders a table into
     * #view-tracking.
     *
     * @param {object} [params] — unused (reserved for future use)
     */
    show: function (/* params */) {
      var container = document.getElementById("view-tracking");
      if (!container) { return; }

      container.classList.add("active");
      container.innerHTML = "<p>Loading users\u2026</p>";

      // Guard: API must be available
      var api = window.AItelier && window.AItelier.API;
      if (!api || typeof api.getLoggedUsers !== "function") {
        container.innerHTML = "<p style='color:var(--del-color,#b00);'>API not available.</p>";
        return;
      }

      api.getLoggedUsers().then(function (users) {
        // Empty state
        if (!users || users.length === 0) {
          container.innerHTML = "<p>No users tracked yet.</p>";
          return;
        }

        // Format a single timestamp via Utils.formatTime if available
        function _fmtTime(iso) {
          try {
            var utils = window.AItelier && window.AItelier.Utils;
            if (utils && typeof utils.formatTime === "function") {
              return utils.formatTime(iso);
            }
          } catch (_e) { /* fall through */ }
          return iso ? String(iso).slice(0, 16) : "";
        }

        var html = '<table><thead><tr>'
          + '<th>Email</th>'
          + '<th>Latest Access</th>'
          + '<th>Access Rights</th>'
          + '</tr></thead><tbody>';

        for (var i = 0; i < users.length; i++) {
          var u = users[i];
          var email = _escapeHtml(u.email);
          var time = _escapeHtml(_fmtTime(u.last_seen_at));
          var rights = u.access_rights
            ? _escapeHtml(u.access_rights.charAt(0).toUpperCase() + u.access_rights.slice(1))
            : "";
          html += '<tr><td>' + email + '</td><td>' + time + '</td><td>' + rights + '</td></tr>';
        }

        html += '</tbody></table>';
        container.innerHTML = html;
      }).catch(function (err) {
        container.innerHTML = '<p style="color:var(--del-color,#b00);">'
          + 'Failed to load users: ' + _escapeHtml(err && err.message ? err.message : String(err))
          + '</p>';
      });
    },

    /**
     * Hide the tracking view.
     * Clears the #view-tracking content and removes the active class.
     */
    hide: function () {
      var container = document.getElementById("view-tracking");
      if (!container) { return; }
      container.innerHTML = "";
      container.classList.remove("active");
    },
  };


  // ── Expose globally ──────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.UserTracking = UserTracking;
})();
