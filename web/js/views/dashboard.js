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

  /** @type {boolean} true if the inline "new project" form is currently open. */
  var _formOpen = false;

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

  /**
   * Render the full project table into #view-dashboard.
   * Clears the container, builds table HTML, and appends.
   *
   * @param {Array} projects — array of project objects from API
   */
  function _renderTable(projects) {
    var container = document.getElementById("view-dashboard");
    if (!container) {
      return;
    }

    // Clear existing content (including form, table, etc.)
    container.innerHTML = "";

    // ── New Project button ──
    var headerRow = document.createElement("div");
    headerRow.style.display = "flex";
    headerRow.style.flexDirection = "row";
    headerRow.style.justifyContent = "space-between";
    headerRow.style.alignItems = "center";
    headerRow.style.marginBottom = "var(--pico-spacing, 1rem)";

    var title = document.createElement("h3");
    title.textContent = "Projects";
    title.style.margin = "0";
    headerRow.appendChild(title);

    var newBtn = document.createElement("button");
    newBtn.id = "btn-new-project";
    newBtn.textContent = "+ New Project";
    newBtn.className = "outline";
    newBtn.style.flexShrink = "0";
    newBtn.addEventListener("click", function () {
      _toggleForm();
    });
    headerRow.appendChild(newBtn);

    container.appendChild(headerRow);

    // ── Inline new project form (hidden by default) ──
    var formContainer = document.createElement("div");
    formContainer.id = "new-project-form";
    formContainer.style.display = "none";
    formContainer.style.marginBottom = "var(--pico-spacing, 1rem)";
    formContainer.style.padding = "var(--pico-spacing, 1rem)";
    formContainer.style.border = "1px solid var(--muted-border-color, #e0e0e0)";
    formContainer.style.borderRadius = "0.5rem";
    formContainer.appendChild(_buildNewProjectForm());
    container.appendChild(formContainer);

    // ── Reconnect overlay (hidden by default) ──
    var reconnectOverlay = document.createElement("div");
    reconnectOverlay.id = "dashboard-reconnect-overlay";
    reconnectOverlay.style.display = "none";
    reconnectOverlay.style.position = "relative";
    reconnectOverlay.style.textAlign = "center";
    reconnectOverlay.style.padding = "2rem 1rem";
    reconnectOverlay.style.backgroundColor = "rgba(255, 255, 255, 0.85)";
    reconnectOverlay.style.borderRadius = "0.5rem";
    reconnectOverlay.style.marginTop = "1rem";
    reconnectOverlay.textContent = "Reconnecting\u2026";
    container.appendChild(reconnectOverlay);

    // ── Empty state ──
    if (!projects || projects.length === 0) {
      var emptyMsg = document.createElement("p");
      emptyMsg.className = "empty-state";
      emptyMsg.textContent = "No projects yet \u2014 create your first project";
      container.appendChild(emptyMsg);
      return;
    }

    // ── Table ──
    var table = document.createElement("table");
    table.style.width = "100%";

    // thead
    var thead = document.createElement("thead");
    var headerTr = document.createElement("tr");
    var headers = ["#", "Project Name", "Status", "Tasks", "Last Update"];
    for (var h = 0; h < headers.length; h++) {
      var th = document.createElement("th");
      th.textContent = headers[h];
      // Add appropriate class to columns
      if (h === 0) { th.className = "col-idx"; }
      if (h === 4) { th.className = "col-updated"; }
      headerTr.appendChild(th);
    }
    thead.appendChild(headerTr);
    table.appendChild(thead);

    // tbody
    var tbody = document.createElement("tbody");

    for (var i = 0; i < projects.length; i++) {
      var row = _createRow(projects[i], i + 1);
      if (row) {
        // Row click → navigate to project (except when clicking the delete button)
        row.addEventListener("click", function (e) {
          // Ignore clicks on the delete button or its container
          if (e.target && e.target.classList.contains("btn-delete-project")) {
            return;
          }
          var pid = this.dataset.projectId;
          if (pid) {
            try {
              var router = window.AItelier && window.AItelier.Router;
              if (router && typeof router.navigate === "function") {
                router.navigate("#/projects/" + encodeURIComponent(pid));
              }
            } catch (_err) {
              // fallback: set location.hash directly
              window.location.hash = "#/projects/" + encodeURIComponent(pid);
            }
          }
        });

        // Delete button (small, dim) at the end of the row
        var deleteTd = document.createElement("td");
        deleteTd.style.textAlign = "right";
        deleteTd.style.width = "3rem";

        var delBtn = document.createElement("button");
        delBtn.className = "btn-delete-project";
        delBtn.textContent = "\u2717"; // ✗
        delBtn.style.background = "none";
        delBtn.style.border = "none";
        delBtn.style.color = "var(--muted-color, #888)";
        delBtn.style.cursor = "pointer";
        delBtn.style.fontSize = "0.85rem";
        delBtn.style.padding = "0.25rem 0.5rem";
        delBtn.title = "Delete project";

        delBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          var pid = this.parentElement.parentElement.dataset.projectId;
          if (pid) {
            _confirmDelete(pid);
          }
        });

        deleteTd.appendChild(delBtn);
        row.appendChild(deleteTd);

        tbody.appendChild(row);
      }
    }

    table.appendChild(tbody);
    container.appendChild(table);

    // ── Update reconnect overlay visibility after everything is rendered ──
    _updateReconnectOverlay();
  }


  // ── New Project Form ──────────────────────────────────────────────

  /**
   * Build the inline "new project" form HTML elements.
   * Returns a DocumentFragment or HTMLElement containing the form.
   *
   * @returns {HTMLElement} the form element
   */
  function _buildNewProjectForm() {
    var form = document.createElement("form");
    form.id = "form-new-project";
    form.style.display = "flex";
    form.style.flexDirection = "column";
    form.style.gap = "0.75rem";

    // Row 1: project_id (required) + name (optional)
    var row1 = document.createElement("div");
    row1.style.display = "flex";
    row1.style.flexDirection = "row";
    row1.style.gap = "0.75rem";
    row1.style.flexWrap = "wrap";

    // project_id field
    var idGroup = document.createElement("div");
    idGroup.style.flex = "1";
    idGroup.style.minWidth = "200px";

    var idLabel = document.createElement("label");
    idLabel.htmlFor = "f-project-id";
    idLabel.textContent = "Project ID (slug) *";
    idGroup.appendChild(idLabel);

    var idInput = document.createElement("input");
    idInput.type = "text";
    idInput.id = "f-project-id";
    idInput.name = "project_id";
    idInput.required = true;
    idInput.placeholder = "e.g. my-todo-app";
    idInput.autocomplete = "off";
    idGroup.appendChild(idInput);

    // Validation error for project_id
    var idError = document.createElement("small");
    idError.id = "f-project-id-error";
    idError.style.color = "var(--del-color, #d04040)";
    idError.style.display = "none";
    idGroup.appendChild(idError);

    row1.appendChild(idGroup);

    // name field
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

    // Row 3: repo_path (shown when "existing" is selected)
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

    form.appendChild(row3);

    // Row 4: repo_url (shown when "clone" is selected)
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

    form.appendChild(row4);

    // Row 5: Error message area
    var errorArea = document.createElement("div");
    errorArea.id = "f-form-error";
    errorArea.style.color = "var(--del-color, #d04040)";
    errorArea.style.display = "none";
    form.appendChild(errorArea);

    // Row 6: Action buttons
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
    cancelBtn.addEventListener("click", function () {
      _toggleForm();
    });
    buttonRow.appendChild(cancelBtn);

    form.appendChild(buttonRow);

    // ── Conditional repo fields on select change ──
    repoSelect.addEventListener("change", function () {
      var val = this.value;
      var pathGroup = document.getElementById("f-repo-path-group");
      var urlGroup = document.getElementById("f-repo-url-group");
      if (pathGroup) { pathGroup.style.display = val === "existing" ? "block" : "none"; }
      if (urlGroup) { urlGroup.style.display = val === "clone" ? "block" : "none"; }
    });

    // ── Form submit handler ──
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      _handleFormSubmit();
    });

    return form;
  }


  // ── Form toggle ───────────────────────────────────────────────────

  /**
   * Show or hide the inline "new project" form.
   * Resets form fields when opening.
   */
  function _toggleForm() {
    _formOpen = !_formOpen;
    var formContainer = document.getElementById("new-project-form");
    if (!formContainer) {
      return;
    }

    if (_formOpen) {
      formContainer.style.display = "block";
      // Reset form fields
      var form = document.getElementById("form-new-project");
      if (form) {
        form.reset();
      }
      // Hide conditional fields
      var pathGroup = document.getElementById("f-repo-path-group");
      var urlGroup = document.getElementById("f-repo-url-group");
      if (pathGroup) { pathGroup.style.display = "none"; }
      if (urlGroup) { urlGroup.style.display = "none"; }
      // Hide errors
      _hideFormError();
      _hideFieldError("f-project-id-error");
      // Focus the project_id field
      var idInput = document.getElementById("f-project-id");
      if (idInput) {
        setTimeout(function () { idInput.focus(); }, 100);
      }
    } else {
      formContainer.style.display = "none";
    }
  }


  // ── Form validation ──────────────────────────────────────────────

  /**
   * Collect and validate form data.
   *
   * @returns {{valid: boolean, errors: object, data: object}}
   *   errors is a field-name → error-message mapping.
   *   data contains trimmed field values (only populated when valid).
   */
  function _validateForm() {
    var idInput = document.getElementById("f-project-id");
    var nameInput = document.getElementById("f-name");
    var repoSelect = document.getElementById("f-repo-type");
    var pathInput = document.getElementById("f-repo-path");
    var urlInput = document.getElementById("f-repo-url");

    var errors = {};
    var data = {};

    // project_id: required, non-empty after trim
    var projectId = idInput ? idInput.value.trim() : "";
    if (!projectId) {
      errors.project_id = "Project ID is required";
    } else if (!/^[a-z0-9][a-z0-9_-]*$/.test(projectId)) {
      // Allow alphanumeric, hyphens, underscores — must start with alnum
      errors.project_id = "Project ID must start with a letter or digit, and contain only letters, digits, hyphens, or underscores";
    } else {
      data.project_id = projectId;
    }

    // name (optional)
    var name = nameInput ? nameInput.value.trim() : "";
    if (name) {
      data.name = name;
    }

    // repo_type (default "new")
    var repoType = repoSelect ? repoSelect.value : "new";
    data.repo_type = repoType;

    // repo_path (required for "existing")
    if (repoType === "existing") {
      var repoPath = pathInput ? pathInput.value.trim() : "";
      if (!repoPath) {
        errors.repo_path = "Local repo path is required when using existing repository";
      } else {
        data.repo_path = repoPath;
      }
    }

    // repo_url (required for "clone")
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


  /**
   * Show a field-level validation error message.
   *
   * @param {string} errorId — element ID of the error <small>
   * @param {string} message — error text to show
   */
  function _showFieldError(errorId, message) {
    var el = document.getElementById(errorId);
    if (el) {
      el.textContent = message;
      el.style.display = "block";
    }
  }

  /**
   * Hide a field-level validation error message.
   *
   * @param {string} errorId — element ID of the error <small>
   */
  function _hideFieldError(errorId) {
    var el = document.getElementById(errorId);
    if (el) {
      el.textContent = "";
      el.style.display = "none";
    }
  }

  /**
   * Show a form-level error message.
   *
   * @param {string} message — error text
   */
  function _showFormError(message) {
    var el = document.getElementById("f-form-error");
    if (el) {
      el.textContent = message;
      el.style.display = "block";
    }
  }

  /**
   * Hide the form-level error message.
   */
  function _hideFormError() {
    var el = document.getElementById("f-form-error");
    if (el) {
      el.textContent = "";
      el.style.display = "none";
    }
  }


  // ── Form submit handling ─────────────────────────────────────────

  /**
   * Handle the new-project form submission.
   * Validates → calls API.createProject() → handles 409/success/error.
   */
  function _handleFormSubmit() {
    var result = _validateForm();
    if (!result.valid) {
      // Show inline validation errors
      _hideFormError();
      for (var field in result.errors) {
        if (result.errors.hasOwnProperty(field)) {
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
      }
      return;
    }

    // Clear errors
    _hideFieldError("f-project-id-error");
    _hideFormError();

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.createProject !== "function") {
      _showFormError("API client not available");
      return;
    }

    // Disable submit button to prevent double-submit
    var submitBtn = document.querySelector("#form-new-project button[type='submit']");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Creating\u2026";
    }

    api.createProject(result.data).then(function () {
      // Success — close form and refresh
      _toggleForm();
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = "Create Project";
      }
      _refresh();
    }).catch(function (err) {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = "Create Project";
      }

      if (err && err.status === 409) {
        _showFormError("Project already exists");
      } else if (err && err.status === 400) {
        _showFormError(err.message || "Invalid input");
      } else {
        var msg = (err && err.message) || "Failed to create project";
        _showFormError(msg);
      }
    });
  }


  // ── Delete confirmation ──────────────────────────────────────────

  /**
   * Show the confirm dialog for deleting a project.
   *
   * @param {string} projectId — the project to delete
   */
  function _confirmDelete(projectId) {
    if (!projectId) {
      return;
    }

    _pendingDeleteId = projectId;

    var dialog = document.getElementById("confirm-dialog");
    if (!dialog) {
      return;
    }

    // Set dialog title and message
    var titleEl = document.getElementById("confirm-title");
    if (titleEl) {
      titleEl.textContent = "Delete Project";
    }

    var msgEl = document.getElementById("confirm-message");
    if (msgEl) {
      msgEl.textContent = 'Are you sure you want to delete "' + projectId + '"? This will permanently remove all tasks, files, and workspace data.';
    }

    // Bind confirm button event
    var yesBtn = document.getElementById("confirm-yes");
    var noBtn = document.getElementById("confirm-no");

    function cleanUp() {
      if (yesBtn) { yesBtn.removeEventListener("click", _onConfirm); }
      if (noBtn) { noBtn.removeEventListener("click", _onCancel); }
      dialog.removeEventListener("close", _onCancel);
    }

    function _onConfirm() {
      cleanUp();
      _executeDelete(_pendingDeleteId);
      _pendingDeleteId = null;
      if (typeof dialog.close === "function") {
        dialog.close();
      }
    }

    function _onCancel() {
      cleanUp();
      _pendingDeleteId = null;
    }

    yesBtn.addEventListener("click", _onConfirm);
    noBtn.addEventListener("click", _onCancel);
    dialog.addEventListener("close", _onCancel);

    // Show the dialog
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  }

  /**
   * Execute the delete API call and refresh on success.
   *
   * @param {string} projectId
   */
  function _executeDelete(projectId) {
    if (!projectId) {
      return;
    }

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
    if (_isRefreshing) {
      return;
    }

    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.listProjects !== "function") {
      return;
    }

    _isRefreshing = true;

    api.listProjects().then(function (projects) {
      _isRefreshing = false;
      _renderTable(projects || []);
    }).catch(function (/* err */) {
      _isRefreshing = false;
      // Keep existing table data on error; update reconnect overlay
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
})();
