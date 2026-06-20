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


  // ── Render entry point ────────────────────────────────────────────

  /**
   * Fetch project data + tasks, then re-render the full view.
   * Called by _refresh() after fetching data.
   *
   * @param {object} project — project object from API.getProject()
   * @param {Array} tasks — task array from API.listTasks()
   */
  function _render(project, tasks) {
    _cachedProject = project;
    _cachedTasks = tasks || [];

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

    // ── 1. Info Card ──
    container.appendChild(_renderInfoCard(project));

    // ── 2. Task List Table ──
    container.appendChild(_renderTaskTable(tasks));

    // ── 3. Workspace File Tree (fetch on render) ──
    var wsSection = document.createElement("div");
    wsSection.id = "workspace-section";
    wsSection.style.marginTop = "var(--pico-spacing, 1rem)";
    var wsTitle = document.createElement("h4");
    wsTitle.textContent = "Workspace Files";
    wsSection.appendChild(wsTitle);
    var wsStatus = document.createElement("p");
    wsStatus.id = "workspace-loading";
    wsStatus.textContent = "Loading workspace tree\u2026";
    wsStatus.className = "empty-state";
    wsSection.appendChild(wsStatus);
    container.appendChild(wsSection);

    // Fetch workspace tree asynchronously
    _fetchWorkspaceTree(wsSection);

    // ── Update reconnect overlay visibility ──
    _updateReconnectOverlay();
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

    // Retry — only shown when status contains "failed"
    var isFailed = status.indexOf("failed") !== -1;

    if (isFailed) {
      var retryBtn = document.createElement("button");
      retryBtn.id = "btn-project-retry";
      retryBtn.textContent = "Retry";
      retryBtn.className = "outline";
      retryBtn.addEventListener("click", function () {
        _handleActionRetry(this);
      });
      btnRow.appendChild(retryBtn);
    }

    // Refresh Planning — always shown
    var refreshBtn = document.createElement("button");
    refreshBtn.id = "btn-project-refresh";
    refreshBtn.textContent = "Refresh Planning";
    refreshBtn.className = "outline";
    refreshBtn.addEventListener("click", function () {
      _handleActionRefresh(this);
    });
    btnRow.appendChild(refreshBtn);

    // Pause / Resume — toggle based on status
    var isPaused = status.indexOf("paused") !== -1;
    var isRunning = status.indexOf("running") !== -1 ||
                    status.indexOf("advancing") !== -1 ||
                    status.indexOf("planning") !== -1 ||
                    status.indexOf("executing") !== -1;

    if (isPaused) {
      var resumeBtn = document.createElement("button");
      resumeBtn.id = "btn-project-resume";
      resumeBtn.textContent = "Resume";
      resumeBtn.className = "outline";
      resumeBtn.addEventListener("click", function () {
        _handleActionPauseResume("executing", this);
      });
      btnRow.appendChild(resumeBtn);
    } else if (isRunning) {
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
   * Fetch the workspace tree from API and render as expandable tree.
   *
   * @param {HTMLElement} section — the workspace section element
   */
  function _fetchWorkspaceTree(section) {
    if (!_projectId) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.workspaceTree !== "function") {
      var errEl = document.getElementById("workspace-loading");
      if (errEl) {
        errEl.textContent = "Workspace browsing not available";
      }
      return;
    }

    api.workspaceTree(_projectId).then(function (data) {
      var loadingEl = document.getElementById("workspace-loading");
      if (loadingEl) {
        loadingEl.parentElement.removeChild(loadingEl);
      }

      var tree = (data && data.tree) || [];
      if (tree.length === 0) {
        var empty = document.createElement("p");
        empty.className = "empty-state";
        empty.textContent = "Workspace is empty";
        section.appendChild(empty);
        return;
      }

      var treeContainer = _renderFileTree(tree);
      section.appendChild(treeContainer);
    }).catch(function (/* err */) {
      var loadingEl = document.getElementById("workspace-loading");
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
  function _renderFileTree(treeArray) {
    var nested = _buildTreeIndices(treeArray);

    var treeEl = document.createElement("ul");
    treeEl.id = "workspace-tree";
    treeEl.style.listStyle = "none";
    treeEl.style.paddingLeft = "0";
    treeEl.style.margin = "0";
    treeEl.style.fontSize = "0.85rem";

    _renderTreeLevel(nested, treeEl, "");

    return treeEl;
  }

  /**
   * Recursively render one level of the tree.
   *
   * @param {object} node — tree node with _files and child dirs
   * @param {HTMLElement} parentEl — parent <ul> element
   * @param {string} parentPath — accumulated path prefix for this level
   */
  function _renderTreeLevel(node, parentEl, parentPath) {
    if (!node || typeof node !== "object") {
      return;
    }

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
          _showFileContent(_projectId, path);
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

      // Click handler: toggle directory expansion
      (function (dp, chUl) {
        dirHeader.addEventListener("click", function (e) {
          e.stopPropagation();
          _toggleDir(dp, chUl, childNode);
        });
      })(dirPath, childrenUl);

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
   */
  function _toggleDir(dirPath, childrenEl, childNode) {
    var isExpanded = childrenEl.dataset.expanded === "true";

    if (isExpanded) {
      // Collapse: hide children
      childrenEl.style.display = "none";
      childrenEl.dataset.expanded = "false";
      delete _expandedDirs[dirPath];
      return;
    }

    // Expand: check if children are already rendered
    if (childrenEl.children.length > 0) {
      // Already rendered — just show
      childrenEl.style.display = "block";
      childrenEl.dataset.expanded = "true";
      _expandedDirs[dirPath] = true;
      return;
    }

    // Not yet rendered — fetch subdirectory contents via API
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.workspaceTree !== "function") {
      return;
    }

    // Show a loading indicator
    var loadingItem = document.createElement("li");
    loadingItem.textContent = "Loading\u2026";
    loadingItem.style.fontStyle = "italic";
    loadingItem.style.color = "var(--muted-color, #888)";
    loadingItem.style.fontSize = "0.8rem";
    childrenEl.appendChild(loadingItem);

    api.workspaceTree(_projectId, dirPath).then(function (data) {
      // Remove loading indicator
      while (childrenEl.firstChild) {
        childrenEl.removeChild(childrenEl.firstChild);
      }

      var subTree = (data && data.tree) || [];
      // Build a nested structure for the subdirectory contents
      var nested = _buildTreeIndices(subTree);
      // Only render children of the directory itself
      // Recurse with the child node data we already have
      _renderTreeLevel(childNode, childrenEl, dirPath);

      childrenEl.style.display = "block";
      childrenEl.dataset.expanded = "true";
      _expandedDirs[dirPath] = true;
    }).catch(function () {
      // On error, remove loading and show error
      while (childrenEl.firstChild) {
        childrenEl.removeChild(childrenEl.firstChild);
      }
      var errItem = document.createElement("li");
      errItem.textContent = "Failed to load";
      errItem.style.fontStyle = "italic";
      errItem.style.color = "var(--del-color, #d04040)";
      errItem.style.fontSize = "0.8rem";
      childrenEl.appendChild(errItem);
    });
  }

  /**
   * Show file content in a modal/dialog.
   * Creates a <dialog> element dynamically if one doesn't exist.
   *
   * @param {string} pid — project ID
   * @param {string} filePath — relative file path within workspace
   */
  function _showFileContent(pid, filePath) {
    if (!pid || !filePath) {
      return;
    }

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
    api.workspaceFile(pid, filePath).then(function (data) {
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
   */
  function _refresh() {
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

    // Fetch project and tasks in parallel
    Promise.all([
      api.getProject(_projectId),
      api.listTasks(_projectId),
    ]).then(function (results) {
      _isRefreshing = false;
      var project = results[0];
      var tasks = results[1] || [];

      if (project) {
        _render(project, tasks);
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

      // Start polling
      if (_pollTimer === null) {
        _pollTimer = setInterval(function () {
          _refresh();
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
      _expandedDirs = {};
      _expandedTaskRows = {};
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
