"use strict";

(function () {
  /**
   * AItelier.Trace — execution trace viewer for a project's pipeline run.
   *
   * Renders the durable trace records returned by GET /api/runs/{id}/trace
   * (prompt / response / tool_call / error / step events) as a chronological,
   * expandable list with a category filter.
   *
   * DOM target: #view-trace
   * Route:      #/projects/{id}/trace
   *
   * A view object with show(params)/hide() as required by AItelier.Router.
   */

  // ── Private state ──────────────────────────────────────────────────

  /** @type {string|null} project id currently shown. */
  var _projectId = null;

  /** @type {string} active category filter ("" = all). */
  var _category = "";

  /** @type {number} max entries to fetch. */
  var _LIMIT = 500;

  /** Built-in DPE step labels; manifest labels take precedence when loaded. */
  var _STEP_LABELS = {
    "1": "Researcher", "1_review": "Research Review",
    "2": "Architect", "2_review": "Architecture Review",
    "3": "PM", "3_review": "PM Review",
    "5": "Final Verifier", "5_review": "Final Review", "5_test": "Unit Tests",
    "t_plan": "Task Planner", "t_plan_review": "Plan Review",
    "t_impl": "Implementer", "t_impl_review": "Impl Review",
    "t_verify": "Task Verifier", "t_verify_review": "Verify Review",
    "task_loop": "Task Loop", "git_sync_pre": "Git Sync",
  };

  var _CATEGORIES = ["", "prompt", "response", "tool_call", "tool_result", "error", "step"];


  // ── Helpers ────────────────────────────────────────────────────────

  function _stepLabel(stepId) {
    if (!stepId) { return ""; }
    try {
      var cache = window.AItelier && window.AItelier.configManifests;
      if (cache) {
        var keys = Object.keys(cache);
        for (var i = 0; i < keys.length; i++) {
          var m = cache[keys[i]];
          if (m && m.labels && m.labels[stepId]) { return m.labels[stepId]; }
        }
      }
    } catch (_e) { /* fall through */ }
    return _STEP_LABELS[stepId] || stepId;
  }

  /** Format an SQLite "YYYY-MM-DD HH:MM:SS" timestamp to a short HH:MM:SS. */
  function _shortTime(ts) {
    if (!ts) { return ""; }
    var s = String(ts);
    var m = s.match(/(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : s;
  }

  /** Extract the most human-relevant text from a trace payload. */
  function _payloadText(payload) {
    if (payload == null) { return ""; }
    if (typeof payload === "string") { return payload; }
    if (typeof payload === "object") {
      // Common content-bearing fields for prompt/response/error traces.
      var direct = payload.content || payload.text || payload.message ||
                   payload.response || payload.prompt || payload.error;
      if (typeof direct === "string" && direct) {
        return direct;
      }
      try {
        return JSON.stringify(payload, null, 2);
      } catch (_e) {
        return String(payload);
      }
    }
    return String(payload);
  }


  // ── Rendering ──────────────────────────────────────────────────────

  function _renderToolbar(container) {
    var bar = document.createElement("div");
    bar.className = "trace-toolbar";

    var back = document.createElement("a");
    back.href = "#/projects/" + encodeURIComponent(_projectId);
    back.textContent = "← Back to project";
    bar.appendChild(back);

    var spacer = document.createElement("span");
    spacer.style.flex = "1";
    bar.appendChild(spacer);

    var catLabel = document.createElement("label");
    catLabel.textContent = "Category:";
    catLabel.style.margin = "0";
    catLabel.style.fontSize = "0.85rem";
    bar.appendChild(catLabel);

    var select = document.createElement("select");
    _CATEGORIES.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c === "" ? "All" : c;
      if (c === _category) { opt.selected = true; }
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      _category = select.value;
      _load();
    });
    bar.appendChild(select);

    var refresh = document.createElement("button");
    refresh.className = "outline";
    refresh.textContent = "Refresh";
    refresh.addEventListener("click", function () { _load(); });
    bar.appendChild(refresh);

    container.appendChild(bar);
  }

  function _renderEntry(t) {
    var entry = document.createElement("div");
    var cat = t.category || "step";
    entry.className = "trace-entry cat-" + cat;

    var head = document.createElement("div");
    head.className = "trace-head";

    var catSpan = document.createElement("span");
    catSpan.className = "trace-cat";
    catSpan.textContent = cat;
    head.appendChild(catSpan);

    var stepSpan = document.createElement("span");
    stepSpan.className = "trace-step";
    stepSpan.textContent = _stepLabel(t.step_id);
    head.appendChild(stepSpan);

    var eventSpan = document.createElement("span");
    eventSpan.className = "trace-event";
    eventSpan.textContent = t.event || "";
    head.appendChild(eventSpan);

    var timeSpan = document.createElement("span");
    timeSpan.className = "trace-time";
    timeSpan.textContent = _shortTime(t.created_at);
    head.appendChild(timeSpan);

    entry.appendChild(head);

    // Collapsible body with the payload text.
    var body = document.createElement("pre");
    body.className = "trace-body";
    body.style.display = "none";
    body.textContent = _payloadText(t.payload);
    entry.appendChild(body);

    head.addEventListener("click", function () {
      body.style.display = body.style.display === "none" ? "block" : "none";
    });

    return entry;
  }

  function _render(traces) {
    var container = document.getElementById("view-trace");
    if (!container) { return; }
    container.innerHTML = "";

    var title = document.createElement("h3");
    title.textContent = "Execution Trace — " + _projectId;
    container.appendChild(title);

    _renderToolbar(container);

    if (!traces || traces.length === 0) {
      var empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No trace records" +
        (_category ? " for category “" + _category + "”." : " yet.");
      container.appendChild(empty);
      return;
    }

    var count = document.createElement("p");
    count.className = "empty-state";
    count.style.margin = "0 0 0.5rem 0";
    count.textContent = traces.length + " record(s), oldest first · click a row to expand";
    container.appendChild(count);

    var list = document.createElement("div");
    list.id = "trace-list";
    traces.forEach(function (t) { list.appendChild(_renderEntry(t)); });
    container.appendChild(list);
  }

  function _renderLoading() {
    var container = document.getElementById("view-trace");
    if (!container) { return; }
    container.innerHTML = "";
    var title = document.createElement("h3");
    title.textContent = "Execution Trace — " + _projectId;
    container.appendChild(title);
    _renderToolbar(container);
    var loading = document.createElement("p");
    loading.className = "empty-state";
    loading.textContent = "Loading trace…";
    container.appendChild(loading);
  }

  function _renderError(msg) {
    var container = document.getElementById("view-trace");
    if (!container) { return; }
    container.innerHTML = "";
    var title = document.createElement("h3");
    title.textContent = "Execution Trace — " + _projectId;
    container.appendChild(title);
    _renderToolbar(container);
    var err = document.createElement("p");
    err.className = "empty-state";
    err.textContent = msg || "Failed to load trace.";
    container.appendChild(err);
  }


  // ── Data ───────────────────────────────────────────────────────────

  function _load() {
    if (!_projectId) { return; }
    var api = window.AItelier && window.AItelier.API;
    if (!api || typeof api.getTrace !== "function") {
      _renderError("Trace API not available.");
      return;
    }
    _renderLoading();
    var opts = { limit: _LIMIT };
    if (_category) { opts.category = _category; }
    // run_id accepts a project_id (resolved to its most recent run server-side).
    api.getTrace(_projectId, opts).then(function (data) {
      _render((data && data.traces) || []);
    }).catch(function (err) {
      _renderError("Failed to load trace: " + ((err && err.message) || "unknown error"));
    });
  }


  // ── Public view API ────────────────────────────────────────────────

  var Trace = {
    show: function (params) {
      var pid = params && params.id;
      if (!pid) { return; }
      _projectId = pid;

      var container = document.getElementById("view-trace");
      if (container) { container.classList.add("active"); }

      _load();
    },

    hide: function () {
      var container = document.getElementById("view-trace");
      if (container) { container.classList.remove("active"); }
      _projectId = null;
      _category = "";
    },
  };

  window.AItelier = window.AItelier || {};
  window.AItelier.Trace = Trace;
})();
