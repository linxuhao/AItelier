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

  /** @returns {boolean} true if the current user may perform write actions. */
  function _canWrite() {
    try {
      var api = window.AItelier && window.AItelier.API;
      return (api && typeof api.canWrite === "function") ? api.canWrite() : true;
    } catch (_e) {
      return true;
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


  // ── Render entry point ────────────────────────────────────────────

  /**
   * Fetch project data + tasks, then re-render the full view.
   * Called by _refresh() after fetching data.
   *
   * @param {object} project — project object from API.getProject()
   * @param {Array} tasks — task array from API.listTasks()
   */
  function _render(project, tasks, run) {
    _cachedProject = project;
    _cachedTasks = tasks || [];
    _cachedRun = run || null;

    var container = document.getElementById("view-project");
    if (!container) {
      return;
    }

    // Clear existing content
    container.innerHTML = "";

    // ── Back link ──
    var backLink = document.createElement("a");
    backLink.href = "#/";
    backLink.textContent = "\u2190 Back to Dashboard";
    backLink.style.display = "inline-block";
    backLink.style.marginBottom = "var(--pico-spacing, 1rem)";
    backLink.addEventListener("click", function (e) {
      e.preventDefault();
      try {
        var router = window.AItelier && window.AItelier.Router;
        if (router && typeof router.navigate === "function") {
          router.navigate("#/");
        }
      } catch (_err) {
        window.location.hash = "#/";
      }
    });
    container.appendChild(backLink);

    // ── Reconnect overlay ──
    var reconnectOverlay = document.createElement("div");
    reconnectOverlay.id = "project-reconnect-overlay";
    reconnectOverlay.style.display = "none";
    reconnectOverlay.style.position = "relative";
    reconnectOverlay.style.textAlign = "center";
    reconnectOverlay.style.padding = "2rem 1rem";
    reconnectOverlay.style.backgroundColor = "rgba(255, 255, 255, 0.85)";
    reconnectOverlay.style.borderRadius = "0.5rem";
    reconnectOverlay.style.marginTop = "1rem";
    reconnectOverlay.textContent = "Reconnecting\u2026";
    container.appendChild(reconnectOverlay);

    if (!project) {
      return;
    }

    // Merge config manifest labels so step names render for any config.
    _ensureConfigLabels();

    // ── Dynamic content slot (info card + task table) ──
    // Wrapped in a dedicated container so the polling refresh can update just
    // this region in place — the workspace file trees below are built once and
    // left alone, so a poll never collapses an expanded dir, resets scroll, or
    // refetches the trees mid-interaction (#3).
    var dynamic = document.createElement("div");
    dynamic.id = "project-dynamic";
    dynamic.appendChild(_renderInfoCard(project));
    var overview = _renderRunOverview(run);
    if (overview) {
      dynamic.appendChild(overview);
    }
    if (project.has_task_loop !== false) {
      dynamic.appendChild(_renderTaskTable(tasks));
    }
    container.appendChild(dynamic);

    // ── 3. Workspace File Trees (collapsible) ──
    // Pipeline Artifacts (DPS staging) is small → expanded by default.
    // Project Repository (the code repo) can hold hundreds of files → folded
    // by default and lazy-loaded on first expand.
    container.appendChild(
      _buildWorkspaceSection("", "Pipeline Artifacts", "dps", true));
    container.appendChild(_buildRepoSection());
    container.appendChild(
      _buildWorkspaceSection("-code", "Project Repository", "code", false));

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
   * @param {object} project — project object from API
   * @param {Array} tasks — task array from API
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

    // Remember which task rows are expanded so we can restore them after the
    // table is rebuilt (expansion is otherwise click-only state).
    var expandedIds = Object.keys(_expandedTaskRows);
    // Preserve which loop steps the user expanded in the overview.
    var expandedSteps = _expandedOverviewSteps;
    _expandedOverviewSteps = {};

    dynamic.innerHTML = "";
    dynamic.appendChild(_renderInfoCard(project));
    var overview = _renderRunOverview(run, expandedSteps);
    if (overview) {
      dynamic.appendChild(overview);
    }
    if (project.has_task_loop !== false) {
      dynamic.appendChild(_renderTaskTable(tasks));
    }

    // Restore task-row expansion. _toggleTaskDetail toggles, so clear the flag
    // first (it is still set from before the rebuild) and let toggle re-expand.
    expandedIds.forEach(function (tid) {
      var row = dynamic.querySelector('tr[data-task-id="' + tid + '"]');
      if (row) {
        delete _expandedTaskRows[tid];
        _toggleTaskDetail(row, isNaN(Number(tid)) ? tid : Number(tid));
      } else {
        delete _expandedTaskRows[tid];
      }
    });

    _updateReconnectOverlay();
  }


  // ════════════════════════════════════════════════════════════════════
  //  Run Overview (pipeline stepper)
  // ════════════════════════════════════════════════════════════════════

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
    if (status === "completed") { return "✓"; }       // ✓
    if (status === "failed") { return "✗"; }           // ✗
    if (status === "running" || status === "claimed") { return "▶"; }  // ▶
    return "○";                                         // ○
  }

  /** Render the per-loop-step instance detail panel below the strip. */
  function _renderOverviewPanel(panel, manifest, byStep) {
    panel.innerHTML = "";
    Object.keys(_expandedOverviewSteps).forEach(function (stepId) {
      var insts = byStep[stepId] || [];
      if (!insts.length) { return; }
      var block = document.createElement("div");
      block.className = "run-detail-block";
      var h = document.createElement("div");
      h.className = "run-detail-title";
      h.textContent = ((manifest.labels && manifest.labels[stepId]) || stepId) +
        " — " + insts.length + " run(s)";
      block.appendChild(h);
      insts.forEach(function (s, i) {
        var row = document.createElement("div");
        row.className = "run-inst";
        var rr = (s.retry_count || 0) + (s.validation_retry_count || 0);
        row.textContent = "#" + (i + 1) + "  " + _statusGlyph(s.status) + " " + s.status +
          (rr ? ("  ↻" + rr + " retr" + (rr === 1 ? "y" : "ies")) : "");
        if (s.error) { row.title = s.error; }
        block.appendChild(row);
      });
      panel.appendChild(block);
    });
  }

  /**
   * Render the pipeline overview stepper: every manifest step in order, marked
   * done / active / future / failed, with retry (↻) and loop (×N) badges. Loop
   * steps are clickable to reveal per-instance status in a panel below.
   *
   * @param {object} run — run detail (manifest + step instances) from getRun
   * @param {object} [expandedSteps] — loop steps to keep expanded across refresh
   * @returns {HTMLElement|null}
   */
  function _renderRunOverview(run, expandedSteps) {
    if (!run || !run.manifest || !Array.isArray(run.manifest.steps) ||
        run.manifest.steps.length === 0) {
      return null;
    }
    // Seed expansion state from the preserved snapshot (poll refresh).
    if (expandedSteps) {
      Object.keys(expandedSteps).forEach(function (k) { _expandedOverviewSteps[k] = true; });
    }

    var manifest = run.manifest;
    var byStep = {};
    (run.steps || []).forEach(function (s) {
      (byStep[s.step_id] = byStep[s.step_id] || []).push(s);
    });

    var section = document.createElement("section");
    section.id = "run-overview";
    section.className = "run-overview";

    // Header with overall progress. Count only steps that actually ran (have
    // instances) so a finished run that skipped a conditional step (e.g.
    // git_sync_pre on a new repo) reads N/N, not N/total.
    var terminal = run.status === "completed" || run.status === "failed";
    var done = 0, ran = 0, skipped = 0;
    manifest.steps.forEach(function (id) {
      var insts = byStep[id] || [];
      if (!insts.length) { if (terminal) { skipped++; } return; }
      ran++;
      if (insts.every(function (s) { return s.status === "completed"; })) { done++; }
    });
    var head = document.createElement("div");
    head.className = "run-overview-head";
    var title = document.createElement("strong");
    title.textContent = "Pipeline";
    head.appendChild(title);
    var prog = document.createElement("span");
    prog.className = "run-overview-progress";
    // On a live run, future steps still count toward the total; on a finished
    // run, skipped steps are excluded from the denominator and noted separately.
    var denom = terminal ? ran : manifest.steps.length;
    prog.textContent = done + "/" + denom + " steps" +
      (run.status ? " · " + run.status : "") +
      (skipped ? " · " + skipped + " skipped" : "");
    head.appendChild(prog);
    section.appendChild(head);

    var strip = document.createElement("div");
    strip.className = "run-strip";
    var panel = document.createElement("div");
    panel.className = "run-detail-panel";

    manifest.steps.forEach(function (stepId) {
      var insts = byStep[stepId] || [];
      var label = (manifest.labels && manifest.labels[stepId]) || stepId;
      var st = _aggStepStatus(insts, terminal);
      var retries = insts.reduce(function (a, s) {
        return a + (s.retry_count || 0) + (s.validation_retry_count || 0);
      }, 0);
      var isCheckpoint = manifest.checkpoints &&
        Object.prototype.hasOwnProperty.call(manifest.checkpoints, stepId);

      var pill = document.createElement("div");
      pill.className = "run-step run-step-" + st;

      var lbl = document.createElement("span");
      lbl.className = "run-step-label";
      lbl.textContent = (isCheckpoint ? "⏸ " : "") + label;
      pill.appendChild(lbl);

      if (insts.length > 1) {
        var loopBadge = document.createElement("span");
        loopBadge.className = "run-step-badge run-step-loop";
        loopBadge.textContent = "×" + insts.length;
        pill.appendChild(loopBadge);
      }
      if (retries > 0) {
        var rb = document.createElement("span");
        rb.className = "run-step-badge run-step-retry";
        rb.textContent = "↻" + retries;
        rb.title = retries + " retr" + (retries === 1 ? "y" : "ies");
        pill.appendChild(rb);
      }

      if (insts.length > 1) {
        pill.classList.add("run-step-clickable");
        pill.title = "Click to show per-task status";
        pill.addEventListener("click", function () {
          if (_expandedOverviewSteps[stepId]) {
            delete _expandedOverviewSteps[stepId];
          } else {
            _expandedOverviewSteps[stepId] = true;
          }
          _renderOverviewPanel(panel, manifest, byStep);
        });
      } else if (insts.length === 1 && insts[0].error) {
        pill.title = insts[0].error;
      }

      strip.appendChild(pill);
    });

    section.appendChild(strip);
    _renderOverviewPanel(panel, manifest, byStep);
    section.appendChild(panel);
    return section;
  }


  // ════════════════════════════════════════════════════════════════════
  //  1.  Project Info Card
  // ════════════════════════════════════════════════════════════════════

  /**
   * Render the project info card section.
   *
   * @param {object} project — project object from API
   * @returns {HTMLElement} the info card element
   */
  function _renderInfoCard(project) {
    var card = document.createElement("section");
    card.style.marginBottom = "var(--pico-spacing, 1rem)";
    card.style.padding = "var(--pico-spacing, 1rem)";
    card.style.border = "1px solid var(--muted-border-color, #e0e0e0)";
    card.style.borderRadius = "0.5rem";

    // ── Project name ──
    var nameEl = document.createElement("h3");
    nameEl.textContent = project.name || project.project_id || "";
    nameEl.style.margin = "0 0 0.5rem 0";
    card.appendChild(nameEl);

    // ── Status badge ──
    var status = project.status || "planning";
    var parsed = _parseStatus(status);
    var badge = document.createElement("span");
    badge.className = "status-badge";
    if (parsed.className) {
      badge.classList.add(parsed.className);
    }
    badge.textContent = parsed.icon + " " + parsed.text;
    badge.style.display = "inline-block";
    badge.style.marginBottom = "0.5rem";
    card.appendChild(badge);

    // ── Current step ──
    var step = project.current_project_step || "";
    if (step) {
      var stepLabel = _STEP_LABELS.hasOwnProperty(step) ? _STEP_LABELS[step] : step;
      var stepEl = document.createElement("p");
      stepEl.style.margin = "0.25rem 0";
      stepEl.style.fontSize = "0.85rem";
      stepEl.style.color = "var(--muted-color, #888)";
      stepEl.textContent = "Current step: " + stepLabel;
      card.appendChild(stepEl);
    }

    // ── Brief description (first 200 chars) ──
    var brief = project.brief || "";
    if (brief) {
      var briefEl = document.createElement("p");
      briefEl.style.margin = "0.5rem 0";
      briefEl.style.fontSize = "0.9rem";
      briefEl.style.lineHeight = "1.5";
      briefEl.textContent = (function () {
        try {
          var utils = window.AItelier && window.AItelier.Utils;
          if (utils && typeof utils.truncate === "function") {
            return utils.truncate(brief, 200);
          }
        } catch (_e) {
          // fallthrough
        }
        return brief.length > 200 ? brief.slice(0, 200) + "\u2026" : brief;
      })();
      card.appendChild(briefEl);
    }

    // ── Action buttons ──
    var btnRow = document.createElement("div");
    btnRow.style.display = "flex";
    btnRow.style.flexDirection = "row";
    btnRow.style.gap = "0.5rem";
    btnRow.style.flexWrap = "wrap";
    btnRow.style.marginTop = "0.75rem";

    var writable = _canWrite();

    // Retry — only shown when status contains "failed"
    var isFailed = status.indexOf("failed") !== -1;

    if (isFailed && writable) {
      var retryBtn = document.createElement("button");
      retryBtn.id = "btn-project-retry";
      retryBtn.textContent = "Retry";
      retryBtn.className = "outline";
      retryBtn.addEventListener("click", function () {
        _handleActionRetry(this);
      });
      btnRow.appendChild(retryBtn);
    }

    // Refresh Planning — write action, shown only to writers
    if (writable) {
      var refreshBtn = document.createElement("button");
      refreshBtn.id = "btn-project-refresh";
      refreshBtn.textContent = "Refresh Planning";
      refreshBtn.className = "outline";
      refreshBtn.addEventListener("click", function () {
        _handleActionRefresh(this);
      });
      btnRow.appendChild(refreshBtn);
    }

    // View Traces — open the execution-trace view for this project
    var traceBtn = document.createElement("button");
    traceBtn.id = "btn-project-trace";
    traceBtn.textContent = "View Traces";
    traceBtn.className = "outline";
    traceBtn.addEventListener("click", function () {
      var pid = _projectId;
      var router = window.AItelier && window.AItelier.Router;
      var target = "#/projects/" + encodeURIComponent(pid) + "/trace";
      if (router && typeof router.navigate === "function") {
        router.navigate(target);
      } else {
        window.location.hash = target;
      }
    });
    btnRow.appendChild(traceBtn);

    // Pause / Resume — toggle based on status
    var isPaused = status.indexOf("paused") !== -1;
    var isRunning = status.indexOf("running") !== -1 ||
                    status.indexOf("advancing") !== -1 ||
                    status.indexOf("planning") !== -1 ||
                    status.indexOf("executing") !== -1;

    if (isPaused && writable) {
      var resumeBtn = document.createElement("button");
      resumeBtn.id = "btn-project-resume";
      resumeBtn.textContent = "Resume";
      resumeBtn.className = "outline";
      resumeBtn.addEventListener("click", function () {
        _handleActionPauseResume("executing", this);
      });
      btnRow.appendChild(resumeBtn);
    } else if (isRunning && writable) {
      var pauseBtn = document.createElement("button");
      pauseBtn.id = "btn-project-pause";
      pauseBtn.textContent = "Pause";
      pauseBtn.className = "outline";
      pauseBtn.addEventListener("click", function () {
        _handleActionPauseResume("paused", this);
      });
      btnRow.appendChild(pauseBtn);
    }

    card.appendChild(btnRow);
    return card;
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
  //  2.  Task List Table
  // ════════════════════════════════════════════════════════════════════

  /**
   * Render the task list table section.
   *
   * @param {Array} tasks — array of task objects from API
   * @returns {HTMLElement} the task section element
   */
  function _renderTaskTable(tasks) {
    var section = document.createElement("section");
    section.id = "project-tasks";
    section.style.marginTop = "var(--pico-spacing, 1rem)";
    section.style.marginBottom = "var(--pico-spacing, 1rem)";

    var title = document.createElement("h4");
    title.textContent = "Tasks";
    section.appendChild(title);

    // Empty state
    if (!tasks || tasks.length === 0) {
      var emptyMsg = document.createElement("p");
      emptyMsg.className = "empty-state";
      emptyMsg.textContent = "No tasks yet \u2014 type in chat to add tasks";
      section.appendChild(emptyMsg);
      return section;
    }

    // Table
    var table = document.createElement("table");
    table.style.width = "100%";

    // thead
    var thead = document.createElement("thead");
    var headerTr = document.createElement("tr");
    var headers = ["#", "Task ID", "Status", "Step", "Prompt"];
    for (var h = 0; h < headers.length; h++) {
      var th = document.createElement("th");
      th.textContent = headers[h];
      if (h === 0) { th.className = "col-idx"; }
      headerTr.appendChild(th);
    }
    thead.appendChild(headerTr);
    table.appendChild(thead);

    // tbody
    var tbody = document.createElement("tbody");
    tbody.id = "task-tbody";

    for (var i = 0; i < tasks.length; i++) {
      var row = _createTaskRow(tasks[i], i + 1);
      if (row) {
        tbody.appendChild(row);
      }
    }

    table.appendChild(tbody);
    section.appendChild(table);

    return section;
  }

  /**
   * Create a task table row by cloning the #tpl-task-row template.
   *
   * @param {object} task — task object from API
   * @param {number} index — 1-based row index
   * @returns {HTMLTableRowElement|null}
   */
  function _createTaskRow(task, index) {
    var template = document.getElementById("tpl-task-row");
    if (!template) {
      return null;
    }

    var row = template.content.cloneNode(true).firstElementChild;
    if (!row) {
      return null;
    }

    var cells = row.children;
    if (cells.length < 5) {
      return null;
    }

    var taskId = task.id || "-";

    // # column
    cells[0].textContent = String(index);

    // Task ID
    cells[1].textContent = String(taskId);

    // Status badge
    var status = task.status || "pending";
    var parsed = _parseStatus(status);
    var badge = cells[2].querySelector("span");
    if (badge) {
      badge.textContent = parsed.icon + " " + parsed.text;
      badge.className = "status-badge";
      if (parsed.className) {
        badge.classList.add(parsed.className);
      }
    } else {
      cells[2].textContent = parsed.icon + " " + parsed.text;
    }

    // Current step
    var step = task.current_step || task.current_project_step || "";
    cells[3].textContent = step || "-";

    // Prompt (truncated to 60 chars)
    var prompt = task.prompt || "";
    cells[4].textContent = (function () {
      try {
        var utils = window.AItelier && window.AItelier.Utils;
        if (utils && typeof utils.truncate === "function") {
          return utils.truncate(prompt, 60);
        }
      } catch (_e) {
        // fallthrough
      }
      return prompt.length > 60 ? prompt.slice(0, 60) + "\u2026" : prompt;
    })();
    cells[4].className = "task-prompt";

    // Attach task data for click-to-expand
    row.dataset.taskId = String(taskId);
    row.dataset.taskStatus = status || "";
    row.dataset.completedSteps = task.completed_steps || "[]";
    // Stash the full (untruncated) prompt so the detail row can show it — the
    // cell above only holds the 60-char preview.
    row.dataset.prompt = prompt;

    // ── Click handler: toggle detail row ──
    (function (tr, tid) {
      tr.addEventListener("click", function (e) {
        // Only toggle on row click, not on child button clicks
        if (e.target && e.target.tagName === "BUTTON") {
          return;
        }
        _toggleTaskDetail(tr, tid);
      });
    })(row, taskId);

    return row;
  }

  /**
   * Toggle the expanded detail row for a task.
   * Shows completed steps and retry count.
   *
   * @param {HTMLTableRowElement} taskRow — the clicked task row
   * @param {number} taskId — the task ID
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

    var detailTr = document.createElement("tr");
    detailTr.className = "task-detail-row";
    detailTr.style.backgroundColor = "var(--table-row-hover-background-color, rgba(0,0,0,0.02))";

    var detailTd = document.createElement("td");
    detailTd.colSpan = 5;
    detailTd.style.padding = "0.75rem 1rem";
    detailTd.style.fontSize = "0.85rem";

    var content = document.createElement("div");
    content.style.lineHeight = "1.6";

    // Full task prompt (the row cell only shows a 60-char preview)
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
  //  3.  Workspace File Tree
  // ════════════════════════════════════════════════════════════════════

  /**
   * Build the read-only Repository panel: branch, working-tree state,
   * ahead/behind vs upstream, remote URL, recent commits, and a Download ZIP
   * action. All read-only — available to every user. The git snapshot is
   * fetched lazily on first expand so the project view stays cheap.
   *
   * @returns {HTMLElement} the section element
   */
  function _buildRepoSection() {
    var section = document.createElement("div");
    section.id = "repo-section";
    section.style.marginTop = "var(--pico-spacing, 1rem)";

    var header = document.createElement("h4");
    header.style.cursor = "pointer";
    header.style.userSelect = "none";
    var caret = document.createElement("span");
    caret.textContent = "▸ ";
    header.appendChild(caret);
    header.appendChild(document.createTextNode("Repository"));
    section.appendChild(header);

    var body = document.createElement("div");
    body.style.display = "none";
    section.appendChild(body);

    var loaded = false;
    function _loadIfNeeded() {
      if (loaded) { return; }
      loaded = true;
      var loading = document.createElement("p");
      loading.className = "empty-state";
      loading.textContent = "Loading…";
      body.appendChild(loading);
      _fetchRepoStatus(body);
    }

    header.addEventListener("click", function () {
      var isOpen = body.style.display !== "none";
      body.style.display = isOpen ? "none" : "block";
      caret.textContent = isOpen ? "▸ " : "▾ ";
      if (!isOpen) { _loadIfNeeded(); }
    });

    return section;
  }

  /**
   * Fetch repo status into the given panel body and render it.
   *
   * @param {HTMLElement} body — the repo section body element
   */
  function _fetchRepoStatus(body) {
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.repoStatus !== "function") {
      body.innerHTML = "";
      var na = document.createElement("p");
      na.className = "empty-state";
      na.textContent = "Repository status not available";
      body.appendChild(na);
      return;
    }

    api.repoStatus(_projectId).then(function (data) {
      body.innerHTML = "";
      body.appendChild(_renderRepoStatus(data || {}, body));
    }).catch(function () {
      body.innerHTML = "";
      var err = document.createElement("p");
      err.className = "empty-state";
      err.textContent = "Failed to load repository status";
      body.appendChild(err);
    });
  }

  /** A small "label: value" line for the repo panel. */
  function _repoLine(label, value) {
    var p = document.createElement("p");
    p.style.margin = "0.2rem 0";
    p.style.fontSize = "0.85rem";
    var strong = document.createElement("strong");
    strong.textContent = label + ": ";
    p.appendChild(strong);
    p.appendChild(document.createTextNode(value));
    return p;
  }

  /** Surface a repo action error via the app's error toast. */
  function _repoError(err) {
    var msg = (err && err.message) || "Repository action failed";
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app && typeof app.showError === "function") { app.showError(msg); }
      else { window.alert(msg); }
    } catch (_e) { /* swallow */ }
  }

  /**
   * Run a repo write action: disable the button, await the promise, then
   * re-render the whole panel (which re-enables controls and reflects new
   * state). Errors surface via the toast and re-enable the button.
   *
   * @param {HTMLButtonElement} btn — the clicked button
   * @param {function():Promise} factory — produces the action promise
   * @param {HTMLElement} panelBody — repo panel body to refresh on success
   * @param {function(object)} [onOk] — optional success callback (gets result)
   */
  function _repoAction(btn, factory, panelBody, onOk) {
    if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
    factory().then(function (res) {
      if (onOk) { try { onOk(res); } catch (_e) { /* ignore */ } }
      _fetchRepoStatus(panelBody);
    }).catch(function (err) {
      if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); }
      _repoError(err);
    });
  }

  /** Build the write-actions row for the repo panel (writers only). */
  function _renderRepoActions(data, panelBody) {
    var api = window.AItelier && window.AItelier.API;
    var pid = _projectId;
    var row = document.createElement("div");
    row.style.display = "flex";
    row.style.flexWrap = "wrap";
    row.style.gap = "0.5rem";
    row.style.marginTop = "0.75rem";
    row.style.paddingTop = "0.75rem";
    row.style.borderTop = "1px solid var(--muted-border-color, #e0e0e0)";

    function mkBtn(label, handler) {
      var b = document.createElement("button");
      b.textContent = label;
      b.className = "outline";
      b.style.fontSize = "0.8rem";
      b.style.margin = "0";
      b.addEventListener("click", function () { handler(b); });
      row.appendChild(b);
      return b;
    }

    mkBtn(data.remote_url ? "Set Remote" : "Add Remote", function (b) {
      var url = window.prompt("Remote URL (origin):", data.remote_url || "");
      if (!url) { return; }
      _repoAction(b, function () { return api.repoSetRemote(pid, url); }, panelBody);
    });

    mkBtn("Commit", function (b) {
      var msg = window.prompt("Commit message:", "");
      if (!msg) { return; }
      _repoAction(b, function () { return api.repoCommit(pid, msg); }, panelBody,
        function (res) { if (res && res.committed === false) { _repoError({ message: res.message }); } });
    });

    mkBtn("Push", function (b) {
      _repoAction(b, function () { return api.repoPush(pid); }, panelBody);
    });

    mkBtn("Pull", function (b) {
      _repoAction(b, function () { return api.repoPull(pid); }, panelBody);
    });

    mkBtn("Force Sync", function (b) {
      var branch = window.prompt(
        "Force-sync: fetch and HARD RESET the working tree to origin/<branch>.\n" +
        "Local commits are discarded (a backup branch is created first).\n\n" +
        "Branch to sync from:", data.branch || "main");
      if (!branch) { return; }
      if (!window.confirm(
        "This DISCARDS uncommitted changes and local commits, resetting to " +
        "origin/" + branch + ".\nA backup/<timestamp> branch is created first. Continue?")) {
        return;
      }
      _repoAction(b, function () {
        return api.repoSync(pid, branch, true, true);
      }, panelBody, function (res) {
        if (res && res.backup_branch) {
          window.alert("Synced to origin/" + branch +
            ".\nPrevious state saved on branch: " + res.backup_branch);
        }
      });
    });

    mkBtn("Make PR", function (b) {
      var title = window.prompt("Pull request title:", "");
      if (!title) { return; }
      var base = window.prompt("Base branch (merge into):", "main");
      if (!base) { return; }
      var prBody = window.prompt("PR description (optional):", "") || "";
      _repoAction(b, function () {
        return api.repoPR(pid, { title: title, body: prBody, base: base });
      }, panelBody, function (res) {
        if (res && res.url) {
          window.alert("Pull request #" + res.number + " created:\n" + res.url);
          window.open(res.url, "_blank", "noopener");
        }
      });
    });

    return row;
  }

  /**
   * Render a repo status payload into a DOM fragment.
   *
   * @param {object} data — payload from API.repoStatus
   * @param {HTMLElement} [panelBody] — panel body, for action refresh
   * @returns {HTMLElement}
   */
  function _renderRepoStatus(data, panelBody) {
    var wrap = document.createElement("div");

    // Download ZIP is available regardless of git state (read action).
    var actions = document.createElement("div");
    actions.style.margin = "0 0 0.75rem 0";
    var api = window.AItelier && window.AItelier.API;
    var dl = document.createElement("a");
    dl.textContent = "⬇ Download ZIP";
    dl.setAttribute("role", "button");
    dl.className = "outline";
    dl.style.fontSize = "0.85rem";
    dl.setAttribute("download", "");
    if (api && typeof api.repoArchiveUrl === "function") {
      dl.href = api.repoArchiveUrl(_projectId);
    }
    actions.appendChild(dl);
    wrap.appendChild(actions);

    if (!data.is_git) {
      wrap.appendChild(_repoLine("Status", "Not a git repository"));
      if (data.path) { wrap.appendChild(_repoLine("Path", data.path)); }
      return wrap;
    }

    var writable = _canWrite();

    if (data.branch) { wrap.appendChild(_repoLine("Branch", data.branch)); }

    // Working-tree state badge.
    var stateLine = document.createElement("p");
    stateLine.style.margin = "0.2rem 0";
    stateLine.style.fontSize = "0.85rem";
    var stateStrong = document.createElement("strong");
    stateStrong.textContent = "Working tree: ";
    stateLine.appendChild(stateStrong);
    var stateBadge = document.createElement("span");
    stateBadge.className = "status-badge";
    if (data.dirty) {
      stateBadge.classList.add("status-warn");
      stateBadge.textContent = "✗ " + (data.dirty_count || 0) + " uncommitted change(s)";
    } else {
      stateBadge.classList.add("status-ok");
      stateBadge.textContent = "✓ clean";
    }
    stateLine.appendChild(stateBadge);
    wrap.appendChild(stateLine);

    // Remote + upstream sync state.
    wrap.appendChild(_repoLine("Remote", data.remote_url || "— none configured"));
    if (data.upstream && (data.ahead != null || data.behind != null)) {
      wrap.appendChild(_repoLine(
        "Sync",
        (data.ahead || 0) + " ahead, " + (data.behind || 0) +
        " behind " + data.upstream));
    } else {
      wrap.appendChild(_repoLine("Sync", "no upstream tracking branch"));
    }

    // Recent commits.
    var commits = data.commits || [];
    var commitsHead = document.createElement("p");
    commitsHead.style.margin = "0.6rem 0 0.2rem 0";
    commitsHead.style.fontSize = "0.85rem";
    var ch = document.createElement("strong");
    ch.textContent = "Recent commits:";
    commitsHead.appendChild(ch);
    wrap.appendChild(commitsHead);

    if (commits.length === 0) {
      var noCommits = document.createElement("p");
      noCommits.className = "empty-state";
      noCommits.textContent = "No commits yet";
      wrap.appendChild(noCommits);
    } else {
      var list = document.createElement("ul");
      list.style.listStyle = "none";
      list.style.paddingLeft = "0";
      list.style.margin = "0";
      list.style.fontSize = "0.8rem";
      commits.forEach(function (c) {
        var li = document.createElement("li");
        li.style.padding = "0.15rem 0";
        li.style.borderBottom = "1px solid var(--muted-border-color, #eee)";
        var hash = document.createElement("code");
        hash.textContent = c.hash;
        hash.style.marginRight = "0.5rem";
        li.appendChild(hash);
        li.appendChild(document.createTextNode(c.subject || ""));
        var meta = document.createElement("span");
        meta.style.color = "var(--muted-color, #888)";
        meta.style.marginLeft = "0.5rem";
        meta.textContent = "— " + (c.author || "") +
          (c.date ? ", " + c.date.slice(0, 10) : "");
        li.appendChild(meta);
        list.appendChild(li);
      });
      wrap.appendChild(list);
    }

    // Write actions (remote / commit / push / pull / force-sync / PR) — only
    // for users with write permission and only when we have a panel body to
    // refresh into after an action completes.
    if (writable && panelBody) {
      wrap.appendChild(_renderRepoActions(data, panelBody));
    }

    return wrap;
  }

  /**
   * Build a collapsible workspace section with a clickable header.
   *
   * The tree is fetched into the section body. When expanded === false the
   * section starts folded and the (potentially large) tree is fetched lazily
   * on first expand, so the project view doesn't pull hundreds of repo files
   * on every render.
   *
   * @param {string} idSuffix — appended to "workspace-section" for the id
   * @param {string} title — section heading text
   * @param {string} root — "dps" or "code"
   * @param {boolean} expanded — start expanded (and fetch immediately)?
   * @returns {HTMLElement} the section element
   */
  function _buildWorkspaceSection(idSuffix, title, root, expanded) {
    var section = document.createElement("div");
    section.id = "workspace-section" + idSuffix;
    section.style.marginTop = "var(--pico-spacing, 1rem)";

    var header = document.createElement("h4");
    header.style.cursor = "pointer";
    header.style.userSelect = "none";
    var caret = document.createElement("span");
    caret.textContent = expanded ? "▾ " : "▸ ";  // ▾ / ▸
    header.appendChild(caret);
    header.appendChild(document.createTextNode(title));
    section.appendChild(header);

    var body = document.createElement("div");
    body.className = "workspace-body";
    body.style.display = expanded ? "block" : "none";
    section.appendChild(body);

    var loaded = false;
    function _loadIfNeeded() {
      if (loaded) { return; }
      loaded = true;
      var status = document.createElement("p");
      status.className = "empty-state";
      status.textContent = "Loading…";
      body.appendChild(status);
      _fetchWorkspaceTree(body, root);
    }

    header.addEventListener("click", function () {
      var isOpen = body.style.display !== "none";
      body.style.display = isOpen ? "none" : "block";
      caret.textContent = isOpen ? "▸ " : "▾ ";
      if (!isOpen) { _loadIfNeeded(); }
    });

    if (expanded) { _loadIfNeeded(); }
    return section;
  }

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
      if (loadingEl && loadingEl.parentElement) {
        loadingEl.parentElement.removeChild(loadingEl);
      }

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
      dirLi.style.display = "flex";
      dirLi.style.flexDirection = "column";
      dirLi.style.alignItems = "stretch";
      dirLi.style.padding = "0.1rem 0";

      // Directory header (clickable)
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
      dirNameSpan.textContent = dirName + "/";
      dirNameSpan.style.fontWeight = "600";
      dirHeader.appendChild(dirNameSpan);

      dirLi.appendChild(dirHeader);

      // Children container (hidden by default)
      var childrenUl = document.createElement("ul");
      childrenUl.style.listStyle = "none";
      childrenUl.style.paddingLeft = "1rem";
      childrenUl.style.margin = "0";
      childrenUl.style.display = "none";
      childrenUl.dataset.dirPath = dirPath;
      childrenUl.dataset.expanded = "false";
      dirLi.appendChild(childrenUl);

      // Click handler: toggle directory expansion.
      // childNode MUST be captured by the IIFE — it is a `var` (function-scoped)
      // reassigned every loop iteration, so referencing it free would make every
      // directory's handler see the LAST directory's node (all subfolders would
      // then render the same files).
      (function (dp, chUl, node) {
        dirHeader.addEventListener("click", function (e) {
          e.stopPropagation();
          _toggleDir(dp, chUl, node, root);
        });
      })(dirPath, childrenUl, childNode);

      parentEl.appendChild(dirLi);
    }
  }

  /**
   * Toggle expansion of a directory node.
   * On first expand, renders children. On subsequent toggles, shows/hides.
   *
   * @param {string} dirPath — relative path to the directory
   * @param {HTMLElement} childrenEl — the <ul> element for children
   * @param {object} childNode — tree node data with _files and subdirs
   * @param {string} root — "dps" or "code" (forwarded to child file clicks)
   */
  function _toggleDir(dirPath, childrenEl, childNode, root) {
    var isExpanded = childrenEl.dataset.expanded === "true";

    if (isExpanded) {
      // Collapse: hide children
      childrenEl.style.display = "none";
      childrenEl.dataset.expanded = "false";
      delete _expandedDirs[dirPath];
      return;
    }

    // Expand. The initial tree fetch already returned the full file list and
    // _buildTreeIndices built the complete nested structure, so childNode holds
    // everything we need — render straight from it (no extra API round-trip).
    if (childrenEl.children.length === 0) {
      _renderTreeLevel(childNode, childrenEl, dirPath, root);
    }
    childrenEl.style.display = "block";
    childrenEl.dataset.expanded = "true";
    _expandedDirs[dirPath] = true;
  }

  /**
   * Show file content in a modal/dialog.
   * Creates a <dialog> element dynamically if one doesn't exist.
   *
   * @param {string} pid — project ID
   * @param {string} filePath — relative file path within workspace
   * @param {string} [root] — "dps" (default) or "code" (project repo)
   */
  function _showFileContent(pid, filePath, root) {
    if (!pid || !filePath) {
      return;
    }
    root = root || "dps";

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.workspaceFile !== "function") {
      return;
    }

    // Find or create the file content dialog
    var dialog = document.getElementById("file-content-dialog");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "file-content-dialog";
      dialog.style.maxWidth = "80vw";
      dialog.style.maxHeight = "80vh";
      dialog.style.width = "800px";
      dialog.style.padding = "0";
      dialog.style.border = "none";
      dialog.style.borderRadius = "0.5rem";
      dialog.style.boxShadow = "0 8px 32px rgba(0, 0, 0, 0.3)";

      var article = document.createElement("article");
      article.style.margin = "0";
      article.style.padding = "var(--pico-spacing, 1rem)";
      article.style.display = "flex";
      article.style.flexDirection = "column";
      article.style.maxHeight = "80vh";

      // Header
      var header = document.createElement("header");
      header.style.flexShrink = "0";
      header.style.paddingBottom = "var(--pico-spacing, 1rem)";

      var titleEl = document.createElement("h3");
      titleEl.id = "file-content-title";
      titleEl.style.margin = "0";
      titleEl.textContent = filePath;
      header.appendChild(titleEl);

      article.appendChild(header);

      // Content area
      var contentDiv = document.createElement("div");
      contentDiv.id = "file-content-body";
      contentDiv.style.flex = "1";
      contentDiv.style.overflowY = "auto";
      contentDiv.style.maxHeight = "55vh";
      contentDiv.style.padding = "0 0 var(--pico-spacing, 1rem) 0";
      contentDiv.style.whiteSpace = "pre-wrap";
      contentDiv.style.wordWrap = "break-word";
      contentDiv.style.fontFamily = '"SF Mono", "Consolas", "Liberation Mono", monospace';
      contentDiv.style.fontSize = "0.85rem";
      contentDiv.style.lineHeight = "1.5";

      var pre = document.createElement("pre");
      pre.style.margin = "0";
      var code = document.createElement("code");
      code.id = "file-content-code";
      pre.appendChild(code);
      contentDiv.appendChild(pre);

      article.appendChild(contentDiv);

      // Footer with close button
      var footer = document.createElement("footer");
      footer.style.flexShrink = "0";
      footer.style.display = "flex";
      footer.style.flexDirection = "row";
      footer.style.justifyContent = "flex-end";
      footer.style.paddingTop = "var(--pico-spacing, 1rem)";
      footer.style.borderTop = "1px solid var(--muted-border-color, #e0e0e0)";

      var closeBtn = document.createElement("button");
      closeBtn.id = "file-content-close";
      closeBtn.textContent = "Close";
      closeBtn.className = "outline";
      closeBtn.addEventListener("click", function () {
        if (typeof dialog.close === "function") {
          dialog.close();
        }
      });
      footer.appendChild(closeBtn);

      article.appendChild(footer);
      dialog.appendChild(article);
      document.body.appendChild(dialog);

      // Close on backdrop click
      dialog.addEventListener("click", function (e) {
        if (e.target === dialog) {
          dialog.close();
        }
      });
    }

    // Update title
    var titleEl = document.getElementById("file-content-title");
    if (titleEl) {
      titleEl.textContent = filePath;
    }

    // Show loading state
    var codeEl = document.getElementById("file-content-code");
    if (codeEl) {
      codeEl.textContent = "Loading\u2026";
    }

    // Open the dialog
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }

    // Fetch file content
    api.workspaceFile(pid, filePath, root).then(function (data) {
      var content = (data && data.content) || "";
      if (codeEl) {
        // Limit to 50000 chars
        if (content.length > 50000) {
          content = content.slice(0, 50000) + "\n\n... [truncated at 50000 chars]";
        }
        codeEl.textContent = content;
      }
    }).catch(function (/* err */) {
      if (codeEl) {
        codeEl.textContent = "Failed to load file content";
      }
    });
  }


  // ── Reconnect overlay ────────────────────────────────────────────

  /**
   * Show or hide the reconnection overlay based on App.state.connectionOk.
   */
  function _updateReconnectOverlay() {
    var overlay = document.getElementById("project-reconnect-overlay");
    if (!overlay) {
      return;
    }

    var connected = _isConnectionOk();
    overlay.style.display = connected ? "none" : "block";
  }


  // ── Refresh ──────────────────────────────────────────────────────

  /**
   * Fetch project + tasks via API and re-render the view.
   * Uses _isRefreshing flag to prevent stacked requests.
   *
   * @param {boolean} [dynamic] — when true, update only the dynamic content
   *   in place (poll path) instead of rebuilding the whole view.
   */
  function _refresh(dynamic) {
    if (_isRefreshing) {
      return;
    }

    if (!_projectId) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getProject !== "function" || typeof api.listTasks !== "function") {
      return;
    }

    _isRefreshing = true;

    // Fetch project, tasks, and run detail in parallel. The run fetch is
    // best-effort (a project may not have a run yet) — a failure just hides the
    // pipeline overview rather than failing the whole refresh.
    var runP = (typeof api.getRun === "function")
      ? api.getRun(_projectId).catch(function () { return null; })
      : Promise.resolve(null);
    Promise.all([
      api.getProject(_projectId),
      api.listTasks(_projectId),
      runP,
    ]).then(function (results) {
      _isRefreshing = false;
      var project = results[0];
      var tasks = results[1] || [];
      var run = results[2] || null;

      if (project) {
        if (dynamic) {
          _updateDynamic(project, tasks, run);
        } else {
          _render(project, tasks, run);
        }
      }
    }).catch(function (err) {
      _isRefreshing = false;

      // 404 → project doesn't exist anymore → navigate back to dashboard
      if (err && (err.status === 404 || (err.message && err.message.indexOf("404") !== -1))) {
        try {
          var router = window.AItelier && window.AItelier.Router;
          if (router && typeof router.navigate === "function") {
            router.navigate("#/");
          }
        } catch (_e) {
          window.location.hash = "#/";
        }
        return;
      }

      // Network error → update overlay
      _updateReconnectOverlay();
    });
  }


  // ── Public API ────────────────────────────────────────────────────

  var ProjectDetail = {

    /**
     * Show the project detail view for a given project ID.
     * Fetches project data + tasks, renders the view, and starts polling.
     *
     * @param {object} params — route parameters, expected {id: "project-id"}
     */
    show: function (params) {
      var pid = params && params.id;
      if (!pid) {
        return;
      }

      _projectId = pid;

      // Make #view-project visible
      var container = document.getElementById("view-project");
      if (container) {
        container.classList.add("active");
      }

      // Fetch data and render
      _refresh();

      // Start polling — dynamic (in-place) refresh so the workspace trees and
      // any in-progress interaction (scroll, expanded dirs) are preserved.
      if (_pollTimer === null) {
        _pollTimer = setInterval(function () {
          _refresh(true);
        }, _POLL_INTERVAL);
      }
    },

    /**
     * Hide the project detail view.
     * Stops the polling interval and cleans up state.
     */
    hide: function () {
      // Stop polling
      if (_pollTimer !== null) {
        clearInterval(_pollTimer);
        _pollTimer = null;
      }

      // Hide the section
      var container = document.getElementById("view-project");
      if (container) {
        container.classList.remove("active");
      }

      // Reset state
      _projectId = null;
      _cachedProject = null;
      _cachedTasks = [];
      _cachedRun = null;
      _expandedDirs = {};
      _expandedTaskRows = {};
      _expandedOverviewSteps = {};
    },

    /**
     * Immediately refresh the project data and re-render.
     * Can be called externally (e.g. after a checkpoint resolution).
     */
    refresh: function () {
      _refresh();
    },
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.ProjectDetail = ProjectDetail;
})();
