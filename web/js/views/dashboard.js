"use strict";

(function () {
  /**
   * AItelier.Dashboard — Project list dashboard view.
   *
   * Renders a sortable project table with status icons, task progress,
   * inline create form, and delete confirmation.  Polls every 3 seconds.
   *
   * DOM target: #view-dashboard
   * Dependencies: AItelier.API, AItelier.Router, AItelier.Utils, AItelier.App (optional)
   *
   * Usage:
   *   AItelier.Dashboard.show();
   *   AItelier.Dashboard.hide();
   *   AItelier.Dashboard.refresh();
   */

  // ── Constants ──────────────────────────────────────────────────────

  /** Polling interval in milliseconds. */
  var _POLL_INTERVAL = 10000;  // 10 s (was 3 s — too aggressive, reset form inputs)

  /** Step ID → human-readable label map. Seeded with DPE labels and extended
   * at runtime from every config's manifest (/api/configs) so runs of ANY
   * config render proper step names. Unknown steps fall back to the raw id. */
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

  /** @type {boolean} true if any inline form (new-project / start-run) is open.
   * Polling is paused while true so the form's inputs aren't reset. */
  var _formOpen = false;

  /** @type {Object<string,boolean>} config_name → collapsed?, preserved across
   * re-renders so polling doesn't reopen sections the user collapsed. */
  var _collapsed = {};

  /** @type {string|null} config_name whose inline start-run form is open. */
  var _startFormConfig = null;

  /** @type {string|null} project_id to delete when confirmation dialog opens. */
  var _pendingDeleteId = null;


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


  // ── Task progress formatting ──────────────────────────────────────

  /**
   * Format the task progress string for a project row.
   * Example: "3/5 ▶1" for 3 done, 5 total, 1 running.
   *
   * @param {object} project — project object from API
   * @returns {string} formatted progress string
   */
  function _formatTaskProgress(project) {
    var total = project.task_count || 0;
    var completed = project.completed_count || 0;
    var running = project.running_count || 0;
    var failed = project.failed_count || 0;

    if (total === 0) {
      return "-";
    }

    var parts = [];
    parts.push(completed + "/" + total);

    if (running > 0) {
      parts.push("\u25B6" + running);
    }
    if (failed > 0) {
      parts.push("\u2717" + failed);
    }

    return parts.join(" ");
  }


  // ── Token count formatting ────────────────────────────────────────

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


  // ── Row creation ──────────────────────────────────────────────────

  /**
   * Create a table row for a single project by cloning the
   * #tpl-project-row template and populating its cells.
   *
   * @param {object} project — project object from API.listProjects()
   * @param {number} index — 1-based row index for display
   * @returns {HTMLTableRowElement|null}
   */
  function _createRow(project, index) {
    var template = document.getElementById("tpl-project-row");
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

    var pid = project.project_id || "";
    var name = project.name || pid;

    // # column
    cells[0].textContent = String(index);

    // Project Name (clickable link)
    var link = cells[1].querySelector("a");
    if (link) {
      link.textContent = name;
      link.href = "#/projects/" + encodeURIComponent(pid);
    } else {
      cells[1].textContent = name;
    }

    // Status badge
    var status = project.status || "planning";
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

    // Cache hit ratio inline badge
    var cs = project.cache_stats;
    if (cs && cs.hit_ratio != null && cs.hit_ratio !== undefined) {
      var pct = (cs.hit_ratio * 100).toFixed(1) + "%";
      var cacheSpan = document.createElement("span");
      cacheSpan.className = "cache-inline-badge";
      if (cs.hit_ratio >= 0.7) cacheSpan.classList.add("cache-badge-high");
      else if (cs.hit_ratio >= 0.3) cacheSpan.classList.add("cache-badge-mid");
      else cacheSpan.classList.add("cache-badge-low");
      var tokensText = "";
      if (cs.total_tokens != null && cs.total_tokens !== undefined) {
        tokensText = " \u00B7 " + _fmtTokens(cs.total_tokens);
      }
      cacheSpan.textContent = " \u00B7 Cache " + pct + tokensText;
      cells[2].appendChild(cacheSpan);
    }

    // Task progress
    cells[3].textContent = _formatTaskProgress(project);

    // Last Update (relative time)
    var updatedAt = project.last_update || project.updated_at || project.created_at || "";
    cells[4].textContent = (function () {
      try {
        var utils = window.AItelier && window.AItelier.Utils;
        if (utils && typeof utils.formatTime === "function") {
          return utils.formatTime(updatedAt);
        }
      } catch (_e) {
        // fallthrough
      }
      return updatedAt ? String(updatedAt).slice(0, 16) : "";
    })();

    // Attach project_id to the row as a data attribute for click handling
    row.dataset.projectId = pid;

    return row;
  }


  // ── Table rendering ───────────────────────────────────────────────

  // ── Grouped rendering (pipelines → runs) ──────────────────────────

  /** Build a single run row (reuses _createRow) with click-to-open + delete. */
  function _runRow(run, index) {
    var row = _createRow(run, index);
    if (!row) { return null; }
    row.addEventListener("click", function (e) {
      if (e.target && e.target.classList.contains("btn-delete-project")) { return; }
      var pid = this.dataset.projectId;
      if (!pid) { return; }
      try {
        var rtr = window.AItelier && window.AItelier.Router;
        if (rtr && typeof rtr.navigate === "function") {
          rtr.navigate("#/projects/" + encodeURIComponent(pid));
          return;
        }
      } catch (_e) { /* fall through */ }
      window.location.hash = "#/projects/" + encodeURIComponent(pid);
    });
    var deleteTd = document.createElement("td");
    deleteTd.style.textAlign = "right";
    deleteTd.style.width = "3rem";
    var delBtn = document.createElement("button");
    delBtn.className = "btn-delete-project";
    delBtn.textContent = "✗";
    delBtn.style.background = "none";
    delBtn.style.border = "none";
    delBtn.style.color = "var(--muted-color, #888)";
    delBtn.style.cursor = "pointer";
    delBtn.style.fontSize = "0.85rem";
    delBtn.style.padding = "0.25rem 0.5rem";
    delBtn.title = "Delete run";
    delBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var pid = this.parentElement.parentElement.dataset.projectId;
      if (pid) { _confirmDelete(pid); }
    });
    deleteTd.appendChild(delBtn);
    row.appendChild(deleteTd);
    return row;
  }

  /** Build the runs table for one pipeline section. */
  function _buildRunsTable(runs) {
    var table = document.createElement("table");
    table.style.width = "100%";
    table.style.margin = "0";
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    var heads = ["#", "Run", "Status", "Tasks", "Last Update", ""];
    for (var h = 0; h < heads.length; h++) {
      var th = document.createElement("th");
      th.textContent = heads[h];
      if (h === 0) { th.className = "col-idx"; }
      htr.appendChild(th);
    }
    thead.appendChild(htr);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    for (var i = 0; i < runs.length; i++) {
      var row = _runRow(runs[i], i + 1);
      if (row) { tbody.appendChild(row); }
    }
    table.appendChild(tbody);
    return table;
  }

  /** Inline start-run form for a non-DPE pipeline (name + seed text → POST /api/runs). */
  function _buildStartRunForm(cfg) {
    var wrap = document.createElement("div");
    wrap.className = "start-run-form";
    wrap.style.padding = "0.75rem 0.9rem";
    wrap.style.borderBottom = "1px solid var(--muted-border-color, #eee)";

    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "Run name (optional)";
    nameInput.style.marginBottom = "0.4rem";

    var seed = document.createElement("textarea");
    seed.placeholder = "Seed input for this run (optional)";
    seed.rows = 3;
    seed.style.marginBottom = "0.4rem";

    var errEl = document.createElement("small");
    errEl.className = "srf-error";
    errEl.style.color = "var(--del-color, #b00)";
    errEl.style.display = "none";

    var btnRow = document.createElement("div");
    btnRow.style.display = "flex";
    btnRow.style.gap = "0.5rem";
    var go = document.createElement("button");
    go.textContent = "Start run";
    go.style.fontSize = "0.85rem";
    go.style.padding = "0.25rem 0.75rem";
    go.addEventListener("click", function () {
      _submitStartRun(cfg, nameInput.value.trim(), seed.value, errEl);
    });
    var cancel = document.createElement("button");
    cancel.className = "secondary outline";
    cancel.textContent = "Cancel";
    cancel.style.fontSize = "0.85rem";
    cancel.style.padding = "0.25rem 0.75rem";
    cancel.addEventListener("click", function () {
      wrap.style.display = "none";
      _startFormConfig = null;
      _formOpen = false;
    });
    btnRow.appendChild(go);
    btnRow.appendChild(cancel);

    wrap.appendChild(nameInput);
    wrap.appendChild(seed);
    wrap.appendChild(errEl);
    wrap.appendChild(btnRow);
    return wrap;
  }

  function _submitStartRun(cfg, name, seedText, errEl) {
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.startRun !== "function") { return; }
    var body = { config_name: cfg.config_name };
    if (name) { body.name = name; }
    if (seedText) { body.seed_text = seedText; }
    api.startRun(body).then(function () {
      _startFormConfig = null;
      _formOpen = false;
      _refresh();
    }).catch(function (err) {
      if (errEl) {
        errEl.textContent = "Failed to start run: " + ((err && err.message) || "error");
        errEl.style.display = "block";
      }
    });
  }

  /** Handle a pipeline's "Start run" button. DPE reuses the proven New Project
   * form; other configs toggle an inline seed-input form. */
  function _onStartRun(cfg) {
    if (cfg.config_name === "dpe_default_v2") {
      _startFormConfig = "dpe_default_v2";
      _toggleForm();
      return;
    }
    var section = document.querySelector(
      '.pipeline-section[data-config="' + cfg.config_name + '"]');
    var srf = section ? section.querySelector(".start-run-form") : null;
    if (!srf) { return; }
    var opening = srf.style.display === "none";
    // Close any other open forms first
    _closeAllStartForms();
    if (opening) {
      srf.style.display = "block";
      _startFormConfig = cfg.config_name;
      _formOpen = true;
    }
  }

  /** Closes all inline start-run forms across pipeline sections. */
  function _closeAllStartForms() {
    var forms = document.querySelectorAll(".start-run-form");
    for (var i = 0; i < forms.length; i++) {
      forms[i].style.display = "none";
    }
    _startFormConfig = null;
    _formOpen = false;
  }


  // ── Pipeline grouping ─────────────────────────────────────────────

  /**
   * Group runs by config_name, preserving the catalog order from /api/configs.
   * Also returns the set of configs that have no runs (hidden by default).
   */
  function _groupRunsByConfig(configs, runs) {
    var groups = [];
    var seen = {};

    // Configs in catalog order (only those with runs)
    for (var i = 0; i < configs.length; i++) {
      var name = configs[i].config_name || configs[i].name;
      if (!name) { continue; }
      var cfgLabel = configs[i].label || name;
      var match = [];
      for (var j = 0; j < runs.length; j++) {
        if ((runs[j].config_name || "dpe_default_v2") === name) {
          match.push(runs[j]);
        }
      }
      if (match.length > 0) {
        groups.push({
          config_name: name,
          label: cfgLabel,
          manifest: configs[i],
          runs: match,
        });
        seen[name] = true;
      }
    }

    // Runs whose config is no longer installed — orphan bucket
    var orphanRuns = [];
    for (var k = 0; k < runs.length; k++) {
      var cfgName = runs[k].config_name || "dpe_default_v2";
      if (!seen[cfgName]) {
        orphanRuns.push(runs[k]);
      }
    }
    if (orphanRuns.length > 0) {
      groups.push({
        config_name: "_orphan_",
        label: "Other Runs",
        manifest: null,
        runs: orphanRuns,
      });
    }

    return groups;
  }


  // ── Main render ───────────────────────────────────────────────────

  /**
   * Render the dashboard: cycle through each pipeline (config category)
   * and render a section with its runs table.
   *
   * @param {Array} configs — list of config manifests from /api/configs
   * @param {Array} runs — list of run objects from /api/runs
   */
  function _renderTable(configs, runs) {
    var container = document.getElementById("view-dashboard");
    if (!container) { return; }

    // Store all runs (with config labels) for the table views
    var allConfigLabels = {};
    for (var c = 0; c < configs.length; c++) {
      allConfigLabels[configs[c].config_name || configs[c].name] = configs[c].label || configs[c].name;
    }
    // Attach label to each run
    for (var r = 0; r < runs.length; r++) {
      var cfgName = runs[r].config_name || "dpe_default_v2";
      runs[r].config_label = allConfigLabels[cfgName] || cfgName;
    }

    var groups = _groupRunsByConfig(configs, runs);

    // Build inner content
    var html = "";

    if (groups.length === 0) {
      html += '<div style="padding: 2em; text-align: center; color: var(--muted-color, #888);">';
      html += "No runs yet. Click &ldquo;New Run&rdquo; on any pipeline below to start one.";
      html += "</div>";
    } else {
      for (var g = 0; g < groups.length; g++) {
        var grp = groups[g];
        // Skip orphan runs if there are named groups (orphans are appended at the end)
        if (grp.config_name === "_orphan_" && groups.length > 1) {
          continue;
        }

        var collapsibleId = "psec-" + (grp.config_name || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
        var isCollapsed = _collapsed[grp.config_name] === true;

        html += '<div class="pipeline-section" data-config="' + escapeAttr(grp.config_name) + '">';

        // Section header (pipeline name + controls)
        html += '<div class="pipeline-header" style="display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem 0.9rem; border-bottom: 1px solid var(--muted-border-color, #eee); cursor: pointer;" onclick="var e=document.getElementById(\'' + collapsibleId + '\');if(e){var c=e.style.display===\'none\';e.style.display=c?\'\':\'none\';var ic=this.querySelector(\'.collapse-icon\');if(ic)ic.textContent=c?\'\u25BC\':\'\u25B6\';}">';
        html += '<span class="collapse-icon" style="font-size:0.7rem;color:var(--muted-color,#888)">' + (isCollapsed ? "\u25B6" : "\u25BC") + "</span>";

        // Pipeline label & badge of count
        html += '<strong style="flex:1">' + escapeHtml(grp.label) + "</strong>";
        html += '<span style="font-size:0.8rem;color:var(--muted-color,#888)">' + grp.runs.length + " run" + (grp.runs.length !== 1 ? "s" : "") + "</span>";

        var isDpe = grp.config_name === "dpe_default_v2";
        var startBtnLabel = isDpe ? "New Project" : "Start run";
        html += '<button class="start-run-btn" style="font-size:0.8rem;padding:0.2rem 0.6rem;cursor:pointer" onclick="var D=window.AItelier&&window.AItelier.Dashboard;if(D)D._onStartRun&&D._onStartRun(' + JSON.stringify(grp.manifest || grp) + ');event.stopPropagation();">' + startBtnLabel + "</button>";

        html += "</div>";

        // Collapsible body
        html += '<div id="' + collapsibleId + '" class="pipeline-body" style="' + (isCollapsed ? "display:none" : "") + '">';
        html += "</div>"; // placeholder — replaced below
        html += "</div>";
      }
    }

    // If there are orphan runs and more than one group, show them at the bottom
    var orphanGroup = null;
    for (var og = 0; og < groups.length; og++) {
      if (groups[og].config_name === "_orphan_") {
        orphanGroup = groups[og];
        break;
      }
    }
    if (orphanGroup && groups.length > 1) {
      html += '<div class="pipeline-section" data-config="_orphan_">';
      html += '<div class="pipeline-header" style="display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem 0.9rem; border-bottom: 1px solid var(--muted-border-color, #eee);">';
      html += '<strong style="flex:1; color: var(--muted-color, #888);">Other Runs</strong>';
      html += '<span style="font-size:0.8rem;color:var(--muted-color,#888)">' + orphanGroup.runs.length + " run" + (orphanGroup.runs.length !== 1 ? "s" : "") + "</span>";
      html += "</div>";
      html += '<div class="pipeline-body"></div>';
      html += "</div>";
    }

    container.innerHTML = html;

    // Mount the DPE new-project form + reconnect overlay at the top. Rebuilt
    // every render so they survive polling; the form's open state is restored
    // from _formOpen/_startFormConfig (polling is paused while _formOpen).
    var reconnectOverlay = document.createElement("div");
    reconnectOverlay.id = "dashboard-reconnect-overlay";
    reconnectOverlay.style.display = "none";
    reconnectOverlay.style.textAlign = "center";
    reconnectOverlay.style.padding = "1rem";
    reconnectOverlay.textContent = "Reconnecting…";
    container.insertBefore(reconnectOverlay, container.firstChild);

    var formContainer = document.createElement("div");
    formContainer.id = "new-project-form";
    formContainer.style.display =
      (_formOpen && _startFormConfig === "dpe_default_v2") ? "block" : "none";
    formContainer.style.marginBottom = "var(--pico-spacing, 1rem)";
    formContainer.style.padding = "var(--pico-spacing, 1rem)";
    formContainer.style.border = "1px solid var(--muted-border-color, #e0e0e0)";
    formContainer.style.borderRadius = "0.5rem";
    formContainer.appendChild(_buildNewProjectForm());
    container.insertBefore(formContainer, container.firstChild);

    // Now attach the actual table DOM elements (so each row can have event listeners)
    var bodyDivs = container.querySelectorAll(".pipeline-body");
    var bodyIdx = 0;
    for (var gi = 0; gi < groups.length; gi++) {
      if (groups[gi].config_name === "_orphan_" && groups.length > 1) {
        continue; // handled separately below
      }
      var bd = bodyDivs[bodyIdx];
      if (bd) {
        var tbl = _buildRunsTable(groups[gi].runs);
        bd.appendChild(tbl);
      }
      bodyIdx++;
    }
    // Orphan runs group
    if (orphanGroup && groups.length > 1) {
      var orphanBody = container.querySelector('.pipeline-section[data-config="_orphan_"] .pipeline-body');
      if (orphanBody) {
        var otbl = _buildRunsTable(orphanGroup.runs);
        orphanBody.appendChild(otbl);
      }
    }

    // Show reconnect overlay if needed
    _updateReconnectOverlay();
  }


  // ── HTML helpers ──────────────────────────────────────────────────

  function escapeHtml(str) {
    if (typeof str !== "string") return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function escapeAttr(str) {
    if (typeof str !== "string") return "";
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }


  // ── Config manifest loading ───────────────────────────────────────

  var _configsLoaded = false;
  function _loadConfigs() {
    if (_configsLoaded) { return; }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getConfigs !== "function") { return; }

    api.getConfigs().then(function (resp) {
      if (!resp || !resp.configs) { return; }
      // Keep a cache of manifests for other views
      var cache = {};
      resp.configs.forEach(function (m) {
        cache[m.config_name] = m;
        var labels = m.labels || {};
        Object.keys(labels).forEach(function (k) { _STEP_LABELS[k] = labels[k]; });
      });
      window.AItelier = window.AItelier || {};
      window.AItelier.configManifests = cache;
    }).catch(function () { _configsLoaded = false; });
  }


  // ── New Project / DPE form ────────────────────────────────────────

  // Recovered from git (pre-92078f8) and grafted onto the runs model. The form
  // is rebuilt with its own wiring by _renderTable on every render, so it
  // survives polling. The only adaptation from the original: submit goes to the
  // generic run-launch path (startRun \u2192 POST /api/runs) instead of createProject,
  // and that path now honors repo_type/repo_url/repo_path (see run_launcher.py),
  // so new / existing / clone repos can all be launched from the dashboard.

  /** Build the inline "new project" form (DOM, self-wired). */
  function _buildNewProjectForm() {
    var form = document.createElement("form");
    form.id = "form-new-project";
    form.style.display = "flex";
    form.style.flexDirection = "column";
    form.style.gap = "0.75rem";

    // Row 1: project_id (optional slug) + name (optional)
    var row1 = document.createElement("div");
    row1.style.display = "flex";
    row1.style.flexDirection = "row";
    row1.style.gap = "0.75rem";
    row1.style.flexWrap = "wrap";

    var idGroup = document.createElement("div");
    idGroup.style.flex = "1";
    idGroup.style.minWidth = "200px";
    var idLabel = document.createElement("label");
    idLabel.htmlFor = "f-project-id";
    idLabel.textContent = "Project ID (slug, optional)";
    idGroup.appendChild(idLabel);
    var idInput = document.createElement("input");
    idInput.type = "text";
    idInput.id = "f-project-id";
    idInput.name = "project_id";
    idInput.placeholder = "auto-generated if blank";
    idInput.autocomplete = "off";
    idGroup.appendChild(idInput);
    var idError = document.createElement("small");
    idError.id = "f-project-id-error";
    idError.style.color = "var(--del-color, #d04040)";
    idError.style.display = "none";
    idGroup.appendChild(idError);
    row1.appendChild(idGroup);

    var nameGroup = document.createElement("div");
    nameGroup.style.flex = "1";
    nameGroup.style.minWidth = "200px";
    var nameLabel = document.createElement("label");
    nameLabel.htmlFor = "f-name";
    nameLabel.textContent = "Display Name (optional)";
    nameGroup.appendChild(nameLabel);
    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.id = "f-name";
    nameInput.name = "name";
    nameInput.placeholder = "e.g. My Todo App";
    nameInput.autocomplete = "off";
    nameGroup.appendChild(nameInput);
    row1.appendChild(nameGroup);
    form.appendChild(row1);

    // Build request (seed text) \u2014 what DPE works from.
    var seedGroup = document.createElement("div");
    var seedLabel = document.createElement("label");
    seedLabel.htmlFor = "f-seed-text";
    seedLabel.textContent = "Build request";
    seedGroup.appendChild(seedLabel);
    var seedInput = document.createElement("textarea");
    seedInput.id = "f-seed-text";
    seedInput.name = "seed_text";
    seedInput.rows = 3;
    seedInput.placeholder = "Describe what to build, or the change to make on the repo";
    seedGroup.appendChild(seedInput);
    form.appendChild(seedGroup);

    // Row 2: repo_type select
    var row2 = document.createElement("div");
    var repoLabel = document.createElement("label");
    repoLabel.htmlFor = "f-repo-type";
    repoLabel.textContent = "Repository Type";
    row2.appendChild(repoLabel);
    var repoSelect = document.createElement("select");
    repoSelect.id = "f-repo-type";
    repoSelect.name = "repo_type";
    var optNew = document.createElement("option");
    optNew.value = "new";
    optNew.textContent = "New (create fresh repo)";
    repoSelect.appendChild(optNew);
    var optExisting = document.createElement("option");
    optExisting.value = "existing";
    optExisting.textContent = "Existing (use local repo)";
    repoSelect.appendChild(optExisting);
    var optClone = document.createElement("option");
    optClone.value = "clone";
    optClone.textContent = "Clone (from URL)";
    repoSelect.appendChild(optClone);
    row2.appendChild(repoSelect);
    form.appendChild(row2);

    // Row 3: repo_path (shown when "existing")
    var row3 = document.createElement("div");
    row3.id = "f-repo-path-group";
    row3.style.display = "none";
    var pathLabel = document.createElement("label");
    pathLabel.htmlFor = "f-repo-path";
    pathLabel.textContent = "Local Repo Path";
    row3.appendChild(pathLabel);
    var pathInput = document.createElement("input");
    pathInput.type = "text";
    pathInput.id = "f-repo-path";
    pathInput.name = "repo_path";
    pathInput.placeholder = "/home/user/projects/my-app";
    pathInput.autocomplete = "off";
    row3.appendChild(pathInput);
    var pathError = document.createElement("small");
    pathError.id = "f-repo-path-error";
    pathError.style.color = "var(--del-color, #d04040)";
    pathError.style.display = "none";
    row3.appendChild(pathError);
    form.appendChild(row3);

    // Row 4: repo_url (shown when "clone")
    var row4 = document.createElement("div");
    row4.id = "f-repo-url-group";
    row4.style.display = "none";
    var urlLabel = document.createElement("label");
    urlLabel.htmlFor = "f-repo-url";
    urlLabel.textContent = "Git URL";
    row4.appendChild(urlLabel);
    var urlInput = document.createElement("input");
    urlInput.type = "text";
    urlInput.id = "f-repo-url";
    urlInput.name = "repo_url";
    urlInput.placeholder = "https://github.com/user/repo.git";
    urlInput.autocomplete = "off";
    row4.appendChild(urlInput);
    var urlError = document.createElement("small");
    urlError.id = "f-repo-url-error";
    urlError.style.color = "var(--del-color, #d04040)";
    urlError.style.display = "none";
    row4.appendChild(urlError);
    form.appendChild(row4);

    // Form-level error
    var errorArea = document.createElement("div");
    errorArea.id = "f-form-error";
    errorArea.style.color = "var(--del-color, #d04040)";
    errorArea.style.display = "none";
    form.appendChild(errorArea);

    // Action buttons
    var buttonRow = document.createElement("div");
    buttonRow.style.display = "flex";
    buttonRow.style.flexDirection = "row";
    buttonRow.style.gap = "0.5rem";
    var submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.textContent = "Create Project";
    buttonRow.appendChild(submitBtn);
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "outline";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", function () { _toggleForm(); });
    buttonRow.appendChild(cancelBtn);
    form.appendChild(buttonRow);

    // Conditional repo fields on select change
    repoSelect.addEventListener("change", function () {
      var val = this.value;
      var pathGroup = document.getElementById("f-repo-path-group");
      var urlGroup = document.getElementById("f-repo-url-group");
      if (pathGroup) { pathGroup.style.display = val === "existing" ? "block" : "none"; }
      if (urlGroup) { urlGroup.style.display = val === "clone" ? "block" : "none"; }
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      _handleFormSubmit();
    });

    return form;
  }

  /** Show or hide the inline new-project form; resets fields when opening. */
  function _toggleForm() {
    _formOpen = !_formOpen;
    var formContainer = document.getElementById("new-project-form");
    if (!formContainer) { return; }

    if (_formOpen) {
      _closeAllStartForms();
      _formOpen = true;  // _closeAllStartForms clears it
      _startFormConfig = "dpe_default_v2";
      formContainer.style.display = "block";
      var form = document.getElementById("form-new-project");
      if (form) { form.reset(); }
      var pathGroup = document.getElementById("f-repo-path-group");
      var urlGroup = document.getElementById("f-repo-url-group");
      if (pathGroup) { pathGroup.style.display = "none"; }
      if (urlGroup) { urlGroup.style.display = "none"; }
      _hideFormError();
      _hideFieldError("f-project-id-error");
      var idInput = document.getElementById("f-project-id");
      if (idInput) { setTimeout(function () { idInput.focus(); }, 100); }
    } else {
      formContainer.style.display = "none";
      _startFormConfig = null;
    }
  }

  /** Collect + validate form fields. @returns {{valid, errors, data}} */
  function _validateForm() {
    var idInput = document.getElementById("f-project-id");
    var nameInput = document.getElementById("f-name");
    var seedInput = document.getElementById("f-seed-text");
    var repoSelect = document.getElementById("f-repo-type");
    var pathInput = document.getElementById("f-repo-path");
    var urlInput = document.getElementById("f-repo-url");

    var errors = {};
    var data = { config_name: "dpe_default_v2" };

    // project_id: optional, but if given must be a valid slug
    var projectId = idInput ? idInput.value.trim() : "";
    if (projectId) {
      if (!/^[a-z0-9][a-z0-9_-]*$/.test(projectId)) {
        errors.project_id = "Project ID must start with a lowercase letter or digit, and contain only lowercase letters, digits, hyphens, or underscores";
      } else {
        data.project_id = projectId;
      }
    }

    var name = nameInput ? nameInput.value.trim() : "";
    if (name) { data.name = name; }

    var seed = seedInput ? seedInput.value.trim() : "";
    if (seed) { data.seed_text = seed; }

    var repoType = repoSelect ? repoSelect.value : "new";
    data.repo_type = repoType;

    if (repoType === "existing") {
      var repoPath = pathInput ? pathInput.value.trim() : "";
      if (!repoPath) {
        errors.repo_path = "Local repo path is required when using an existing repository";
      } else {
        data.repo_path = repoPath;
      }
    }

    if (repoType === "clone") {
      var repoUrl = urlInput ? urlInput.value.trim() : "";
      if (!repoUrl) {
        errors.repo_url = "Git URL is required when cloning a repository";
      } else {
        data.repo_url = repoUrl;
      }
    }

    return {
      valid: Object.keys(errors).length === 0,
      errors: errors,
      data: data,
    };
  }

  function _showFieldError(errorId, message) {
    var el = document.getElementById(errorId);
    if (el) { el.textContent = message; el.style.display = "block"; }
  }
  function _hideFieldError(errorId) {
    var el = document.getElementById(errorId);
    if (el) { el.textContent = ""; el.style.display = "none"; }
  }
  function _showFormError(message) {
    var el = document.getElementById("f-form-error");
    if (el) { el.textContent = message; el.style.display = "block"; }
  }
  function _hideFormError() {
    var el = document.getElementById("f-form-error");
    if (el) { el.textContent = ""; el.style.display = "none"; }
  }

  /** Validate -> POST /api/runs (honors repo_type/url/path) -> close + refresh. */
  function _handleFormSubmit() {
    var result = _validateForm();
    if (!result.valid) {
      _hideFormError();
      _hideFieldError("f-project-id-error");
      _hideFieldError("f-repo-path-error");
      _hideFieldError("f-repo-url-error");
      for (var field in result.errors) {
        if (!result.errors.hasOwnProperty(field)) { continue; }
        if (field === "project_id") {
          _showFieldError("f-project-id-error", result.errors[field]);
        } else if (field === "repo_path") {
          _showFieldError("f-repo-path-error", result.errors[field]);
        } else if (field === "repo_url") {
          _showFieldError("f-repo-url-error", result.errors[field]);
        } else {
          _showFormError(result.errors[field]);
        }
      }
      return;
    }

    _hideFieldError("f-project-id-error");
    _hideFieldError("f-repo-path-error");
    _hideFieldError("f-repo-url-error");
    _hideFormError();

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.startRun !== "function") {
      _showFormError("API client not available");
      return;
    }

    var submitBtn = document.querySelector("#form-new-project button[type='submit']");
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Creating\u2026"; }

    api.startRun(result.data).then(function () {
      // Success \u2014 _toggleForm flips _formOpen (true here) so it closes.
      _toggleForm();
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Create Project"; }
      _refresh();
    }).catch(function (err) {
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Create Project"; }
      if (err && err.status === 409) {
        _showFormError("Project already exists");
      } else if (err && err.status === 400) {
        _showFormError((err && err.message) || "Invalid input");
      } else {
        _showFormError((err && err.message) || "Failed to create project");
      }
    });
  }


  // ── Confirmation dialog ───────────────────────────────────────────

  // Recovered from git: the styled #confirm-dialog modal (in index.html), with a
  // per-project message and proper listener cleanup. Falls back to nothing if the
  // dialog markup is absent.
  function _confirmDelete(projectId) {
    if (!projectId) { return; }

    _pendingDeleteId = projectId;

    var dialog = document.getElementById("confirm-dialog");
    if (!dialog) {
      // No modal markup — fall back to a native confirm so delete still works.
      if (window.confirm('Delete "' + projectId + '" permanently?')) {
        _doDelete(projectId);
        _pendingDeleteId = null;
      }
      return;
    }

    var titleEl = document.getElementById("confirm-title");
    if (titleEl) { titleEl.textContent = "Delete Project"; }
    var msgEl = document.getElementById("confirm-message");
    if (msgEl) {
      msgEl.textContent = 'Are you sure you want to delete "' + projectId +
        '"? This will permanently remove all tasks, files, and workspace data.';
    }

    var yesBtn = document.getElementById("confirm-yes");
    var noBtn = document.getElementById("confirm-no");

    function cleanUp() {
      if (yesBtn) { yesBtn.removeEventListener("click", onConfirm); }
      if (noBtn) { noBtn.removeEventListener("click", onCancel); }
      dialog.removeEventListener("close", onCancel);
    }
    function onConfirm() {
      cleanUp();
      _doDelete(_pendingDeleteId);
      _pendingDeleteId = null;
      if (typeof dialog.close === "function") { dialog.close(); }
    }
    function onCancel() {
      cleanUp();
      _pendingDeleteId = null;
    }

    if (yesBtn) { yesBtn.addEventListener("click", onConfirm); }
    if (noBtn) { noBtn.addEventListener("click", onCancel); }
    dialog.addEventListener("close", onCancel);

    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  }

  function _doDelete(projectId) {
    if (!projectId) { return; }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.deleteProject !== "function") {
      return;
    }

    api.deleteProject(projectId).then(function () {
      _refresh();
    }).catch(function (err) {
      var msg = (err && err.message) || "Failed to delete project";
      // Show error in a flash/toast-like manner
      try {
        var app = window.AItelier && window.AItelier.App;
        if (app && typeof app.showError === "function") {
          app.showError(msg);
        }
      } catch (_e) {
        // fallback: alert
        window.alert(msg);
      }
    });
  }


  // ── Reconnect overlay ────────────────────────────────────────────

  /**
   * Show or hide the reconnection overlay based on App.state.connectionOk.
   */
  function _updateReconnectOverlay() {
    var overlay = document.getElementById("dashboard-reconnect-overlay");
    if (!overlay) {
      return;
    }

    var connected = _isConnectionOk();
    overlay.style.display = connected ? "none" : "block";
  }


  // ── Refresh ──────────────────────────────────────────────────────

  /**
   * Fetch projects via API and re-render the table.
   * Uses _isRefreshing flag to prevent stacked requests.
   */
  function _refresh() {
    // Never auto-refresh while the user is filling out the create form
    if (_formOpen) {
      return;
    }

    if (_isRefreshing) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.listAllRuns !== "function") {
      return;
    }

    _isRefreshing = true;

    // Fetch the installed pipelines (catalog) + all runs, then render grouped.
    var configsP = (typeof api.getConfigs === "function")
      ? api.getConfigs().then(function (r) { return (r && r.configs) || []; })
                        .catch(function () { return []; })
      : Promise.resolve([]);
    var runsP = api.listAllRuns()
      .then(function (r) { return (r && r.runs) || []; })
      .catch(function () { return []; });

    Promise.all([configsP, runsP]).then(function (res) {
      _isRefreshing = false;
      _renderTable(res[0] || [], res[1] || []);
    }).catch(function (/* err */) {
      _isRefreshing = false;
      // Keep existing data on error; update reconnect overlay
      _updateReconnectOverlay();
    });
  }


  // ── Public API ────────────────────────────────────────────────────

  var Dashboard = {

    /**
     * Show the dashboard view.
     * Renders the project table into #view-dashboard and starts
     * polling every 3 seconds.
     */
    show: function () {
      // Show the container
      var container = document.getElementById("view-dashboard");
      if (container) container.classList.add("active");

      // Load config manifests once (data-driven step labels for any config).
      _loadConfigs();

      // Render the table (fetch project data)
      _refresh();

      // Start polling
      if (_pollTimer === null) {
        _pollTimer = setInterval(function () {
          _refresh();
        }, _POLL_INTERVAL);
      }
    },

    /**
     * Hide the dashboard view.
     * Stops the polling interval.  Does NOT clear the DOM — the
     * section remains in its hidden state with existing data.
     */
    hide: function () {
      // Hide the container
      var container = document.getElementById("view-dashboard");
      if (container) container.classList.remove("active");

      // Stop polling
      if (_pollTimer !== null) {
        clearInterval(_pollTimer);
        _pollTimer = null;
      }
    },

    /**
     * Immediately refresh the project list.
     * Can be called externally (e.g. after a checkpoint resolution
     * or project creation from another view).
     */
    refresh: function () {
      _refresh();
    },
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.Dashboard = Dashboard;

  // Expose internal helpers for inline onclick handlers in rendered HTML
  Dashboard._onStartRun = _onStartRun;
  Dashboard._closeAllStartForms = _closeAllStartForms;

  // The new-project form is built (and self-wired) by _renderTable on every
  // render via _buildNewProjectForm(), so no one-time DOM-ready wiring is needed.

})();
