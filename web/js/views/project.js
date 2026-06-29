"use strict";

(function () {
  /**
   * AItelier.ProjectDetail — Project detail view.
   *
   * Shows a single project's information, its associated tasks, and a
   * browsable workspace file tree.  Polls every 3 seconds.
   *
   * DOM target: #view-project
   * Dependencies: AItelier.API, AItelier.Router, AItelier.Utils, AItelier.App (optional)
   *
   * Usage:
   *   AItelier.ProjectDetail.show({id: "my-proj"});
   *   AItelier.ProjectDetail.hide();
   *   AItelier.ProjectDetail.refresh();
   *
   * Template renderer functions are defined inline in this IIFE:
   *   _renderInfoCardHtml(project, canWrite)
   *   _renderCheckpointCardHtml(checkpoint, canWrite)
   *   _renderQuickNavHtml(hasRun, hasTasks)
   *   _renderRunOverviewHtml(run, expandedSteps)
   *   _renderTaskTableHtml(tasks)
   *   _renderRepoStatusHtml(repoData, canWrite)
   *   _renderFileContentModalHtml(content, path)
   */

  // ── Constants ──────────────────────────────────────────────────────

  /** Polling interval in milliseconds. */
  var _POLL_INTERVAL = 3000;

  /** Step ID → human-readable label mapping for status display. */
  /** Merge config-manifest step labels into _STEP_LABELS (from the shared cache
   * populated by the dashboard, or fetched here if not yet loaded). Lets any
   * config's run render proper step names; unknown steps fall back to raw ids. */
  function _ensureConfigLabels() {
    try {
      var cache = window.AItelier && window.AItelier.configManifests;
      if (cache) {
        Object.keys(cache).forEach(function (name) {
          var labels = (cache[name] && cache[name].labels) || {};
          Object.keys(labels).forEach(function (k) { _STEP_LABELS[k] = labels[k]; });
        });
        return;
      }
      var api = window.AItelier && window.AItelier.API;
      if (api && typeof api.getConfigs === "function") {
        api.getConfigs().then(function (resp) {
          var configs = (resp && resp.configs) || [];
          var built = {};
          configs.forEach(function (m) {
            built[m.config_name] = m;
            var labels = m.labels || {};
            Object.keys(labels).forEach(function (k) { _STEP_LABELS[k] = labels[k]; });
          });
          window.AItelier.configManifests = built;
        }).catch(function () {});
      }
    } catch (_e) { /* labels fall back to raw ids */ }
  }

  var _STEP_LABELS = {
    "1": "Researcher",
    "1_review": "Research Review",
    "2": "Architect",
    "2_review": "Architecture Review",
    "3": "PM",
    "3_review": "PM Review",
    "5": "Final Verifier",
    "5_review": "Final Review",
    "t_plan": "Task Planner",
    "t_plan_review": "Plan Review",
    "t_impl": "Implementer",
    "t_impl_review": "Impl Review",
    "t_verify": "Task Verifier",
    "t_verify_review": "Verify Review",
    "task_loop": "Task Loop",
    "5_test": "Unit Tests",
  };

  /** Status → display CSS class mapping for badge colors. */
  var _STATUS_CLASS_MAP = {
    completed: "status-ok",
    running: "status-warn",
    advancing: "status-warn",
    failed: "status-err",
    paused: "",
    planning: "",
    waiting_user_approval: "",
    superseded: "",
  };

  /** Status → Unicode icon mapping. */
  var _STATUS_ICON_MAP = {
    completed: "\u2713",                 // ✓
    running: "\u25B6",                   // ▶
    advancing: "\u25B6",                 // ▶
    failed: "\u2717",                    // ✗
    paused: "\u23F8",                    // ⏸
    pending: "\u25CB",                   // ○
    planning: "\u25CB",                  // ○
    waiting_user_approval: "\u23F8",     // ⏸
    superseded: "\u2298",                // ⊘ replaced by a goal-loop re-run
  };


  // ── Private state ──────────────────────────────────────────────────

  /** @type {number|null} setInterval handle for polling. */
  var _pollTimer = null;

  /** @type {boolean} true while a refresh is in-flight (prevent stacking). */
  var _isRefreshing = false;

  /** @type {string|null} current project ID being viewed. */
  var _projectId = null;

  /** @type {object|null} cached project data from last fetch. */
  var _cachedProject = null;

  /** @type {Array} cached task data from last fetch. */
  var _cachedTasks = [];

  /** @type {object|null} cached run detail (manifest + step instances). */
  var _cachedRun = null;

  /** @type {object|null} cached pending checkpoint from GET .../checkpoint
   * (null when the project is not waiting for approval). */
  var _cachedCheckpoint = null;

  /**
   * Track expanded state for workspace directories.
   * @type {Object<string, boolean>}
   */
  var _expandedDirs = {};

  /**
   * Track which task rows have their detail row expanded.
   * @type {Object<number, boolean>}
   */
  var _expandedTaskRows = {};

  /**
   * Track which run-overview loop steps are expanded to per-instance detail.
   * @type {Object<string, boolean>}
   */
  var _expandedOverviewSteps = {};


  // ── Lazy-access helpers ───────────────────────────────────────────

  /** @returns {boolean} true if the App layer reports a connection issue. */
  function _isConnectionOk() {
    try {
      var app = window.AItelier && window.AItelier.App;
      return app ? !!app.state.connectionOk : true;
    } catch (_e) {
      return true;
    }
  }

  /** @returns {boolean} true only once /api/me has confirmed write permission.
   * Fails closed (no write affordances) until then, so buttons never flash for
   * a read-only user during the optimistic pre-resolution window. */
  function _canWrite() {
    try {
      var app = window.AItelier && window.AItelier.App;
      if (!app || !app.state) { return false; }
      return !!(app.state.permissionResolved && app.state.canWrite);
    } catch (_e) {
      return false;
    }
  }


  // ── Status parsing ────────────────────────────────────────────────

  /**
   * Parse a project status string (possibly compound like "running:t_impl"
   * or "checkpoint:Architecture Review") into a structured display object.
   *
   * @param {string} status — raw status string from API
   * @returns {{text: string, className: string, icon: string}}
   */
  function _parseStatus(status) {
    if (!status) {
      return { text: "", className: "", icon: "\u003F" };
    }

    // Compound patterns: "running:step_id" or "checkpoint:label"
    var colonIdx = status.indexOf(":");
    var baseStatus = colonIdx >= 0 ? status.slice(0, colonIdx) : status;
    var suffix = colonIdx >= 0 ? status.slice(colonIdx + 1) : "";

    var icon = _STATUS_ICON_MAP.hasOwnProperty(baseStatus)
      ? _STATUS_ICON_MAP[baseStatus] : "\u003F";

    var className = _STATUS_CLASS_MAP.hasOwnProperty(baseStatus)
      ? _STATUS_CLASS_MAP[baseStatus] : "";

    var text = "";

    if (baseStatus === "running" && suffix) {
      // "running:t_impl" → "▶ Implementer"
      text = _STEP_LABELS.hasOwnProperty(suffix) ? _STEP_LABELS[suffix] : suffix;
    } else if (baseStatus === "checkpoint" && suffix) {
      // "checkpoint:Architecture Review" → "⏸ Architecture Review"
      text = suffix;
    } else if (baseStatus === "failed" && suffix) {
      text = suffix;
    } else {
      text = baseStatus;
    }

    return {
      text: text,
      className: className,
      icon: icon,
    };
  }

  /** Aggregate status across a step's instances → future|active|done|failed|skipped.
   * A step with no instances is "future" on a live run, but "skipped" once the
   * run is over (e.g. git_sync_pre only runs for existing/cloned repos). */
  function _aggStepStatus(insts, terminal) {
    if (!insts || insts.length === 0) { return terminal ? "skipped" : "future"; }
    var running = insts.some(function (s) {
      return s.status === "running" || s.status === "claimed" || s.status === "in_progress";
    });
    if (running) { return "active"; }
    if (insts.every(function (s) { return s.status === "completed"; })) { return "done"; }
    if (insts.some(function (s) { return s.status === "failed"; })) { return "failed"; }
    return "active";  // mixed (some done, some pending)
  }

  /** Unicode glyph for an instance status. */
  function _statusGlyph(status) {
    if (status === "completed") { return "\u2713"; }       // ✓
    if (status === "failed") { return "\u2717"; }           // ✗
    if (status === "running" || status === "claimed") { return "\u25B6"; }  // ▶
    return "\u25CB";                                         // ○
  }

  /**
   * Format a token count for human-readable display.
   * Values below 1000 are shown as raw numbers; 1000+ as "k" shorthand.
   *
   * @param {number} n — token count
   * @returns {string} formatted string, e.g. "123", "12.5k"
   */
  function _fmtTokens(n) {
    if (typeof n !== "number" || n < 1000) return String(n);
    return (n / 1000).toFixed(1) + "k";
  }


  // ════════════════════════════════════════════════════════════════════
  //  Template-literal HTML Renderer Functions
  // ════════════════════════════════════════════════════════════════════
  //
  //  Pure functions that return HTML strings (or null) for each project
  //  page section.  Each is self-contained — takes data parameters, no
  //  closure over _cachedProject / _cachedTasks / _cachedRun / etc.
  //
  //  Safety rules (apply to ALL functions):
  //    1. All user-supplied text (names, prompts, paths, messages) MUST
  //       be escaped via AItelier.Utils.escapeHtml() before interpolation.
  //    2. Markdown content (briefs, checkpoint messages) MUST be rendered
  //       via AItelier.Utils.renderMarkdown() (DOMPurify-internal).
  //    3. All data-action values must exactly match the event-delegation
  //       contract defined in the project design.
  // ════════════════════════════════════════════════════════════════════

  // ────────────────────────────────────────────────────────────────
  //  1.  Project Info Card  —  _renderInfoCardHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the project info card as an HTML string.
   *
   * @param {object|null} project  — project object from API.getProject()
   * @param {boolean}      canWrite  — true if the current user has write access
   * @returns {string|null}  HTML for the info card, or null if project is null
   */
  function _renderInfoCardHtml(project, canWrite) {
    if (!project) {
      return null;
    }

    var utils = window.AItelier && window.AItelier.Utils;
    var esc   = (utils && typeof utils.escapeHtml === "function")
                ? function (s) { return utils.escapeHtml(s); }
                : function (s) { return String(s); };
    var md    = (utils && typeof utils.renderMarkdown === "function")
                ? function (s) { return utils.renderMarkdown(s); }
                : function (s) { return esc(s); };
    var trunc = (utils && typeof utils.truncate === "function")
                ? function (s, n) { return utils.truncate(s, n); }
                : function (s, n) {
                    return s.length > n ? s.slice(0, n) + "\u2026" : s;
                  };

    var name   = esc(project.name || project.project_id || "");
    var status = project.status || "planning";
    var parsed = _parseStatus(status);
    var icon   = parsed.icon || "";
    var txt    = esc(parsed.text || status);
    var badgeClass = parsed.className || "";

    // Status badge HTML — single class attribute
    var badgeHtml = '<span class="status-badge' +
        (badgeClass ? ' ' + badgeClass : "") +
        '">' + icon + " " + txt + "</span>";

    // Description (brief, truncated to 200 chars, rendered markdown)
    var brief      = project.brief || "";
    var briefHtml  = "";
    if (brief) {
      var truncated = trunc(brief, 200);
      briefHtml = '<p class="project-brief">' + md(truncated) + "</p>";
    }

    // Current step
    var step    = project.current_project_step || "";
    var stepHtml = "";
    if (step) {
      var stepLabel = _STEP_LABELS.hasOwnProperty(step) ? _STEP_LABELS[step] : step;
      stepHtml = '<p style="margin:0.25rem 0;font-size:0.85rem;color:var(--muted-color,#888)">Current step: ' +
        esc(stepLabel) + "</p>";
    }

    // Action buttons
    var buttonsHtml = "";
    var isFailed  = status.indexOf("failed") !== -1;
    var isPaused  = status.indexOf("paused") !== -1;
    var isRunning = status.indexOf("running") !== -1 ||
                    status.indexOf("advancing") !== -1 ||
                    status.indexOf("planning") !== -1 ||
                    status.indexOf("executing") !== -1;

    if (isFailed && canWrite) {
      buttonsHtml += '<button data-action="project-retry" class="outline">Retry</button>\n';
    }

    if (canWrite) {
      buttonsHtml += '<button data-action="project-refresh" class="outline">Refresh Planning</button>\n';
    }

    // View Traces — always visible (read-only action)
    buttonsHtml += '<button data-action="project-trace" class="outline">View Traces</button>\n';

    if (isPaused && canWrite) {
      buttonsHtml += '<button data-action="project-resume" class="outline">Resume</button>\n';
    } else if (isRunning && canWrite) {
      buttonsHtml += '<button data-action="project-pause" class="outline">Pause</button>\n';
    }

    // Assemble card HTML
    return '<article id="project-info-card" class="project-card">\n' +
      '  <header class="project-card-header">\n' +
      '    <h3 style="margin:0">' + name + '</h3>\n' +
      "    " + badgeHtml + "\n" +
      "  </header>\n" +
      '  <div class="project-card-body">\n' +
      briefHtml + "\n" +
      stepHtml + "\n" +
      "  </div>\n" +
      '  <footer class="project-card-footer">\n' +
      buttonsHtml +
      "  </footer>\n" +
      "</article>";
  }


  // ────────────────────────────────────────────────────────────────
  //  2.  Checkpoint Approval Card  —  _renderCheckpointCardHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the inline checkpoint approval card as an HTML string, or null
   * when no checkpoint is pending review.
   *
   * @param {object|null} checkpoint  — checkpoint data from API.getCheckpoint()
   * @param {boolean}      canWrite    — true if the current user has write access
   * @returns {string|null}
   */
  function _renderCheckpointCardHtml(checkpoint, canWrite) {
    if (!checkpoint) {
      return null;
    }
    var step = checkpoint.step || checkpoint.checkpoint || "";
    if (!step) {
      return null;
    }

    var utils = window.AItelier && window.AItelier.Utils;
    var esc   = (utils && typeof utils.escapeHtml === "function")
                ? function (s) { return utils.escapeHtml(s); }
                : function (s) { return String(s); };
    var md    = (utils && typeof utils.renderMarkdown === "function")
                ? function (s) { return utils.renderMarkdown(s); }
                : function (s) { return esc(s); };

    var label         = checkpoint.label || "Checkpoint";
    var cpMessage     = checkpoint.checkpoint_message || checkpoint.message || "";
    var diffSummary   = checkpoint.diff_summary || "";

    // Detect conversational checkpoints
    var conversational = (step === "gather");
    try {
      var cfgName = checkpoint.config_name || checkpoint.graph_name || "";
      var manifests = window.AItelier && window.AItelier.configManifests;
      var m = manifests && manifests[cfgName];
      if (m && m.checkpoints && m.checkpoints[step]
          && m.checkpoints[step].kind === "conversational") {
        conversational = true;
      }
    } catch (_e) { /* fall back to legacy check */ }

    var extraClass = conversational ? " conversational" : "";

    var html = '<article id="project-checkpoint-card" class="project-card checkpoint-card' +
      extraClass + '" data-step="' + esc(step) + '">\n';
    html += '  <header class="project-card-header">\n';
    html += '    <h4 style="margin:0">\u23F8 ' + esc(label) + ' — awaiting review</h4>\n';
    html += "  </header>\n";
    html += '  <div class="project-card-body">\n';

    if (conversational) {
      html += '    <p style="margin:0.25rem 0 0.75rem 0;font-size:0.9rem">This checkpoint is a conversation — answer it in chat to continue.</p>\n';
      html += "  </div>\n";
      html += '  <footer class="project-card-footer">\n';
      html += '    <button data-action="checkpoint-chat">Continue in chat</button>\n';
      html += "  </footer>\n";
      html += "</article>";
      return html;
    }

    // Standard (non-conversational) checkpoint
    html += '    <p style="margin:0.25rem 0 0.75rem 0;font-size:0.9rem;color:var(--muted-color,#888)">' +
      (canWrite
        ? "The pipeline is paused. Approve to continue, or reject with feedback to redo this step."
        : "The pipeline is paused for review. Sign in as a writer to approve or reject.") +
      "</p>\n";

    if (cpMessage) {
      html += '    <div class="checkpoint-message">' + md(cpMessage) + "</div>\n";
    }

    if (diffSummary) {
      html += '    <div class="checkpoint-diff-summary" style="margin-top:0.5rem;font-size:0.85rem;color:var(--muted-color,#888)">' +
        esc(diffSummary) + "</div>\n";
    }

    html += "  </div>\n";
    html += '  <footer class="project-card-footer">\n';

    if (canWrite) {
      html += '    <button data-action="checkpoint-approve" data-step="' + esc(step) + '">Approve</button>\n';
      html += '    <button data-action="checkpoint-reject" data-step="' + esc(step) + '" class="outline">Reject\u2026</button>\n';
    }

    html += '    <button data-action="checkpoint-review" data-step="' + esc(step) + '" class="outline">Review full diff</button>\n';
    html += "  </footer>\n";

    // Reject feedback block (initially hidden)
    html += '  <div class="feedback-block hidden" data-step="' + esc(step) + '">\n';
    html += '    <textarea placeholder="What should be changed? (required)" rows="3" style="width:100%"></textarea>\n';
    html += '    <button data-action="checkpoint-submit" data-step="' + esc(step) + '" class="secondary" style="margin-top:0.5rem">Submit rejection</button>\n';
    html += "  </div>\n";

    html += "</article>";
    return html;
  }


  // ────────────────────────────────────────────────────────────────
  //  3.  Quick Navigation Bar  —  _renderQuickNavHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the quick-navigation jump-link bar as an HTML string.
   *
   * @param {boolean} hasRun    — true if a run overview section will be rendered
   * @param {boolean} hasTasks  — true if a task table section will be rendered
   * @returns {string}
   */
  function _renderQuickNavHtml(hasRun, hasTasks) {
    var html = '<nav class="project-quick-nav">\n';
    html += '  <span>Jump to:</span>\n';
    if (hasTasks) {
      html += '  <a class="quick-nav-link" data-action="quick-nav" data-target="project-tasks">Tasks</a>\n';
    }
    if (hasRun) {
      html += '  <a class="quick-nav-link" data-action="quick-nav" data-target="run-overview">Pipeline</a>\n';
    }
    html += '  <a class="quick-nav-link" data-action="quick-nav" data-target="workspace-section-dps">Artifacts</a>\n';
    html += '  <a class="quick-nav-link" data-action="quick-nav" data-target="repo-section">Repository</a>\n';
    html += "</nav>";
    return html;
  }


  // ────────────────────────────────────────────────────────────────
  //  4.  Pipeline Run Overview  —  _renderRunOverviewHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the pipeline run-overview stepper as an HTML string, or null
   * when no run / manifest is available.
   *
   * @param {object|null} run — run detail from API.getRun()
   * @param {object}      expandedSteps — map of stepId → boolean for loop steps
   * @returns {string|null}
   */
  function _renderRunOverviewHtml(run, expandedSteps) {
    if (!run || !run.manifest || !Array.isArray(run.manifest.steps) ||
        run.manifest.steps.length === 0) {
      return null;
    }

    var manifest = run.manifest;
    var terminal = run.status === "completed" || run.status === "failed";

    // Group step instances by step_id
    var byStep = {};
    (run.steps || []).forEach(function (s) {
      (byStep[s.step_id] = byStep[s.step_id] || []).push(s);
    });

    // Count progress
    var done = 0, ran = 0, skipped = 0;
    manifest.steps.forEach(function (id) {
      var insts = byStep[id] || [];
      if (!insts.length) {
        if (terminal) { skipped++; }
        return;
      }
      ran++;
      if (insts.every(function (s) { return s.status === "completed"; })) { done++; }
    });

    var denom = terminal ? ran : manifest.steps.length;
    var progressText = done + "/" + denom + " steps" +
      (run.status ? " · " + run.status : "") +
      (skipped ? " · " + skipped + " skipped" : "");

    var esc = (window.AItelier && window.AItelier.Utils &&
               typeof window.AItelier.Utils.escapeHtml === "function")
              ? window.AItelier.Utils.escapeHtml
              : function (s) { return String(s); };

    var html = '<section id="run-overview" class="run-overview">\n';

    // Header
    html += '  <div class="run-overview-head">\n';
    html += '    <strong>Pipeline</strong>\n';
    html += '    <span class="run-overview-progress">' + esc(progressText) + '</span>\n';
    html += "  </div>\n";

    // Step strip
    html += '  <div class="run-strip">\n';

    manifest.steps.forEach(function (stepId) {
      var insts      = byStep[stepId] || [];
      var label      = (manifest.labels && manifest.labels[stepId]) || stepId;
      var st         = _aggStepStatus(insts, terminal);
      var retries    = insts.reduce(function (a, s) {
        return a + (s.retry_count || 0) + (s.validation_retry_count || 0);
      }, 0);
      var isCheckpoint = manifest.checkpoints &&
        Object.prototype.hasOwnProperty.call(manifest.checkpoints, stepId);

      var pillClass = "run-step run-step-" + st;
      var isClickable = insts.length > 1;

      if (isClickable) {
        pillClass += " run-step-clickable";
      }

      html += '    <div class="' + pillClass + '"';
      if (isClickable) {
        html += ' data-action="toggle-overview-step" data-step="' + esc(stepId) + '"';
        html += ' title="Click to show per-task status"';
      } else if (insts.length === 1 && insts[0].error) {
        html += ' title="' + esc(insts[0].error) + '"';
      }
      html += ">\n";

      html += '      <span class="run-step-label">' +
        (isCheckpoint ? "\u23F8 " : "") + esc(label) + '</span>\n';

      if (insts.length > 1) {
        html += '      <span class="run-step-badge run-step-loop">\u00D7' +
          insts.length + '</span>\n';
      }
      if (retries > 0) {
        html += '      <span class="run-step-badge run-step-retry" title="' +
          retries + " retr" + (retries === 1 ? "y" : "ies") + '">\u21BB' +
          retries + '</span>\n';
      }
      var cs = run.cache_stats_by_step && run.cache_stats_by_step[stepId];
      if (cs && cs.hit_ratio != null && cs.hit_ratio !== undefined) {
        var pct = (cs.hit_ratio * 100).toFixed(1) + "%";
        var badgeText = pct;
        if (cs.total_tokens != null && cs.total_tokens !== undefined) {
          badgeText += " \u00B7 " + _fmtTokens(cs.total_tokens);
        }
        var badgeClass = "run-step-badge run-step-cache";
        if (cs.hit_ratio >= 0.7) badgeClass += " cache-badge-high";
        else if (cs.hit_ratio >= 0.3) badgeClass += " cache-badge-mid";
        else badgeClass += " cache-badge-low";
        html += '      <span class="' + badgeClass + '">' + badgeText + '</span>\n';
      }

      html += "    </div>\n";
    });

    html += "  </div>\n";

    // Detail panel — show expanded loop steps
    var hasDetail = false;
    var detailHtml = "";

    if (expandedSteps) {
      Object.keys(expandedSteps).forEach(function (stepId) {
        if (!expandedSteps[stepId]) { return; }
        var insts = byStep[stepId] || [];
        if (!insts.length) { return; }
        hasDetail = true;
        var label = (manifest.labels && manifest.labels[stepId]) || stepId;
        detailHtml += '    <div class="run-detail-block">\n';
        detailHtml += '      <div class="run-detail-title">' +
          esc(label) + " — " + insts.length + " run(s)</div>\n";

        insts.forEach(function (s, i) {
          var rr = (s.retry_count || 0) + (s.validation_retry_count || 0);
          var glyph = _statusGlyph(s.status);
          var line = "#" + (i + 1) + "  " + glyph + " " + s.status +
            (rr ? ("  \u21BB" + rr + " retr" + (rr === 1 ? "y" : "ies")) : "");
          detailHtml += '      <div class="run-inst"' +
            (s.error ? ' title="' + esc(s.error) + '"' : "") + ">" +
            esc(line) + "</div>\n";
        });

        detailHtml += "    </div>\n";
      });
    }

    if (hasDetail) {
      html += '  <div class="run-detail-panel">\n';
      html += detailHtml;
      html += "  </div>\n";
    }

    html += "</section>";
    return html;
  }


  // ────────────────────────────────────────────────────────────────
  //  5.  Task Table  —  _renderTaskTableHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the task list table as an HTML string.
   *
   * @param {Array} tasks — array of task objects from API.listTasks()
   * @returns {string}
   */
  function _renderTaskTableHtml(tasks) {
    var utils = window.AItelier && window.AItelier.Utils;
    var esc   = (utils && typeof utils.escapeHtml === "function")
                ? function (s) { return utils.escapeHtml(s); }
                : function (s) { return String(s); };
    var timeAgo = (utils && typeof utils.timeAgo === "function")
                  ? function (t) { return utils.timeAgo(t); }
                  : function (t) { return String(t); };
    var truncFn = (utils && typeof utils.truncate === "function")
                  ? function (s, n) { return utils.truncate(s, n); }
                  : function (s, n) {
                      return s.length > n ? s.slice(0, n) + "\u2026" : s;
                    };

    if (!tasks || tasks.length === 0) {
      return '<div class="empty-state">No tasks yet</div>';
    }

    var html = '<section id="project-tasks">\n';
    html += '  <h4>Tasks</h4>\n';
    html += '  <table style="width:100%">\n';
    html += "    <thead>\n";
    html += "      <tr>\n";
    html += '        <th class="col-idx">#</th>\n';
    html += '        <th>Prompt</th>\n';
    html += '        <th>Status</th>\n';
    html += '        <th>Steps</th>\n';
    html += '        <th>Created</th>\n';
    html += "      </tr>\n";
    html += "    </thead>\n";
    html += '    <tbody id="task-tbody">\n';

    for (var i = 0; i < tasks.length; i++) {
      var task      = tasks[i];
      var taskId    = task.id || "-";
      var status    = task.status || "pending";
      var parsed    = _parseStatus(status);
      var icon      = parsed.icon || "";
      var txt       = esc(parsed.text || status);
      var badgeCls  = parsed.className || "";
      var prompt    = task.prompt || "";
      var truncated = truncFn(prompt, 80);
      // completed_steps comes from the API as a JSON-encoded string
      // (e.g. '["t_plan","t_impl"]'); parse it so .length is the step count
      // (not the string length) and data-completed-steps isn't double-encoded.
      var completedSteps = task.completed_steps || [];
      if (typeof completedSteps === "string") {
        try { completedSteps = JSON.parse(completedSteps); }
        catch (_e) { completedSteps = []; }
      }
      if (!Array.isArray(completedSteps)) { completedSteps = []; }
      var totalSteps     = task.total_steps;
      var stepsStr = completedSteps.length + "/" +
        (totalSteps != null ? totalSteps : "?") + " completed";
      var created   = task.created_at || task.created || "";

      html += '      <tr data-task-id="' + esc(String(taskId)) + '"' +
        ' data-task-status="' + esc(status) + '"' +
        ' data-completed-steps="' + esc(JSON.stringify(completedSteps)) + '"' +
        ' data-prompt="' + esc(prompt) + '">\n';
      html += '        <td class="col-idx">' + (i + 1) + '</td>\n';
      html += '        <td class="task-prompt">' +
        esc(truncated) + '</td>\n';
      html += '        <td><span class="status-badge' +
        (badgeCls ? " " + badgeCls : "") + '">' +
        icon + " " + txt + '</span></td>\n';
      html += '        <td>' + esc(stepsStr) + '</td>\n';
      html += '        <td>' + esc(timeAgo(created)) + '</td>\n';
      html += "      </tr>\n";
    }

    html += "    </tbody>\n";
    html += "  </table>\n";
    html += "</section>";

    return html;
  }


  // ────────────────────────────────────────────────────────────────
  //  6.  Repository Status Panel  —  _renderRepoStatusHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build the repository status panel as an HTML string.
   *
   * @param {object}  repoData  — payload from API.repoStatus()
   * @param {boolean} canWrite  — true if the current user has write access
   * @returns {string}
   */
  function _renderRepoStatusHtml(repoData, canWrite) {
    var esc = (window.AItelier && window.AItelier.Utils &&
               typeof window.AItelier.Utils.escapeHtml === "function")
              ? window.AItelier.Utils.escapeHtml
              : function (s) { return String(s); };

    var api = window.AItelier && window.AItelier.API;

    var html = '<div class="repo-status-panel">\n';

    // Download ZIP - available to readers too. The archive is an owner-scoped
    // read, same access surface as browsing the file tree / file content.
    var archiveUrl = (api && typeof api.repoArchiveUrl === "function")
                     ? api.repoArchiveUrl(_projectId) : "#";
    html += '  <div style="margin:0 0 0.75rem 0">\n';
    html += '    <a href="' + esc(archiveUrl) +
      '" role="button" class="outline" download="" ' +
      'style="font-size:0.85rem">\u2B07 Download ZIP</a>\n';
    html += "  </div>\n";

    // Not a git repository
    if (!repoData.is_git) {
      html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Status:</strong> Not a git repository</p>\n';
      if (repoData.path) {
        html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Path:</strong> ' + esc(repoData.path) + "</p>\n";
      }
      html += "</div>";
      return html;
    }

    // Branch
    if (repoData.branch) {
      html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Branch:</strong> ' + esc(repoData.branch) + "</p>\n";
    }

    // Working tree state
    html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Working tree:</strong> ';
    if (repoData.dirty) {
      html += '<span class="status-badge status-warn">\u2717 ' +
        esc(repoData.dirty_count || 0) + " uncommitted change(s)</span>";
    } else {
      html += '<span class="status-badge status-ok">\u2713 clean</span>';
    }
    html += "</p>\n";

    // Remote + upstream
    html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Remote:</strong> ' +
      esc(repoData.remote_url || "\u2014 none configured") + "</p>\n";

    html += '  <p style="margin:0.2rem 0;font-size:0.85rem"><strong>Sync:</strong> ';
    if (repoData.upstream && (repoData.ahead != null || repoData.behind != null)) {
      html += esc((repoData.ahead || 0) + " ahead, " + (repoData.behind || 0) +
        " behind " + repoData.upstream);
    } else {
      html += "no upstream tracking branch";
    }
    html += "</p>\n";

    // Recent commits
    var commits = repoData.commits || [];
    html += '  <p style="margin:0.6rem 0 0.2rem 0;font-size:0.85rem"><strong>Recent commits:</strong></p>\n';

    if (commits.length === 0) {
      html += '  <p class="empty-state" style="padding:1rem">No commits yet</p>\n';
    } else {
      html += '  <ul class="repo-commit-list">\n';
      for (var ci = 0; ci < commits.length && ci < 10; ci++) {
        var c = commits[ci];
        html += "    <li>\n";
        html += '      <code>' + esc(c.hash || "") + '</code>\n';
        html += "      " + esc(c.subject || "") + "\n";
        html += '      <span style="color:var(--muted-color,#888);margin-left:0.5rem">' +
          "\u2014 " + esc(c.author || "") +
          (c.date ? ", " + esc(c.date.slice(0, 10)) : "") + "</span>\n";
        html += "    </li>\n";
      }
      html += "  </ul>\n";
    }

    // Write action buttons (only for writers)
    if (canWrite) {
      html += '  <div class="repo-actions" style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.75rem;padding-top:0.75rem;border-top:1px solid var(--muted-border-color)">\n';

      var actions = [
        { label: repoData.remote_url ? "Set Remote" : "Add Remote", action: "repo-set-remote" },
        { label: "Commit",    action: "repo-commit" },
        { label: "Push",      action: "repo-push" },
        { label: "Pull",      action: "repo-pull" },
        { label: "Force Sync", action: "repo-force-sync" },
        { label: "Make PR",   action: "repo-make-pr" },
      ];

      for (var ai = 0; ai < actions.length; ai++) {
        html += '    <button data-action="' + actions[ai].action +
          '" class="outline" style="font-size:0.8rem;margin:0">' +
          esc(actions[ai].label) + "</button>\n";
      }

      html += "  </div>\n";
    }

    html += "</div>";
    return html;
  }


  // ────────────────────────────────────────────────────────────────
  //  7.  File Content Modal  —  _renderFileContentModalHtml()
  // ────────────────────────────────────────────────────────────────

  /**
   * Build a file-content viewer <dialog> as an HTML string.
   *
   * @param {string} content  — the file content text
   * @param {string} path     — the file path (for the title)
   * @returns {string}
   */
  function _renderFileContentModalHtml(content, path) {
    var esc = (window.AItelier && window.AItelier.Utils &&
               typeof window.AItelier.Utils.escapeHtml === "function")
              ? window.AItelier.Utils.escapeHtml
              : function (s) { return String(s); };

    // Detect binary content (null content or content containing null bytes)
    var isBinary = (content === null || content === undefined ||
                    (typeof content === "string" && content.indexOf("\x00") !== -1));

    var displayContent = "";
    if (isBinary) {
      displayContent = "Cannot display binary content";
    } else {
      // Truncate at 50000 chars
      displayContent = content.length > 50000
        ? content.slice(0, 50000) + "\n\n... [truncated at 50000 chars]"
        : content;
    }

    // No `open` attribute: the dialog is opened via showModal() in
    // _showFileContent() (modal, with backdrop + Escape). Rendering it `open`
    // makes it a non-modal open dialog, and showModal() then throws
    // ("already open as a non-modal dialog").
    var html = '<dialog id="file-content-dialog" ' +
      'style="max-width:80vw;max-height:80vh;width:800px;padding:0;' +
      'border:none;border-radius:0.5rem;box-shadow:0 8px 32px rgba(0,0,0,0.3)">\n';

    html += '  <article style="margin:0;padding:var(--pico-spacing,1rem);' +
      'display:flex;flex-direction:column;max-height:80vh">\n';

    // Header
    html += '    <header style="flex-shrink:0;padding-bottom:var(--pico-spacing,1rem)">\n';
    html += '      <h3 id="file-content-title" style="margin:0">' + esc(path) + '</h3>\n';
    html += "    </header>\n";

    // Content body
    html += '    <div id="file-content-body" style="flex:1;overflow-y:auto;max-height:55vh;' +
      'padding:0 0 var(--pico-spacing,1rem) 0;' +
      'white-space:pre-wrap;word-wrap:break-word;' +
      'font-family:\'SF Mono\',\'Consolas\',\'Liberation Mono\',monospace;' +
      'font-size:0.85rem;line-height:1.5">\n';

    if (isBinary) {
      html += "      " + esc(displayContent) + "\n";
    } else {
      html += '      <pre style="margin:0"><code id="file-content-code">' +
        esc(displayContent) + "</code></pre>\n";
    }

    html += "    </div>\n";

    // Footer with close button
    html += '    <footer style="flex-shrink:0;display:flex;flex-direction:row;' +
      'justify-content:flex-end;padding-top:var(--pico-spacing,1rem);' +
      'border-top:1px solid var(--muted-border-color,#e0e0e0)">\n';
    html += '      <button data-action="close-modal" class="outline">Close</button>\n';
    html += "    </footer>\n";

    html += "  </article>\n";
    html += "</dialog>";

    return html;
  }


  // ════════════════════════════════════════════════════════════════════
  //  View Shell — _render(), _updateDynamic(), _onDynamicClick()
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch project data + tasks, then re-render the full view.
   * Called by _refresh() after fetching data.
   *
   * Uses template-literal renderer functions (defined above) to build
   * #project-dynamic content, then appends native <details> accordions
   * for workspace sections (outside the dynamic region so they survive polling).
   *
   * @param {object} project — project object from API.getProject()
   * @param {Array} tasks — task array from API.listTasks()
   * @param {object|null} run — run detail from API.getRun()
   */
  function _render(project, tasks, run) {
    _cachedProject = project;
    _cachedTasks = tasks || [];
    _cachedRun = run || null;

    var container = document.getElementById("view-project");
    if (!container) {
      return;
    }

    // Clear everything
    container.innerHTML = "";

    // ── Static shell elements (never change during polling) ──
    // Back link
    container.innerHTML += '<a href="#/" style="display:inline-block;margin-bottom:var(--pico-spacing,1rem)">\u2190 Back to Dashboard</a>';

    // Reconnect overlay
    container.innerHTML += '<div id="project-reconnect-overlay" style="display:none;position:relative;text-align:center;padding:2rem 1rem;background-color:rgba(255,255,255,0.85);border-radius:0.5rem;margin-top:1rem">Reconnecting\u2026</div>';

    if (!project) {
      return;
    }

    // Merge config manifest labels so step names render for any config.
    _ensureConfigLabels();

    // ── Dynamic content slot (#project-dynamic) ──
    var dynamic = document.createElement("div");
    dynamic.id = "project-dynamic";

    var html = "";
    var cpHtml = _renderCheckpointCardHtml(_cachedCheckpoint, _canWrite());
    if (cpHtml) html += cpHtml;
    html += _renderQuickNavHtml(_cachedRun != null, tasks && tasks.length > 0);
    html += _renderInfoCardHtml(project, _canWrite());
    var runHtml = _renderRunOverviewHtml(_cachedRun, _expandedOverviewSteps);
    if (runHtml) html += runHtml;
    html += _renderTaskTableHtml(tasks);
    dynamic.innerHTML = html;

    // Bind event delegation on dynamic container
    dynamic.addEventListener("click", _onDynamicClick);

    container.appendChild(dynamic);

    // ── <details> accordion sections (outside #project-dynamic) ──
    // Pipeline Artifacts (DPS staging) is small → expanded by default.
    // Project Repository (the code repo) can hold hundreds of files → folded
    // by default and lazy-loaded on first expand.
    container.appendChild(_buildWorkspaceDetails("workspace-section-dps", "Pipeline Artifacts", "dps", true));
    container.appendChild(_buildRepoDetails());

    // Attach separate event delegation for repo action buttons
    // (repo-section is outside #project-dynamic, so _onDynamicClick doesn't cover it)
    var _repoSection = document.getElementById("repo-section");
    if (_repoSection) {
      _repoSection.addEventListener("click", function _onRepoClick(event) {
        var actionEl = event.target.closest("[data-action]");
        if (!actionEl) return;
        var action = actionEl.dataset.action;
        switch (action) {
          case "repo-set-remote":
            event.preventDefault(); _actionRepoSetRemote(actionEl); break;
          case "repo-commit":
            event.preventDefault(); _actionRepoCommit(actionEl); break;
          case "repo-push":
            event.preventDefault(); _actionRepoPush(actionEl); break;
          case "repo-pull":
            event.preventDefault(); _actionRepoPull(actionEl); break;
          case "repo-force-sync":
            event.preventDefault(); _actionRepoForceSync(actionEl); break;
          case "repo-make-pr":
            event.preventDefault(); _actionRepoMakePR(actionEl); break;
          case "repo-download":
            // Let the browser navigate naturally
            break;
          case "close-modal":
            event.preventDefault();
            var closeDialog = actionEl.closest("dialog");
            if (closeDialog && typeof closeDialog.close === "function") {
              closeDialog.close();
            }
            break;
        }
      });
    }

    container.appendChild(_buildWorkspaceDetails("workspace-section-code", "Project Repository", "code", false));

    // ── Update reconnect overlay visibility ──
    _updateReconnectOverlay();

    // Record which project this shell was built for, so a stale poll for a
    // different project forces a full rebuild instead of an in-place update.
    container.dataset.renderedPid = _projectId || "";
  }

  /**
   * In-place refresh of the dynamic content (info card + task table) only.
   * Leaves the workspace file trees — and their scroll/expansion/open-file
   * state — untouched. Falls back to a full _render() when the shell is
   * missing or was built for a different project.
   *
   * Preserves feedback textarea values, expanded task rows, and expanded
   * overview steps across the innerHTML swap.
   *
   * @param {object} project — project object from API
   * @param {Array} tasks — task array from API
   * @param {object|null} run — run detail from API
   */
  function _updateDynamic(project, tasks, run) {
    var container = document.getElementById("view-project");
    var dynamic = document.getElementById("project-dynamic");
    if (!container || !dynamic ||
        container.dataset.renderedPid !== (_projectId || "")) {
      _render(project, tasks, run);
      return;
    }

    _cachedProject = project;
    _cachedTasks = tasks || [];
    _cachedRun = run || null;

    // ── Snapshot state BEFORE innerHTML swap ──

    // Expanded task row IDs
    var expandedIds = Object.keys(_expandedTaskRows);

    // Expanded overview steps (deep copy to avoid mutation)
    var expandedSteps = {};
    Object.keys(_expandedOverviewSteps).forEach(function (k) {
      expandedSteps[k] = _expandedOverviewSteps[k];
    });
    _expandedOverviewSteps = {};

    // Feedback textarea state (open rejection box)
    var fbBlock = dynamic.querySelector("#project-checkpoint-card .feedback-block:not(.hidden)");
    var fbState = null;
    if (fbBlock) {
      var ta = fbBlock.querySelector("textarea");
      if (ta) fbState = { step: fbBlock.dataset.step, value: ta.value };
    }

    // ── Rebuild dynamic content ──
    dynamic.innerHTML = "";

    var html = "";
    var cpHtml = _renderCheckpointCardHtml(_cachedCheckpoint, _canWrite());
    if (cpHtml) html += cpHtml;
    html += _renderQuickNavHtml(run != null, tasks && tasks.length > 0);
    html += _renderInfoCardHtml(project, _canWrite());
    var runHtml = _renderRunOverviewHtml(run, expandedSteps);
    if (runHtml) html += runHtml;
    html += _renderTaskTableHtml(tasks);
    dynamic.innerHTML = html;

    // ── Restore state AFTER swap ──

    // Restore feedback if same checkpoint step is still pending
    if (fbState) {
      var newBlock = dynamic.querySelector('.feedback-block[data-step="' + fbState.step + '"]');
      if (newBlock) {
        newBlock.classList.remove("hidden");
        var restoredTa = newBlock.querySelector("textarea");
        if (restoredTa) restoredTa.value = fbState.value;
      }
    }

    // Restore task row expansion
    expandedIds.forEach(function (tid) {
      delete _expandedTaskRows[tid];
      var row = dynamic.querySelector('tr[data-task-id="' + tid + '"]');
      if (row) {
        _toggleTaskDetail(row, isNaN(Number(tid)) ? tid : Number(tid));
      }
    });

    _updateReconnectOverlay();
  }

  /**
   * Single event-delegation click handler on #project-dynamic.
   * Matches event.target.closest('[data-action]') and dispatches via switch.
   * Also detects task row clicks via closest('tr[data-task-id]').
   *
   * @param {Event} event — the click event
   */
  function _onDynamicClick(event) {
    // Check for data-action buttons first
    var actionEl = event.target.closest("[data-action]");
    if (actionEl) {
      var action = actionEl.dataset.action;
      switch (action) {
        case "checkpoint-approve":
          event.preventDefault();
          _handleCheckpointApprove(actionEl.dataset.step, actionEl);
          break;

        case "checkpoint-reject":
          event.preventDefault();
          _toggleCheckpointFeedback(actionEl);
          break;

        case "checkpoint-submit":
          event.preventDefault();
          var cpStep = actionEl.dataset.step;
          var cpCard = actionEl.closest("#project-checkpoint-card");
          var cpFbValue = "";
          if (cpCard) {
            var cpFb = cpCard.querySelector(".feedback-block textarea");
            if (cpFb) cpFbValue = cpFb.value;
          }
          _handleCheckpointReject(cpStep, cpFbValue || "", actionEl);
          break;

        case "checkpoint-review":
          event.preventDefault();
          var cp = _cachedCheckpoint;
          if (cp && window.AItelier && AItelier.CheckpointModal) {
            AItelier.CheckpointModal.show(_projectId, cp);
          }
          break;

        case "checkpoint-chat":
          event.preventDefault();
          var cpRouter = window.AItelier && window.AItelier.Router;
          if (cpRouter && typeof cpRouter.navigate === "function") {
            cpRouter.navigate("#/chat");
          } else {
            window.location.hash = "#/chat";
          }
          break;

        case "project-retry":
          event.preventDefault();
          _handleActionRetry(actionEl);
          break;

        case "project-refresh":
          event.preventDefault();
          _handleActionRefresh(actionEl);
          break;

        case "project-pause":
          event.preventDefault();
          _handleActionPauseResume("paused", actionEl);
          break;

        case "project-resume":
          event.preventDefault();
          _handleActionPauseResume("executing", actionEl);
          break;

        case "project-trace":
          event.preventDefault();
          var tracePid = _projectId;
          var traceRouter = window.AItelier && window.AItelier.Router;
          var traceTarget = "#/projects/" + encodeURIComponent(tracePid) + "/trace";
          if (traceRouter && typeof traceRouter.navigate === "function") {
            traceRouter.navigate(traceTarget);
          } else {
            window.location.hash = traceTarget;
          }
          break;

        case "quick-nav":
          event.preventDefault();
          var targetId = actionEl.dataset.target;
          var targetEl = document.getElementById(targetId);
          if (targetEl) {
            targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
          }
          break;

        case "toggle-overview-step":
          event.preventDefault();
          var stepKey = actionEl.dataset.step;
          if (_expandedOverviewSteps[stepKey]) {
            delete _expandedOverviewSteps[stepKey];
          } else {
            _expandedOverviewSteps[stepKey] = true;
          }
          // Re-render just the run overview portion in place
          if (_cachedRun) {
            var runSection = document.getElementById("run-overview");
            if (runSection) {
              var newOvHtml = _renderRunOverviewHtml(_cachedRun, _expandedOverviewSteps);
              if (newOvHtml) {
                runSection.outerHTML = newOvHtml;
              } else {
                runSection.parentElement.removeChild(runSection);
              }
            }
          }
          break;

        case "repo-download":
          // Let the browser navigate — no preventDefault
          break;

        case "repo-set-remote":
          event.preventDefault();
          _actionRepoSetRemote(actionEl);
          break;

        case "repo-commit":
          event.preventDefault();
          _actionRepoCommit(actionEl);
          break;

        case "repo-push":
          event.preventDefault();
          _actionRepoPush(actionEl);
          break;

        case "repo-pull":
          event.preventDefault();
          _actionRepoPull(actionEl);
          break;

        case "repo-force-sync":
          event.preventDefault();
          _actionRepoForceSync(actionEl);
          break;

        case "repo-make-pr":
          event.preventDefault();
          _actionRepoMakePR(actionEl);
          break;

        case "close-modal":
          event.preventDefault();
          var closeDialog = actionEl.closest("dialog");
          if (closeDialog && typeof closeDialog.close === "function") {
            closeDialog.close();
          }
          break;
      }
      return;
    }

    // Handle task row click
    var taskRow = event.target.closest("tr[data-task-id]");
    if (taskRow) {
      event.preventDefault();
      _toggleTaskDetail(taskRow, taskRow.dataset.taskId);
    }
  }


  // ── Repo action helpers for event delegation ─────────────────────

  function _actionRepoSetRemote(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    var url = window.prompt("Remote URL (origin):", "");
    if (!url) return;
    _repoAction(btn, function () { return api.repoSetRemote(_projectId, url); }, btn.closest(".repo-status-panel"));
  }

  function _actionRepoCommit(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    var msg = window.prompt("Commit message:", "");
    if (!msg) return;
    _repoAction(btn, function () {
      return api.repoCommit(_projectId, msg).then(function (res) {
        if (res && res.committed === false) {
          throw new Error(res.message || "Nothing to commit");
        }
        return res;
      });
    }, btn.closest(".repo-status-panel"));
  }

  function _actionRepoPush(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    _repoAction(btn, function () { return api.repoPush(_projectId); }, btn.closest(".repo-status-panel"));
  }

  function _actionRepoPull(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    _repoAction(btn, function () { return api.repoPull(_projectId); }, btn.closest(".repo-status-panel"));
  }

  function _actionRepoForceSync(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    var branch = window.prompt(
      "Force-sync: fetch and HARD RESET the working tree to origin/<branch>.\n" +
      "Local commits are discarded (a backup branch is created first).\n\n" +
      "Branch to sync from:", "");
    if (!branch) return;
    if (!window.confirm(
      "This DISCARDS uncommitted changes and local commits, resetting to " +
      "origin/" + branch + ".\nA backup/<timestamp> branch is created first. Continue?")) {
      return;
    }
    _repoAction(btn, function () {
      return api.repoSync(_projectId, branch, true, true).then(function (res) {
        if (res && res.backup_branch) {
          window.alert("Synced to origin/" + branch +
            ".\nPrevious state saved on branch: " + res.backup_branch);
        }
        return res;
      });
    }, btn.closest(".repo-status-panel"));
  }

  function _actionRepoMakePR(btn) {
    var api = window.AItelier && window.AItelier.API;
    if (!api) return;
    // Push the current work to a user-named feature branch, then PR it into the
    // base branch. Avoids the "PR head == base == main" dead end.
    var head = window.prompt(
      "Branch name to push your current work to (the PR's source branch):", "");
    if (!head) return;
    var base = window.prompt("Base branch (merge into):", "main");
    if (!base) return;
    if (head === base) {
      window.alert("The feature branch and the base branch must differ.");
      return;
    }
    var title = window.prompt("Pull request title:", "");
    if (!title) return;
    var prBody = window.prompt("PR description (optional):", "") || "";
    _repoAction(btn, function () {
      return api.repoPR(_projectId, {
        title: title, body: prBody, base: base, head: head, push: true,
      }).then(function (res) {
        if (res && res.url) {
          window.alert("Pushed to " + head + " and opened pull request #" +
            res.number + ":\n" + res.url);
          window.open(res.url, "_blank", "noopener");
        }
        return res;
      });
    }, btn.closest(".repo-status-panel"));
  }


  // ════════════════════════════════════════════════════════════════════
  //  <details> Accordion Builders
  // ════════════════════════════════════════════════════════════════════

  /**
   * Build a workspace file-tree section as a native <details> accordion.
   * The tree is fetched lazily on first expand.
   *
   * @param {string} id — DOM element id
   * @param {string} summary — visible heading text
   * @param {string} root — "dps" or "code"
   * @param {boolean} open — start expanded?
   * @returns {HTMLDetailsElement}
   */
  function _buildWorkspaceDetails(id, summary, root, open) {
    var details = document.createElement("details");
    details.id = id;
    details.className = "workspace-details";
    if (open) details.open = true;

    var summ = document.createElement("summary");
    summ.textContent = summary;
    details.appendChild(summ);

    var body = document.createElement("div");
    body.className = "workspace-details-body";
    body.style.padding = "var(--pico-spacing, 1rem)";
    details.appendChild(body);

    // Re-fetch on every expand (not a one-shot latch). A single cached fetch
    // could land on a transient empty/failed moment \u2014 pipeline still writing
    // files, or mid-git-operation \u2014 and then stick empty forever, since polling
    // deliberately leaves the trees untouched. Reloading on each open clears
    // stale content and makes collapse\u2192expand a manual refresh.
    details.addEventListener("toggle", function () {
      if (details.open) {
        body.innerHTML = "";
        var status = document.createElement("p");
        status.className = "empty-state";
        status.textContent = "Loading\u2026";
        body.appendChild(status);
        _fetchWorkspaceTree(body, root);
      }
    });

    return details;
  }

  /**
   * Build the Repository panel as a native <details> accordion.
   * Fetched lazily on first expand.
   *
   * @returns {HTMLDetailsElement}
   */
  function _buildRepoDetails() {
    var details = document.createElement("details");
    details.id = "repo-section";
    details.className = "workspace-details";

    var summ = document.createElement("summary");
    summ.textContent = "Repository";
    details.appendChild(summ);

    var body = document.createElement("div");
    body.className = "workspace-details-body";
    body.style.padding = "var(--pico-spacing, 1rem)";
    details.appendChild(body);

    // Re-fetch on every expand (not a one-shot latch) so a transient failure or
    // a mid-operation snapshot doesn't stick stale \u2014 collapse\u2192expand refreshes.
    details.addEventListener("toggle", function () {
      if (details.open) {
        body.innerHTML = "";
        var status = document.createElement("p");
        status.className = "empty-state";
        status.textContent = "Loading\u2026";
        body.appendChild(status);
        _fetchRepoStatus(body);
      }
    });

    return details;
  }


  // ════════════════════════════════════════════════════════════════════
  //  Checkpoint Action Handlers
  // ════════════════════════════════════════════════════════════════════

  /**
   * Reveal (or hide) the rejection-feedback textarea inside the checkpoint
   * card. The feedback block is pre-rendered with class "hidden" by the
   * template renderer; this function toggles that class.
   *
   * @param {HTMLElement} btn — the "Reject\u2026" button that was clicked
   */
  function _toggleCheckpointFeedback(btn) {
    var card = btn.closest("#project-checkpoint-card");
    if (!card) return;
    var fbBlock = card.querySelector(".feedback-block");
    if (!fbBlock) return;
    fbBlock.classList.toggle("hidden");
    if (!fbBlock.classList.contains("hidden")) {
      var ta = fbBlock.querySelector("textarea");
      if (ta) {
        ta.focus();
        ta.removeAttribute("aria-invalid");
      }
    }
  }

  /**
   * Approve the pending checkpoint, then refresh the view.
   *
   * @param {string} step — checkpoint step ID
   * @param {HTMLButtonElement} btn — the clicked button
   */
  function _handleCheckpointApprove(step, btn) {
    if (!_projectId) {
      return;
    }
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.approveCheckpoint !== "function") {
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Approving\u2026";
    }
    api.approveCheckpoint(_projectId, step, "").then(function () {
      // Clear locally so the card disappears immediately; the next poll
      // confirms from the server.
      _cachedCheckpoint = null;
      _refresh();
    }).catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Approve";
      }
      _showCheckpointError(err, "Approve failed");
    });
  }

  /**
   * Reject the pending checkpoint with feedback, then refresh the view.
   *
   * @param {string} step — checkpoint step ID
   * @param {string} feedback — required rejection reason
   * @param {HTMLButtonElement} btn — the clicked button
   */
  function _handleCheckpointReject(step, feedback, btn) {
    if (!_projectId) {
      return;
    }
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.rejectCheckpoint !== "function") {
      return;
    }
    feedback = (feedback || "").trim();
    if (!feedback) {
      // Focus the textarea if we can find it
      var card = btn && btn.closest("#project-checkpoint-card");
      if (card) {
        var ta = card.querySelector(".feedback-block textarea");
        if (ta) { ta.focus(); ta.setAttribute("aria-invalid", "true"); }
      }
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Submitting\u2026";
    }
    api.rejectCheckpoint(_projectId, step, feedback).then(function () {
      _cachedCheckpoint = null;
      _refresh();
    }).catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Submit rejection";
      }
      _showCheckpointError(err, "Reject failed");
    });
  }

  /** Surface a checkpoint action error via the App error channel. */
  function _showCheckpointError(err, fallback) {
    var msg = (err && err.message) || fallback;
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app && typeof app.showError === "function") {
        app.showError(msg);
      }
    } catch (_e) { /* best-effort */ }
  }


  // ── Action handlers ─────────────────────────────────────────────

  /**
   * Handle a "Retry" button click.
   * Calls API.retryProject() then refreshes.
   *
   * @param {HTMLButtonElement} btn — the clicked button element
   */
  function _handleActionRetry(btn) {
    if (!_projectId) {
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Retrying\u2026";
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.retryProject !== "function") {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Retry";
      }
      return;
    }

    api.retryProject(_projectId).then(function () {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Retry";
      }
      _refresh();
    }).catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Retry";
      }
      var msg = (err && err.message) || "Retry failed";
      try {
        var app = window.AItelier && window.AItelier.App;
        if (app && typeof app.showError === "function") {
          app.showError(msg);
        }
      } catch (_e) {
        // fallback
      }
    });
  }

  /**
   * Handle a "Refresh Planning" button click.
   *
   * @param {HTMLButtonElement} btn — the clicked button element
   */
  function _handleActionRefresh(btn) {
    if (!_projectId) {
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Refreshing\u2026";
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.refreshPlanning !== "function") {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Refresh Planning";
      }
      return;
    }

    api.refreshPlanning(_projectId).then(function () {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Refresh Planning";
      }
      _refresh();
    }).catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Refresh Planning";
      }
      var msg = (err && err.message) || "Refresh failed";
      try {
        var app = window.AItelier && window.AItelier.App;
        if (app && typeof app.showError === "function") {
          app.showError(msg);
        }
      } catch (_e) {
        // fallback
      }
    });
  }

  /**
   * Handle a "Pause" or "Resume" button click.
   *
   * @param {string} newStatus — "paused" or "executing"
   * @param {HTMLButtonElement} btn — the clicked button element
   */
  function _handleActionPauseResume(newStatus, btn) {
    if (!_projectId) {
      return;
    }
    var actionLabel = (newStatus === "paused") ? "Pausing" : "Resuming";

    if (btn) {
      btn.disabled = true;
      btn.textContent = actionLabel + "\u2026";
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.patchProject !== "function") {
      if (btn) {
        btn.disabled = false;
        btn.textContent = (newStatus === "paused") ? "Pause" : "Resume";
      }
      return;
    }

    api.patchProject(_projectId, { status: newStatus }).then(function () {
      if (btn) {
        btn.disabled = false;
        btn.textContent = (newStatus === "paused") ? "Pause" : "Resume";
      }
      _refresh();
    }).catch(function (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = (newStatus === "paused") ? "Pause" : "Resume";
      }
      var msg = (err && err.message) || (actionLabel + " failed");
      try {
        var app = window.AItelier && window.AItelier.App;
        if (app && typeof app.showError === "function") {
          app.showError(msg);
        }
      } catch (_e) {
        // fallback
      }
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  Task Table — _toggleTaskDetail()
  // ════════════════════════════════════════════════════════════════════

  /**
   * Toggle the expanded detail row for a task.
   * Shows the full prompt and completed steps.
   *
   * @param {HTMLTableRowElement} taskRow — the clicked task row
   * @param {number|string} taskId — the task ID
   */
  function _toggleTaskDetail(taskRow, taskId) {
    var isExpanded = !!_expandedTaskRows[taskId];

    // Find existing detail row (next sibling after the task row)
    var detailRow = taskRow.nextElementSibling;
    while (detailRow && detailRow.classList.contains("task-detail-row")) {
      // Remove existing detail row
      detailRow.parentElement.removeChild(detailRow);
      detailRow = taskRow.nextElementSibling;
    }

    if (isExpanded) {
      // Collapse
      delete _expandedTaskRows[taskId];
      return;
    }

    // Expand: insert a detail row after the task row
    var status = taskRow.dataset.taskStatus || "";
    var completedStepsRaw = taskRow.dataset.completedSteps || "[]";
    var completedSteps = [];
    try {
      completedSteps = JSON.parse(completedStepsRaw);
    } catch (_e) {
      completedSteps = [];
    }
    if (!Array.isArray(completedSteps)) { completedSteps = []; }

    var detailTr = document.createElement("tr");
    detailTr.className = "task-detail-row";
    detailTr.style.backgroundColor = "var(--table-row-hover-background-color, rgba(0,0,0,0.02))";

    var detailTd = document.createElement("td");
    detailTd.colSpan = 5;
    detailTd.style.padding = "0.75rem 1rem";
    detailTd.style.fontSize = "0.85rem";

    var content = document.createElement("div");
    content.style.lineHeight = "1.6";

    // Full task prompt (the row cell only shows a short preview)
    var fullPrompt = taskRow.dataset.prompt || "";
    if (fullPrompt) {
      var promptLabel = document.createElement("strong");
      promptLabel.textContent = "Prompt:";
      content.appendChild(promptLabel);

      var promptText = document.createElement("div");
      promptText.style.marginTop = "0.25rem";
      promptText.style.marginBottom = "0.5rem";
      promptText.style.whiteSpace = "pre-wrap";
      promptText.style.wordBreak = "break-word";
      promptText.textContent = fullPrompt;
      content.appendChild(promptText);
    }

    // Completed steps
    if (completedSteps.length > 0) {
      var stepsLabel = document.createElement("strong");
      stepsLabel.textContent = "Completed steps: ";
      content.appendChild(stepsLabel);
      content.appendChild(document.createTextNode(completedSteps.join(" \u2192 ")));
    } else {
      var noSteps = document.createElement("em");
      noSteps.style.color = "var(--muted-color, #888)";
      noSteps.textContent = "No completed steps yet";
      content.appendChild(noSteps);
    }

    // Retry count if failed
    if (status.indexOf("failed") !== -1) {
      var retryLine = document.createElement("div");
      retryLine.style.marginTop = "0.5rem";
      retryLine.style.color = "var(--del-color, #d04040)";
      retryLine.textContent = "Retry count: " + (taskRow.dataset.retryCount || "?");
      content.appendChild(retryLine);
    }

    detailTd.appendChild(content);
    detailTr.appendChild(detailTd);

    // Insert after the task row
    if (taskRow.parentElement) {
      taskRow.parentElement.insertBefore(detailTr, taskRow.nextElementSibling);
    }

    _expandedTaskRows[taskId] = true;
  }


  // ════════════════════════════════════════════════════════════════════
  //  Workspace File Tree
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch a workspace tree from the API and render it as an expandable tree.
   *
   * @param {HTMLElement} section — the workspace section (or body) element
   * @param {string} root — "dps" (pipeline staging) or "code" (project repo)
   */
  function _fetchWorkspaceTree(section, root) {
    if (!_projectId || !section) {
      return;
    }
    root = root || "dps";

    // The loading/empty placeholder is the <p class="empty-state"> we appended
    // to this section in _render(). Scope to the section so the two trees
    // (dps + code) don't fight over a shared element id.
    var loadingEl = section.querySelector("p.empty-state");

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.workspaceTree !== "function") {
      if (loadingEl) {
        loadingEl.textContent = "Workspace browsing not available";
      }
      return;
    }

    api.workspaceTree(_projectId, root).then(function (data) {
      // Clear before injecting so a re-fetch (or two racing toggle events)
      // replaces rather than appends a second tree.
      section.innerHTML = "";

      var tree = (data && data.tree) || [];
      if (tree.length === 0) {
        var empty = document.createElement("p");
        empty.className = "empty-state";
        empty.textContent = root === "code" ? "Repository is empty" : "Workspace is empty";
        section.appendChild(empty);
        return;
      }

      var treeContainer = _renderFileTree(tree, root);
      section.appendChild(treeContainer);
    }).catch(function (/* err */) {
      if (loadingEl) {
        loadingEl.textContent = "Failed to load workspace tree";
      }
    });
  }

  /**
   * Build a nested tree structure from flat file paths.
   *
   * Input: ["dir1/file1.py", "dir1/sub/file2.py", "dir2/file3.py"]
   * Output: { dir1: { _files: ["file1.py"], sub: { _files: ["file2.py"] } },
   *           dir2: { _files: ["file3.py"] } }
   *
   * @param {Array<string>} paths — flat file path strings
   * @returns {object} nested tree with _files arrays at leaves
   */
  function _buildTreeIndices(paths) {
    var tree = {
      _files: [],
    };

    for (var i = 0; i < paths.length; i++) {
      var parts = paths[i].split("/");
      var current = tree;

      for (var j = 0; j < parts.length; j++) {
        var part = parts[j];

        // Last segment → it's a file
        if (j === parts.length - 1) {
          if (current._files.indexOf(part) === -1) {
            current._files.push(part);
          }
        } else {
          // It's a directory
          if (!current.hasOwnProperty(part)) {
            current[part] = { _files: [] };
          }
          current = current[part];
        }
      }
    }

    return tree;
  }

  /**
   * Render a nested tree structure as an expandable <ul>/<li> element.
   *
   * @param {Array<string>} treeArray — flat path array from API
   * @param {string} root — "dps" or "code"
   * @returns {HTMLElement} the tree <ul> element
   */
  function _renderFileTree(treeArray, root) {
    root = root || "dps";
    var nested = _buildTreeIndices(treeArray);

    var treeEl = document.createElement("ul");
    treeEl.id = "workspace-tree-" + root;
    treeEl.style.listStyle = "none";
    treeEl.style.paddingLeft = "0";
    treeEl.style.margin = "0";
    treeEl.style.fontSize = "0.85rem";

    _renderTreeLevel(nested, treeEl, "", root);

    return treeEl;
  }

  /**
   * Recursively render one level of the tree.
   *
   * @param {object} node — tree node with _files and child dirs
   * @param {HTMLElement} parentEl — parent <ul> element
   * @param {string} parentPath — accumulated path prefix for this level
   * @param {string} root — "dps" or "code"
   */
  function _renderTreeLevel(node, parentEl, parentPath, root) {
    if (!node || typeof node !== "object") {
      return;
    }
    root = root || "dps";

    // Collect directory names and sort
    var dirNames = Object.keys(node).filter(function (k) {
      return k !== "_files";
    });
    dirNames.sort();

    // Files first, then directories
    var files = (node._files || []).sort();

    // Render files
    for (var f = 0; f < files.length; f++) {
      var fileName = files[f];
      var fileLi = document.createElement("li");
      fileLi.className = "workspace-file-item";
      fileLi.style.paddingLeft = "0";
      fileLi.style.cursor = "pointer";
      fileLi.style.display = "flex";
      fileLi.style.flexDirection = "row";
      fileLi.style.alignItems = "center";
      fileLi.style.gap = "0.4rem";
      fileLi.style.padding = "0.2rem 0";

      var fileIcon = document.createElement("span");
      fileIcon.className = "file-icon";
      fileIcon.textContent = "\uD83D\uDCC4"; // 📄
      fileIcon.style.fontSize = "0.85rem";
      fileLi.appendChild(fileIcon);

      var fileNameSpan = document.createElement("span");
      fileNameSpan.className = "file-name";
      fileNameSpan.textContent = fileName;
      fileLi.appendChild(fileNameSpan);

      // Click handler for files
      var fullPath = parentPath ? parentPath + "/" + fileName : fileName;
      (function (path) {
        fileLi.addEventListener("click", function (e) {
          e.stopPropagation();
          _showFileContent(_projectId, path, root);
        });
      })(fullPath);

      parentEl.appendChild(fileLi);
    }

    // Render directories
    for (var d = 0; d < dirNames.length; d++) {
      var dirName = dirNames[d];
      var dirPath = parentPath ? parentPath + "/" + dirName : dirName;
      var childNode = node[dirName];

      var dirLi = document.createElement("li");
      dirLi.className = "workspace-file-item";
      dirLi.style.paddingLeft = "0";
      dirLi.style.cursor = "pointer";
      // Column layout: a clickable header row, with the children <ul> stacked
      // BELOW it. A row layout makes the expanded subtree render sideways and
      // clipped (it becomes a flex sibling of the icon/name) — the folder then
      // appears not to expand on click.
      dirLi.style.display = "flex";
      dirLi.style.flexDirection = "column";
      dirLi.style.alignItems = "stretch";
      dirLi.style.padding = "0.1rem 0";

      var dirHeader = document.createElement("div");
      dirHeader.style.display = "flex";
      dirHeader.style.flexDirection = "row";
      dirHeader.style.alignItems = "center";
      dirHeader.style.gap = "0.4rem";
      dirHeader.style.padding = "0.2rem 0";

      var dirIcon = document.createElement("span");
      dirIcon.className = "file-icon";
      dirIcon.textContent = "\uD83D\uDCC1"; // 📁
      dirIcon.style.fontSize = "0.85rem";
      dirHeader.appendChild(dirIcon);

      var dirNameSpan = document.createElement("span");
      dirNameSpan.className = "file-name";
      dirNameSpan.textContent = dirName;
      dirHeader.appendChild(dirNameSpan);

      dirLi.appendChild(dirHeader);
      dirLi.dataset.path = dirPath;

      if (childNode && typeof childNode === "object") {
        var childList = document.createElement("ul");
        childList.style.listStyle = "none";
        childList.style.paddingLeft = "1.2rem";
        childList.style.margin = "0";
        childList.style.display = "none";
        dirLi.appendChild(childList);

        _renderTreeLevel(childNode, childList, dirPath, root);

        // Toggle on click of the header row.
        (function (list) {
          dirHeader.addEventListener("click", function (e) {
            e.stopPropagation();
            list.style.display = list.style.display === "none" ? "block" : "none";
          });
        })(childList);
      }

      parentEl.appendChild(dirLi);
    }
  }


  // ════════════════════════════════════════════════════════════════════
  //  File Content Viewer
  // ════════════════════════════════════════════════════════════════════

  /**
   * Show the content of a file in a modal dialog.
   *
   * @param {string} projectId — project ID
   * @param {string} path — file path relative to workspace root
   * @param {string} root — "dps" or "code"
   */
  function _showFileContent(projectId, path, root) {
    if (!projectId || !path) {
      return;
    }
    root = root || "dps";

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.workspaceFile !== "function") {
      return;
    }

    // Show loading modal immediately
    var existingDialog = document.getElementById("file-content-dialog");
    if (existingDialog) {
      existingDialog.remove();
    }

    var loadingHtml = _renderFileContentModalHtml("Loading\u2026", path);
    var tempContainer = document.createElement("div");
    tempContainer.innerHTML = loadingHtml;
    var dialog = tempContainer.firstElementChild;
    document.body.appendChild(dialog);

    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }

    // Fetch actual content
    api.workspaceFile(projectId, path, root).then(function (data) {
      var content = (data && data.content) || "";
      var contentPath = data.path || path;
      // Replace dialog content in-place
      var titleEl = document.getElementById("file-content-title");
      if (titleEl) { titleEl.textContent = contentPath; }
      var bodyCode = document.getElementById("file-content-code");
      if (bodyCode) {
        var esc = (window.AItelier && window.AItelier.Utils &&
                   typeof window.AItelier.Utils.escapeHtml === "function")
                  ? window.AItelier.Utils.escapeHtml
                  : function (s) { return String(s); };
        bodyCode.textContent = esc(content);
      }
    }).catch(function (/* err */) {
      var bodyEl = document.getElementById("file-content-body");
      if (bodyEl) {
        bodyEl.innerHTML = '<p class="empty-state">Failed to load file content</p>';
      }
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  Utility: repo action (disable, call, refresh, enable)
  // ════════════════════════════════════════════════════════════════════

  /**
   * Shared wrapper for repo action buttons: disable, call API, refresh, re-enable.
   *
   * @param {HTMLElement} btn — the clicked button
   * @param {function} call — returns a Promise for the API call
   * @param {HTMLElement} panel — the repo-status-panel to refresh
   */
  function _repoAction(btn, call, panel) {
    if (!btn || !call) { return; }
    btn.disabled = true;
    var orig = btn.textContent;
    btn.textContent = "\u2026";

    call().then(function () {
      btn.disabled = false;
      btn.textContent = orig;
      if (panel) {
        _fetchRepoStatus(panel);
      }
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = orig;
      var msg = (err && err.message) || "Action failed";
      try {
        var app = window.AItelier && window.AItelier.App;
        if (app && typeof app.showError === "function") {
          app.showError(msg);
        }
      } catch (_e) {
        window.alert(msg);
      }
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  Repo Status — _fetchRepoStatus()
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch repo status for the current project and render the repo-status-panel.
   *
   * @param {HTMLElement} section — the repo section body element to populate
   */
  function _fetchRepoStatus(section) {
    if (!_projectId || !section) {
      return;
    }

    var loadingEl = section.querySelector("p.empty-state");

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.repoStatus !== "function") {
      if (loadingEl) {
        loadingEl.textContent = "Repository browsing not available";
      }
      return;
    }

    api.repoStatus(_projectId).then(function (data) {
      // Clear the section before injecting (not just remove the loading
      // placeholder). A second invocation — a re-expand, or two toggle events
      // racing — would otherwise APPEND a second copy of the panel, duplicating
      // the status + download + pull/push action row. Clearing makes the render
      // idempotent regardless of how many times it runs.
      section.innerHTML = "";

      var html = _renderRepoStatusHtml(data || {}, _canWrite());

      // Create a wrapper to convert HTML string to DOM nodes
      var wrapper = document.createElement("div");
      wrapper.innerHTML = html;
      while (wrapper.firstChild) {
        section.appendChild(wrapper.firstChild);
      }
    }).catch(function (/* err */) {
      if (loadingEl) {
        loadingEl.textContent = "Failed to load repository status";
      }
    });
  }


  // ════════════════════════════════════════════════════════════════════
  //  Reconnect Overlay — _updateReconnectOverlay()
  // ════════════════════════════════════════════════════════════════════

  /**
   * Update the reconnect overlay visibility based on connection state.
   * Shows the overlay when connection is lost during polling, hides when
   * connection is restored.
   */
  function _updateReconnectOverlay() {
    var overlay = document.getElementById("project-reconnect-overlay");
    if (!overlay) { return; }
    overlay.style.display = _isConnectionOk() ? "none" : "block";
  }


  // ════════════════════════════════════════════════════════════════════
  //  Refresh / Polling — _refresh(), show(), hide()
  // ════════════════════════════════════════════════════════════════════

  /**
   * Fetch project data + tasks from the API, then call _render() or
   * _updateDynamic() depending on whether the view shell exists.
   *
   * Skips if _isRefreshing is already true (prevents stacking).
   */
  function _refresh() {
    if (_isRefreshing) {
      return;
    }
    _isRefreshing = true;

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getProject !== "function") {
      _isRefreshing = false;
      return;
    }

    var pid = _projectId;
    if (!pid) {
      _isRefreshing = false;
      return;
    }

    // Fetch project, tasks, and run detail in parallel
    Promise.all([
      api.getProject(pid),
      api.listTasks(pid).catch(function () { return []; }),
      api.getRun(pid).catch(function () { return null; }),
      api.getCheckpoint(pid).catch(function () { return null; }),
    ]).then(function (results) {
      if (!_projectId || _projectId !== pid) {
        // Navigated away; discard results
        _isRefreshing = false;
        return;
      }

      var project = results[0] || null;
      var tasks = results[1] || [];
      var run = results[2] || null;
      _cachedCheckpoint = results[3] || null;

      var dynamic = document.getElementById("project-dynamic");

      // Transient null project (backend mid-tick / brief 404) while a shell is
      // already on screen: keep the last good render. Otherwise _render(null)
      // would clear the container and return early, blanking the WHOLE view —
      // including the artifact/repository accordions — until the next
      // successful poll (the "section randomly missing" symptom).
      if (!project && dynamic) {
        _isRefreshing = false;
        return;
      }

      if (dynamic) {
        _updateDynamic(project, tasks, run);
      } else {
        _render(project, tasks, run);
      }

      _isRefreshing = false;
    }).catch(function () {
      _isRefreshing = false;
    });
  }

  /**
   * Show the project detail view for a given project ID.
   * Starts polling.
   *
   * @param {{id: string}} params — route parameters containing the project ID
   */
  function show(params) {
    _projectId = (params && params.id) || null;
    if (!_projectId) {
      return;
    }

    // Clear previous state
    _expandedTaskRows = {};
    _expandedOverviewSteps = {};
    _expandedDirs = {};

    // Merge manifest labels before first render
    _ensureConfigLabels();

    // Build the static shell (empty project-dynamic container),
    // then the first data fetch triggers a render into it.
    var container = document.getElementById("view-project");
    if (container) {
      // Make #view-project visible — CSS only displays `.active` view sections.
      container.classList.add("active");
      container.innerHTML = "";
      // This wipe just destroyed any previously-rendered shell, INCLUDING the
      // artifact/repository <details> (which only _render() builds). Invalidate
      // the renderedPid marker so the next _refresh does a full _render rather
      // than _updateDynamic — otherwise re-entering the SAME project (renderedPid
      // still matches) takes the lightweight path that never re-appends the trees,
      // so they vanish on the 2nd visit until another project forces a full render.
      delete container.dataset.renderedPid;
      container.innerHTML += '<a href="#/" style="display:inline-block;margin-bottom:var(--pico-spacing,1rem)">\u2190 Back to Dashboard</a>';
      container.innerHTML += '<div id="project-reconnect-overlay" style="display:none;position:relative;text-align:center;padding:2rem 1rem;background-color:rgba(255,255,255,0.85);border-radius:0.5rem;margin-top:1rem">Loading\u2026</div>';
      // Create empty #project-dynamic so _updateDynamic sees it
      var dynamicDiv = document.createElement("div");
      dynamicDiv.id = "project-dynamic";
      container.appendChild(dynamicDiv);
    }

    _refresh();

    // Start polling
    if (_pollTimer) {
      clearInterval(_pollTimer);
    }
    _pollTimer = setInterval(_refresh, _POLL_INTERVAL);
  }

  /**
   * Hide the project detail view and stop polling.
   */
  function hide() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
    var container = document.getElementById("view-project");
    if (container) {
      container.classList.remove("active");
    }
    _projectId = null;
    _cachedProject = null;
    _cachedTasks = [];
    _cachedRun = null;
    _cachedCheckpoint = null;
    _expandedTaskRows = {};
    _expandedOverviewSteps = {};
    _expandedDirs = {};
    _isRefreshing = false;
  }


  // ════════════════════════════════════════════════════════════════════
  //  Public API — export to AItelier.ProjectDetail
  // ════════════════════════════════════════════════════════════════════

  AItelier.ProjectDetail = {
    show: show,
    hide: hide,
    refresh: _refresh,
  };

})();
