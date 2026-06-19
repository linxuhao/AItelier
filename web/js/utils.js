"use strict";

(function () {
  /**
   * AItelier.Utils — shared pure utility functions for the AItelier Web Frontend.
   *
   * All functions are stateless, side-effect-free, and guarded against
   * null/undefined inputs.  No framework dependencies.
   *
   * Dependencies (optional, CDN-loaded):
   *   - marked (for renderMarkdown)
   *   - DOMPurify (for renderMarkdown XSS sanitisation)
   */

  var Utils = {
    // ── renderMarkdown ──────────────────────────────────────────────

    /**
     * Safely render Markdown text to an HTML string.
     * Uses `marked.parse()` if available, then `DOMPurify.sanitize()`.
     * Falls back to HTML-escaped plain text when either dependency is absent.
     *
     * @param {string|null|undefined} text — raw Markdown input
     * @returns {string} safe HTML string (empty string for null/undefined/empty)
     */
    renderMarkdown: function (text) {
      if (text == null || text === "") {
        return "";
      }
      var textStr = String(text);

      if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        try {
          var html = marked.parse(textStr);
          return DOMPurify.sanitize(html);
        } catch (_e) {
          return Utils.escapeHtml(textStr);
        }
      }

      // Safe fallback: escape HTML entities
      return Utils.escapeHtml(textStr);
    },

    // ── formatTime ──────────────────────────────────────────────────

    /**
     * Convert an ISO 8601 timestamp string to a human-readable relative
     * time string (e.g. "just now", "2m ago", "1h ago", "3d ago").
     *
     * @param {string|null|undefined} isoString — ISO 8601 date string
     * @returns {string} relative time, or empty string for invalid input
     */
    formatTime: function (isoString) {
      if (isoString == null || isoString === "") {
        return "";
      }

      var date = new Date(isoString);
      if (isNaN(date.getTime())) {
        return "";
      }

      var diffSeconds = Math.floor((Date.now() - date.getTime()) / 1000);

      if (diffSeconds < 5) {
        return "just now";
      }
      if (diffSeconds < 60) {
        return diffSeconds + "s ago";
      }
      var diffMinutes = Math.floor(diffSeconds / 60);
      if (diffMinutes < 60) {
        return diffMinutes + "m ago";
      }
      var diffHours = Math.floor(diffSeconds / 3600);
      if (diffHours < 24) {
        return diffHours + "h ago";
      }
      var diffDays = Math.floor(diffSeconds / 86400);
      return diffDays + "d ago";
    },

    // ── statusClass ─────────────────────────────────────────────────

    /**
     * Map a pipeline status string to a CSS class name.
     *
     * @param {string|null|undefined} status — pipeline status
     * @returns {string} CSS class name, or "" for unknown/empty status
     */
    statusClass: function (status) {
      if (status == null || status === "") {
        return "";
      }

      var map = {
        completed: "status-ok",
        running: "status-warn",
        advancing: "status-warn",
        failed: "status-err",
      };

      return map.hasOwnProperty(status) ? map[status] : "";
    },

    // ── statusIcon ──────────────────────────────────────────────────

    /**
     * Map a pipeline status string to a Unicode icon character.
     *
     * @param {string|null|undefined} status — pipeline status
     * @returns {string} Unicode icon, or "?" for unknown status
     */
    statusIcon: function (status) {
      if (status == null || status === "") {
        return "\u003F"; // ?
      }

      var map = {
        completed: "\u2713",                 // ✓
        running: "\u25B6",                   // ▶
        advancing: "\u25B6",                 // ▶
        failed: "\u2717",                    // ✗
        paused: "\u2717",                    // ✗
        pending: "\u25CB",                   // ○
        planning: "\u25CB",                  // ○
        waiting_user_approval: "\u23F8",     // ⏸
        checkpoint: "\u23F8",                // ⏸
      };

      return map.hasOwnProperty(status) ? map[status] : "\u003F"; // ?
    },

    // ── truncate ────────────────────────────────────────────────────

    /**
     * Truncate a string to a maximum length, appending an ellipsis ("…")
     * if the original text exceeds the limit.
     *
     * @param {string|null|undefined} text — input text
     * @param {number} maxLen — maximum allowed length (non-negative)
     * @returns {string} truncated string, or "" for null/undefined
     */
    truncate: function (text, maxLen) {
      if (text == null) {
        return "";
      }
      var safeMax = Math.max(0, maxLen);
      var str = String(text);
      if (str.length <= safeMax) {
        return str;
      }
      return str.slice(0, safeMax) + "\u2026"; // …
    },

    // ── debounce ────────────────────────────────────────────────────

    /**
     * Create a debounced version of a function.
     * The function is called after `ms` milliseconds have elapsed since
     * the last invocation.  The standard "trailing" debounce pattern.
     *
     * @param {function} fn — function to debounce
     * @param {number} ms — debounce delay in milliseconds
     * @returns {function} debounced function (preserves `this` context)
     */
    debounce: function (fn, ms) {
      var timer = null;
      return function () {
        var context = this;
        var args = arguments;
        if (timer !== null) {
          clearTimeout(timer);
        }
        timer = setTimeout(function () {
          fn.apply(context, args);
          timer = null;
        }, ms);
      };
    },

    // ── escapeHtml ──────────────────────────────────────────────────

    /**
     * Escape special HTML characters in a string, preventing XSS.
     * Replaces: & < > " '
     *
     * @param {string|null|undefined} text — raw text
     * @returns {string} HTML-escaped string, or "" for null/undefined
     */
    escapeHtml: function (text) {
      if (text == null) {
        return "";
      }
      var str = String(text);
      var entityMap = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      };
      return str.replace(/[&<>"']/g, function (m) {
        return entityMap[m];
      });
    },
  };

  // Expose the namespace globally
  window.AItelier = window.AItelier || {};
  window.AItelier.Utils = Utils;
})();
