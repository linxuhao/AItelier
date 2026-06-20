"use strict";

(function () {
  /**
   * AItelier.CheckpointModal — checkpoint review modal for DPE pipeline
   * human-in-the-loop approval.
   *
   * This is a non-view module (not managed by the hash router).  It wraps
   * the existing <dialog id="checkpoint-modal"> element with approve/reject
   * flows, stale-detection polling, large-file truncation, .md rendering,
   * and DPE-only filtering (skip "gather" meta checkpoints).
   *
   * DOM target: <dialog id="checkpoint-modal">
   * Dependencies: AItelier.API, AItelier.Utils, AItelier.App (optional)
   *
   * Usage:
   *   AItelier.CheckpointModal.show(projectId, checkpointData);
   *   AItelier.CheckpointModal.close();
   *   AItelier.CheckpointModal.isOpen();  // boolean
   */

  // ── Private state ──────────────────────────────────────────────────

  /** @type {string|null} current project ID being reviewed */
  var _projectId = null;

  /** @type {string} current checkpoint step ID (e.g. "1", "2", "3") */
  var _checkpointStep = "";

  /** @type {string} current checkpoint label (e.g. "Architecture Review") */
  var _checkpointLabel = "";

  /** @type {boolean} true while the approve API call is in-flight */
  var _isApproving = false;

  /** @type {boolean} true while the reject API call is in-flight */
  var _isRejecting = false;

  /** @type {boolean} true when the feedback textarea is visible */
  var _feedbackMode = false;

  /** @type {number|null} setInterval handle for stale-detection polling */
  var _staleTimer = null;

  /** @type {boolean} true if the modal was dismissed via the "already_advanced" / stale path */
  var _silentlyClosed = false;

  /** @type {boolean} true if the modal was dismissed via Escape (no action taken) */
  var _escapedClosed = false;

  /** @type {function|null} bound Escape-key handler reference (for cleanup) */
  var _boundKeydownHandler = null;


  // ── DPE checkpoint steps (steps 1, 2, 3).  "gather" is meta — skip. ──
  var _DPE_CHECKPOINT_STEPS = {
    "1": true,
    "2": true,
    "3": true,
  };


  // ── Lazy-access helpers ───────────────────────────────────────────

  /**
   * Lazy-access the dialog element.
   * @returns {HTMLDialogElement|null}
   */
  function _getDialog() {
    return document.getElementById("checkpoint-modal");
  }

  /**
   * Lazy-access a child of the dialog by id.
   * @param {string} id — element id
   * @returns {HTMLElement|null}
   */
  function _getEl(id) {
    return document.getElementById(id);
  }

  /**
   * Try to call AItelier.Dashboard.refresh() if available.
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

  /**
   * Try to show a brief flash message to the user (via App layer or
   * a simple alert fallback).
   *
   * @param {string} message — flash text
   */
  function _flash(message) {
    try {
      // Try App layer toast
      var app = window.AItelier && window.AItelier.App;
      if (app) {
        // Some app layers have a showFlash method
        if (typeof app.showFlash === "function") {
          app.showFlash(message);
          return;
        }
        if (typeof app.showError === "function") {
          // Use showError as fallback but without the error styling
          app.showError(message);
          return;
        }
      }
    } catch (_e) {
      // fallthrough
    }
    // Last resort: console
    console.log("[CheckpointModal]", message);
  }


  // ════════════════════════════════════════════════════════════════════
  //  Stale-detection polling
  // ════════════════════════════════════════════════════════════════════

  /**
   * Start polling GET /api/meta/{pid}/checkpoint every 5 seconds.
   * If the response has no "checkpoint" field or returns 404,
   * the checkpoint was resolved externally → auto-close the modal.
   *
   * @param {string} pid — project ID
   */
  function _startStalePolling(pid) {
    _stopStalePolling();

    _staleTimer = setInterval(function () {
      _checkStale(pid);
    }, 5000);
  }

  /**
   * Stop the stale-detection polling interval.
   */
  function _stopStalePolling() {
    if (_staleTimer !== null) {
      clearInterval(_staleTimer);
      _staleTimer = null;
    }
  }

  /**
   * Perform one stale check: GET /api/meta/{pid}/checkpoint.
   * If the checkpoint is gone, auto-close with a message.
   *
   * @param {string} pid — project ID
   */
  function _checkStale(pid) {
    // Don't check while an approve/reject is in-flight
    if (_isApproving || _isRejecting) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getCheckpoint !== "function") {
      return;
    }

    api.getCheckpoint(pid).then(function (data) {
      // If the dialog is already closed, stop polling
      var dialog = _getDialog();
      if (!dialog || !dialog.open) {
        _stopStalePolling();
        return;
      }

      // No checkpoint field or null/empty → resolved externally
      if (!data || !data.checkpoint) {
        _silentlyClosed = true;
        _silentClose("Checkpoint already resolved");
      }
    }).catch(function (err) {
      // 404 → checkpoint was resolved (project doesn't exist or no checkpoint)
      if (err && (err.status === 404 || (err.message && err.message.indexOf("404") !== -1))) {
        var dialog = _getDialog();
        if (dialog && dialog.open) {
          _silentlyClosed = true;
          _silentClose("Checkpoint already resolved");
        }
      }
      // Other errors (network) → keep the modal open, try again next poll
    });
  }

  /**
   * Silently close the modal without showing any action message.
   *
   * @param {string} reason — brief explanation (shown as flash)
   */
  function _silentClose(reason) {
    _stopStalePolling();
    _silentlyClosed = true;

    var dialog = _getDialog();
    if (dialog && typeof dialog.close === "function") {
      dialog.close();
    }

    _clearContent();
    _resetState();

    if (reason) {
      _flash(reason);
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Content rendering
  // ════════════════════════════════════════════════════════════════════

  /**
   * Populate the modal content with checkpoint data.
   * If checkpointData is not provided, fetch from API.getCheckpoint().
   *
   * @param {string} pid — project ID
   * @param {object|null} checkpointData — optional checkpoint data object
   */
  function _populateContent(pid, checkpointData) {
    var labelEl = _getEl("cp-label");
    var contentEl = _getEl("cp-content");

    if (!contentEl) {
      return;
    }

    // Clear previous content
    contentEl.innerHTML = "";

    // Set label
    var label = _checkpointLabel || "Checkpoint";
    if (labelEl) {
      labelEl.textContent = label;
    }

    // Show loading state if we need to fetch data
    if (!checkpointData) {
      contentEl.textContent = "Loading checkpoint data\u2026";

      var api = window.AItelier && window.AItelier.API;
      if (!api || typeof api.getCheckpoint !== "function") {
        contentEl.textContent = "API client not available.";
        return;
      }

      api.getCheckpoint(pid).then(function (data) {
        // If dialog was already closed, stop
        var dialog = _getDialog();
        if (!dialog || !dialog.open) {
          return;
        }

        if (!data || !data.checkpoint) {
          _silentlyClosed = true;
          _silentClose("Checkpoint already resolved");
          return;
        }

        // Update label from fresh data
        if (data.label) {
          _checkpointLabel = data.label;
          if (labelEl) {
            labelEl.textContent = data.label;
          }
        }
        _checkpointStep = data.step || data.checkpoint || _checkpointStep;

        _renderContent(contentEl, data);
      }).catch(function (err) {
        var dialog = _getDialog();
        if (!dialog || !dialog.open) {
          return;
        }
        if (err && (err.status === 404 || (err.message && err.message.indexOf("404") !== -1))) {
          _silentlyClosed = true;
          _silentClose("Checkpoint already resolved");
          return;
        }
        contentEl.textContent = "Failed to load checkpoint data: " + (err.message || "Unknown error");
      });
      return;
    }

    // Data was provided — render directly
    // Update step from passed data
    if (checkpointData.step) {
      _checkpointStep = checkpointData.step;
    } else if (checkpointData.checkpoint) {
      _checkpointStep = checkpointData.checkpoint;
    }

    if (checkpointData.label && labelEl) {
      _checkpointLabel = checkpointData.label;
      labelEl.textContent = checkpointData.label;
    }

    _renderContent(contentEl, checkpointData);
  }

  /**
   * Render checkpoint files content into the content element.
   *
   * @param {HTMLElement} contentEl — #cp-content element
   * @param {object} data — checkpoint data object
   */
  function _renderContent(contentEl, data) {
    // Extract step_output from data
    var stepOutput = data.step_output || data.stepOutput || {};

    // Normalize: step_output could be {files: {...}} or flat dict
    var files = {};
    var rejectionHistory = null;

    if (stepOutput.files && typeof stepOutput.files === "object" && !Array.isArray(stepOutput.files)) {
      files = stepOutput.files;
    } else if (typeof stepOutput === "object" && !Array.isArray(stepOutput)) {
      // Check if it looks like a flat files dict
      var hasFileContent = false;
      for (var key in stepOutput) {
        if (stepOutput.hasOwnProperty(key) && key !== "rejection_history" && key !== "files") {
          if (typeof stepOutput[key] === "string" && stepOutput[key].length > 10) {
            hasFileContent = true;
            break;
          }
        }
      }
      if (hasFileContent) {
        files = stepOutput;
      } else {
        files = stepOutput.files || {};
      }
    }

    // Collect rejection history
    if (stepOutput.rejection_history) {
      rejectionHistory = stepOutput.rejection_history;
    }

    // Show rejection summary if available
    if (rejectionHistory && Array.isArray(rejectionHistory) && rejectionHistory.length > 0) {
      var latest = rejectionHistory[rejectionHistory.length - 1];
      var lastFeedback = (latest && (latest.user_feedback || latest.reason || "")) || "";

      var revisionNote = document.createElement("div");
      revisionNote.style.marginBottom = "1rem";
      revisionNote.style.padding = "0.5rem";
      revisionNote.style.borderLeft = "3px solid #d49b1a";
      revisionNote.style.backgroundColor = "rgba(212, 155, 26, 0.08)";
      revisionNote.style.borderRadius = "0.25rem";
      revisionNote.style.fontSize = "0.85rem";
      revisionNote.style.lineHeight = "1.5";

      var revisionTitle = document.createElement("strong");
      revisionTitle.textContent = "Revised " + rejectionHistory.length + " time(s)";
      revisionNote.appendChild(revisionTitle);

      if (lastFeedback) {
        var feedbackLine = document.createElement("div");
        feedbackLine.style.marginTop = "0.25rem";
        feedbackLine.style.color = "var(--muted-color, #888)";
        feedbackLine.textContent = "Last feedback: " + lastFeedback;
        revisionNote.appendChild(feedbackLine);
      }

      contentEl.appendChild(revisionNote);
    }

    // No files to show
    var fileKeys = Object.keys(files);
    if (fileKeys.length === 0) {
      var emptyMsg = document.createElement("p");
      emptyMsg.className = "empty-state";
      emptyMsg.textContent = "(No file output to review)";
      contentEl.appendChild(emptyMsg);
      return;
    }

    // Render each file
    for (var i = 0; i < fileKeys.length; i++) {
      var fname = fileKeys[i];
      var fcontent = String(files[fname] || "");

      // Skip internal/system files
      if (fname.charAt(0) === "." || fname === "_snapshot.json" || fname.indexOf("instruction") === 0) {
        continue;
      }

      // Render file section
      _renderFileSection(contentEl, fname, fcontent);
    }
  }

  /**
   * Render a single file section inside the content area.
   *
   * @param {HTMLElement} parentEl — parent container
   * @param {string} fname — file name
   * @param {string} fcontent — file content
   */
  function _renderFileSection(parentEl, fname, fcontent) {
    // ── File header ──
    var header = document.createElement("div");
    header.style.marginTop = (parentEl.children.length > 0 ? "1rem" : "0");
    header.style.marginBottom = "0.25rem";
    header.style.fontSize = "0.85rem";
    header.style.fontWeight = "600";
    header.style.color = "var(--primary, #0066cc)";
    header.textContent = fname;
    parentEl.appendChild(header);

    // Check file size
    var MAX_FILE_SIZE = 50 * 1024; // 50KB
    var isLarge = fcontent.length > MAX_FILE_SIZE;
    var displayContent = fcontent;

    if (isLarge) {
      // Truncate to 50KB and add note
      displayContent = fcontent.slice(0, MAX_FILE_SIZE);
      displayContent += "\n\n[File truncated \u2014 showing first 50KB]";
    }

    // Determine rendering method
    var isMarkdown = fname.toLowerCase().endsWith(".md");

    if (isMarkdown) {
      // Render .md files with Utils.renderMarkdown()
      var mdContainer = document.createElement("div");
      mdContainer.style.lineHeight = "1.6";
      mdContainer.style.fontSize = "0.9rem";

      try {
        var utils = window.AItelier && window.AItelier.Utils;
        if (utils && typeof utils.renderMarkdown === "function") {
          mdContainer.innerHTML = utils.renderMarkdown(displayContent);
        } else {
          // Fallback: escape
          mdContainer.textContent = displayContent;
        }
      } catch (_e) {
        mdContainer.textContent = displayContent;
      }

      parentEl.appendChild(mdContainer);
    } else {
      // All other files: render in <pre><code> block
      var pre = document.createElement("pre");
      pre.style.margin = "0";
      pre.style.padding = "0.75rem";
      pre.style.backgroundColor = "var(--code-background-color, #f5f5f5)";
      pre.style.borderRadius = "0.4rem";
      pre.style.overflowX = "auto";
      pre.style.fontSize = "0.85rem";
      pre.style.lineHeight = "1.5";
      pre.style.whiteSpace = "pre-wrap";
      pre.style.wordBreak = "break-word";
      pre.style.maxHeight = "400px";
      pre.style.overflowY = "auto";

      var code = document.createElement("code");
      code.style.fontFamily = '"SF Mono", "Consolas", "Liberation Mono", monospace';

      try {
        var utils = window.AItelier && window.AItelier.Utils;
        if (utils && typeof utils.escapeHtml === "function") {
          code.textContent = displayContent;
        } else {
          code.textContent = displayContent;
        }
      } catch (_e) {
        code.textContent = displayContent;
      }

      pre.appendChild(code);
      parentEl.appendChild(pre);
    }

    // Large file note (already in content, but add a visible indicator)
    if (isLarge) {
      var largeNote = document.createElement("div");
      largeNote.style.fontSize = "0.8rem";
      largeNote.style.fontStyle = "italic";
      largeNote.style.color = "var(--muted-color, #888)";
      largeNote.style.marginTop = "0.25rem";
      largeNote.textContent = "(File truncated \u2014 showing first 50KB)";
      parentEl.appendChild(largeNote);
    }
  }

  /**
   * Clear the modal content and reset state.
   */
  function _clearContent() {
    var contentEl = _getEl("cp-content");
    if (contentEl) {
      contentEl.innerHTML = "";
    }

    var labelEl = _getEl("cp-label");
    if (labelEl) {
      labelEl.textContent = "Checkpoint";
    }

    // Reset feedback textarea
    var feedbackEl = _getEl("cp-feedback");
    if (feedbackEl) {
      feedbackEl.value = "";
      feedbackEl.hidden = true;
    }

    // Reset reject button text
    var rejectBtn = _getEl("cp-reject");
    if (rejectBtn) {
      rejectBtn.textContent = "Request Changes";
      rejectBtn.disabled = false;
    }

    // Reset approve button
    var approveBtn = _getEl("cp-approve");
    if (approveBtn) {
      approveBtn.textContent = "Approve";
      approveBtn.disabled = false;
    }

    // Reset footer visibility
    var footer = _getDialog() && _getDialog().querySelector("article > footer");
    if (footer) {
      footer.style.display = "";

      // Ensure buttons are visible
      var approveBtn2 = _getEl("cp-approve");
      var rejectBtn2 = _getEl("cp-reject");
      if (approveBtn2) approveBtn2.style.display = "";
      if (rejectBtn2) rejectBtn2.style.display = "";
    }
  }

  /**
   * Reset all internal state variables.
   */
  function _resetState() {
    _projectId = null;
    _checkpointStep = "";
    _checkpointLabel = "";
    _isApproving = false;
    _isRejecting = false;
    _feedbackMode = false;
    _silentlyClosed = false;
    _escapedClosed = false;
  }


  // ════════════════════════════════════════════════════════════════════
  //  Approve flow
  // ════════════════════════════════════════════════════════════════════

  /**
   * Handle the "Approve" button click.
   * Disables the button immediately to prevent double-click, calls
   * API.approveCheckpoint(), and handles success/error/race conditions.
   */
  function _handleApprove() {
    if (_isApproving || _isRejecting) {
      return;
    }

    if (!_projectId) {
      return;
    }

    var approveBtn = _getEl("cp-approve");
    var rejectBtn = _getEl("cp-reject");

    // Disable buttons immediately (prevent double-click)
    _isApproving = true;
    if (approveBtn) {
      approveBtn.disabled = true;
      approveBtn.textContent = "Approving\u2026";
    }
    if (rejectBtn) {
      rejectBtn.disabled = true;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.approveCheckpoint !== "function") {
      _restoreApproveButton();
      return;
    }

    // Call with 45-second timeout (matches t_plan requirement)
    api.approveCheckpoint(_projectId, _checkpointStep, "")
      .then(function (data) {
        // Check for "already_advanced" response — silently close
        if (data && data.status === "already_advanced") {
          _silentlyClosed = true;
          close();
          return;
        }

        // Success — close modal and show flash
        close();

        // Show brief flash
        _flash("\u2713 Approved");

        // Refresh dashboard
        _refreshDashboard();
      })
      .catch(function (err) {
        // On error: re-enable buttons, show error in modal footer

        // Check for "already_advanced" in response body
        if (err && err.message && err.message.indexOf("already_advanced") !== -1) {
          _silentlyClosed = true;
          close();
          return;
        }

        _restoreApproveButton();

        // Show error in footer
        _showFooterError("Approve failed: " + (err.message || "Unknown error"));
      });
  }

  /**
   * Restore the approve button to its default state.
   */
  function _restoreApproveButton() {
    _isApproving = false;
    _isRejecting = false;

    var approveBtn = _getEl("cp-approve");
    var rejectBtn = _getEl("cp-reject");

    if (approveBtn) {
      approveBtn.disabled = false;
      approveBtn.textContent = "Approve";
    }
    if (rejectBtn) {
      rejectBtn.disabled = false;
    }
  }

  /**
   * Show an error message in the modal footer.
   *
   * @param {string} message — error text
   */
  function _showFooterError(message) {
    var dialog = _getDialog();
    if (!dialog) {
      return;
    }

    var footer = dialog.querySelector("article > footer");
    if (!footer) {
      return;
    }

    // Remove any existing error message
    var existingError = footer.querySelector(".cp-footer-error");
    if (existingError) {
      existingError.parentElement.removeChild(existingError);
    }

    var errorEl = document.createElement("div");
    errorEl.className = "cp-footer-error";
    errorEl.style.width = "100%";
    errorEl.style.color = "var(--del-color, #d04040)";
    errorEl.style.fontSize = "0.85rem";
    errorEl.style.marginTop = "0.5rem";
    errorEl.style.padding = "0.5rem";
    errorEl.style.backgroundColor = "rgba(208, 64, 64, 0.08)";
    errorEl.style.borderRadius = "0.25rem";
    errorEl.textContent = message;

    footer.appendChild(errorEl);
  }

  /**
   * Remove any error message from the modal footer.
   */
  function _clearFooterError() {
    var dialog = _getDialog();
    if (!dialog) {
      return;
    }
    var footer = dialog.querySelector("article > footer");
    if (!footer) {
      return;
    }
    var existingError = footer.querySelector(".cp-footer-error");
    if (existingError) {
      existingError.parentElement.removeChild(existingError);
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Reject flow
  // ════════════════════════════════════════════════════════════════════

  /**
   * Handle the "Reject / Request Changes" button click.
   * First click: show feedback textarea.
   * After textarea is visible: submits the rejection.
   */
  function _handleReject() {
    if (_isApproving || _isRejecting) {
      return;
    }

    if (!_projectId) {
      return;
    }

    if (!_feedbackMode) {
      // First click: show feedback textarea
      _showFeedback();
      return;
    }

    // Feedback mode — submit rejection
    var feedbackEl = _getEl("cp-feedback");
    var feedback = feedbackEl ? feedbackEl.value.trim() : "";

    if (!feedback) {
      // Show validation error
      if (feedbackEl) {
        feedbackEl.style.borderColor = "var(--del-color, #d04040)";
        feedbackEl.placeholder = "Feedback is required \u2014 describe what needs to change";
      }
      return;
    }

    // Clear any border styling
    if (feedbackEl) {
      feedbackEl.style.borderColor = "";
    }

    _submitRejection(feedback);
  }

  /**
   * Show the feedback textarea and switch to reject mode.
   */
  function _showFeedback() {
    _feedbackMode = true;

    var feedbackEl = _getEl("cp-feedback");
    var rejectBtn = _getEl("cp-reject");
    var approveBtn = _getEl("cp-approve");

    // Show textarea
    if (feedbackEl) {
      feedbackEl.hidden = false;
      feedbackEl.value = "";
      feedbackEl.style.borderColor = "";
      // Focus after a short delay (DOM needs to catch up)
      setTimeout(function () {
        feedbackEl.focus();
      }, 100);
    }

    // Change reject button to "Submit Rejection"
    if (rejectBtn) {
      rejectBtn.textContent = "Submit Rejection";
    }

    // Hide approve button while in feedback mode
    // (prevent approving while feedback is visible)
    if (approveBtn) {
      approveBtn.style.display = "none";
    }

    // Show a cancel button (or repurpose the approve area)
    var dialog = _getDialog();
    if (dialog) {
      var footer = dialog.querySelector("article > footer");
      if (footer) {
        // Remove any existing cancel button
        var existingCancel = footer.querySelector("#cp-cancel-reject");
        if (existingCancel) {
          existingCancel.parentElement.removeChild(existingCancel);
        }

        var cancelBtn = document.createElement("button");
        cancelBtn.id = "cp-cancel-reject";
        cancelBtn.className = "outline";
        cancelBtn.textContent = "Cancel";
        cancelBtn.style.flexShrink = "0";
        cancelBtn.addEventListener("click", function () {
          _cancelFeedback();
        });
        footer.appendChild(cancelBtn);
      }
    }
  }

  /**
   * Cancel the rejection feedback mode and restore buttons.
   */
  function _cancelFeedback() {
    _feedbackMode = false;

    var feedbackEl = _getEl("cp-feedback");
    var rejectBtn = _getEl("cp-reject");
    var approveBtn = _getEl("cp-approve");
    var cancelBtn = _getEl("cp-cancel-reject");

    // Hide textarea
    if (feedbackEl) {
      feedbackEl.hidden = true;
      feedbackEl.value = "";
      feedbackEl.style.borderColor = "";
    }

    // Restore reject button text
    if (rejectBtn) {
      rejectBtn.textContent = "Request Changes";
    }

    // Show approve button again
    if (approveBtn) {
      approveBtn.style.display = "";
    }

    // Remove cancel button
    if (cancelBtn) {
      cancelBtn.parentElement.removeChild(cancelBtn);
    }
  }

  /**
   * Submit the rejection via API.rejectCheckpoint().
   * On success: close modal, flash, refresh dashboard.
   * On error: keep modal open, show error.
   *
   * @param {string} feedback — rejection reason
   */
  function _submitRejection(feedback) {
    if (_isRejecting || _isApproving) {
      return;
    }

    _isRejecting = true;

    var rejectBtn = _getEl("cp-reject");
    var approveBtn = _getEl("cp-approve");
    var feedbackEl = _getEl("cp-feedback");

    // Disable buttons
    if (rejectBtn) {
      rejectBtn.disabled = true;
      rejectBtn.textContent = "Submitting\u2026";
    }
    if (approveBtn) {
      approveBtn.disabled = true;
    }
    if (feedbackEl) {
      feedbackEl.disabled = true;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.rejectCheckpoint !== "function") {
      _restoreRejectButton();
      return;
    }

    api.rejectCheckpoint(_projectId, _checkpointStep, feedback)
      .then(function (data) {
        // Check for "already_advanced" — silently close
        if (data && data.status === "already_advanced") {
          _silentlyClosed = true;
          close();
          return;
        }

        // Success — close modal and show flash
        close();

        _flash("\u21BA Rejected \u2014 redoing");

        _refreshDashboard();
      })
      .catch(function (err) {
        // Check for "already_advanced" in error message
        if (err && err.message && err.message.indexOf("already_advanced") !== -1) {
          _silentlyClosed = true;
          close();
          return;
        }

        _restoreRejectButton();

        _showFooterError("Reject failed: " + (err.message || "Unknown error"));
      });
  }

  /**
   * Restore the reject button and textarea to pre-submit state.
   */
  function _restoreRejectButton() {
    _isRejecting = false;

    var rejectBtn = _getEl("cp-reject");
    var approveBtn = _getEl("cp-approve");
    var feedbackEl = _getEl("cp-feedback");

    if (rejectBtn) {
      rejectBtn.disabled = false;
      rejectBtn.textContent = "Submit Rejection"; // Keep in feedback mode
    }
    if (approveBtn) {
      approveBtn.disabled = false;
    }
    if (feedbackEl) {
      feedbackEl.disabled = false;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Keyboard handling
  // ════════════════════════════════════════════════════════════════════

  /**
   * Handle Escape key: close the modal without action (user can reopen
   * later via /checkpoint command).
   *
   * @param {KeyboardEvent} e
   */
  function _onKeydown(e) {
    if (e.key === "Escape" || e.key === "Esc") {
      // If in feedback mode, cancel feedback first (don't close on first Escape)
      if (_feedbackMode) {
        e.preventDefault();
        _cancelFeedback();
        return;
      }

      // Close without action
      e.preventDefault();
      _escapedClosed = true;
      close();
    }
  }

  /**
   * Bind the keydown handler.
   */
  function _bindKeyboard() {
    _unbindKeyboard();
    _boundKeydownHandler = _onKeydown;
    document.addEventListener("keydown", _boundKeydownHandler);
  }

  /**
   * Unbind the keydown handler.
   */
  function _unbindKeyboard() {
    if (_boundKeydownHandler) {
      document.removeEventListener("keydown", _boundKeydownHandler);
      _boundKeydownHandler = null;
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  Public API
  // ════════════════════════════════════════════════════════════════════

  var CheckpointModal = {

    /**
     * Show the checkpoint review modal.
     *
     * Populates and opens the #checkpoint-modal dialog.
     * If checkpointData is provided, uses it directly; otherwise fetches
     * from API.getCheckpoint(projectId).
     *
     * Filters out "gather" (meta conversation) checkpoints — those are
     * handled by the Chat view, not this modal.
     *
     * @param {string} projectId — the project ID
     * @param {object|null} checkpointData — optional pre-loaded checkpoint data
     *        with {checkpoint, label, step, step_output, ...}
     */
    show: function (projectId, checkpointData) {
      if (!projectId) {
        return;
      }

      // ── DPE-only filter: skip "gather" (meta conversation) checkpoints ──
      var step = (checkpointData && (checkpointData.step || checkpointData.checkpoint)) || "";
      if (step === "gather") {
        // Meta conversation checkpoint — do NOT show modal
        return;
      }

      // Also check if the data's step is explicitly not in our DPE set
      if (step && !_DPE_CHECKPOINT_STEPS.hasOwnProperty(step)) {
        // step exists but isn't 1, 2, or 3 — could be a non-DPE step.
        // Still show the modal anyway (display the content) — just log it.
      }

      // Reset any previous state
      close();

      _projectId = projectId;

      // Extract step and label from data
      if (checkpointData) {
        _checkpointStep = checkpointData.step || checkpointData.checkpoint || "";
        _checkpointLabel = checkpointData.label || "Checkpoint";
      }

      // Clear previous footer errors
      _clearFooterError();

      // Get the dialog
      var dialog = _getDialog();
      if (!dialog) {
        return;
      }

      // Populate content
      _populateContent(projectId, checkpointData);

      // Bind keyboard events
      _bindKeyboard();

      // Start stale-detection polling
      _startStalePolling(projectId);

      // Open the dialog
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    },

    /**
     * Close the checkpoint modal.
     *
     * Closes the dialog, clears content, stops stale polling, unbinds
     * keyboard handlers, and resets state.
     */
    close: function () {
      close();
    },

    /**
     * Check whether the checkpoint modal is currently open.
     *
     * @returns {boolean} true if the dialog is open
     */
    isOpen: function () {
      var dialog = _getDialog();
      return dialog ? dialog.open : false;
    },
  };

  /**
   * Internal close function (shared between public API and handlers).
   */
  function close() {
    // Stop polling
    _stopStalePolling();

    // Unbind keyboard
    _unbindKeyboard();

    // Close the dialog
    var dialog = _getDialog();
    if (dialog && typeof dialog.close === "function") {
      dialog.close();
    }

    // Clear content
    _clearContent();

    // Reset state
    _resetState();
  }


  // ════════════════════════════════════════════════════════════════════
  //  Wire button event listeners
  // ════════════════════════════════════════════════════════════════════

  // Bind once at module load time (delegating to handlers).
  // Uses dynamic element lookup so it works regardless of when the
  // DOM is fully parsed.

  function _initEventListeners() {
    var approveBtn = _getEl("cp-approve");
    var rejectBtn = _getEl("cp-reject");

    if (approveBtn) {
      approveBtn.addEventListener("click", _handleApprove);
    }

    if (rejectBtn) {
      rejectBtn.addEventListener("click", _handleReject);
    }
  }

  // Initialize when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _initEventListeners);
  } else {
    _initEventListeners();
  }


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.CheckpointModal = CheckpointModal;
})();
