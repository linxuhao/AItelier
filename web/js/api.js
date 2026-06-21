"use strict";

(function () {
  /**
   * AItelier.API — Fetch API wrapper for all AItelier backend REST endpoints.
   *
   * Zero framework dependencies.  All methods return Promises resolving to
   * parsed JSON.  HTTP errors throw ApiError {status, message}.  Network
   * errors set `AItelier.App.state.connectionOk = false` and trigger the
   * reconnect banner.
   *
   * Internal helpers:
   *   _request(method, path, body?, timeout?) — core fetch wrapper
   *   _get(path, timeout?) / _post(path, body?, timeout?)
   *   _patch(path, body) / _delete(path)
   *
   * Usage:
   *   AItelier.API.listProjects().then(function(projects) { ... });
   *   AItelier.API.createProject({project_id: "my-proj"}).then(...);
   */

  // ── Constants ──────────────────────────────────────────────────────

  /** Default request timeout (10 seconds). */
  var _DEFAULT_TIMEOUT = 10000;

  /** Read-only mode: when false, all mutating requests are short-circuited.
   *  Set from GET /api/me on app init. Defaults to true (local/dev, no gate). */
  var _canWrite = true;
  var _SAFE_METHODS = { GET: 1, HEAD: 1, OPTIONS: 1 };


  // ── ApiError constructor ──────────────────────────────────────────

  /**
   * Custom error for API-level failures.
   *
   * @param {number} status — HTTP status code, or 0 for network/timeout errors
   * @param {string} message — human-readable error description
   */
  function ApiError(status, message) {
    this.status = status;
    this.message = message;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;
  ApiError.prototype.name = "ApiError";


  // ── Network-error callbacks (guarded against missing App) ─────────

  /** Notify the App layer that the backend connection is lost. */
  function _reportNetworkError() {
    try {
      var app = window.AItelier && window.AItelier.App;
      if (app) {
        app.state.connectionOk = false;
        if (typeof app.showReconnectBanner === "function") {
          app.showReconnectBanner();
        }
      }
    } catch (_e) {
      // Silently guard against App not being initialized yet.
    }
  }


  // ── Core request helper ───────────────────────────────────────────

  /**
   * Execute an HTTP request with timeout and error handling.
   *
   * @param {string} method — HTTP method ("GET", "POST", "PATCH", "DELETE")
   * @param {string} path — URL path relative to origin (e.g. "/api/projects")
   * @param {object|null|undefined} body — JSON-serializable request body
   * @param {number|undefined} timeout — timeout in ms (default 10000)
   * @returns {Promise<object|array|null>} parsed JSON response
   * @throws {ApiError} on HTTP errors or network failures
   */
  function _request(method, path, body, timeout) {
    // Read-only guard: block every mutating request up front (covers chat,
    // delete, retry, and any button) with a consistent message. The backend
    // also enforces this (403); this is the UX layer.
    if (!_canWrite && !_SAFE_METHODS[method]) {
      return Promise.reject(new ApiError(
        403,
        "Read-only access — sign in as an authorized user to make changes."
      ));
    }

    var effectiveTimeout = (timeout !== undefined && timeout !== null)
      ? timeout : _DEFAULT_TIMEOUT;

    var url = window.location.origin + path;

    // Build fetch options
    var fetchOptions = {
      method: method,
      headers: {
        "Accept": "application/json",
      },
    };

    // Attach JSON body for POST/PATCH (only when a body is provided)
    if (body !== undefined && body !== null && (method === "POST" || method === "PATCH")) {
      fetchOptions.headers["Content-Type"] = "application/json";
      fetchOptions.body = JSON.stringify(body);
    }

    // ── Single attempt: fetch → handle status → parse ──
    function _attempt() {
      var controller = new AbortController();
      fetchOptions.signal = controller.signal;
      var timeoutId = setTimeout(function () {
        controller.abort();
      }, effectiveTimeout);

      return fetch(url, fetchOptions)
        .then(function (response) {
          clearTimeout(timeoutId);
          return _handleResponse(response);
        })
        .catch(function (err) {
          clearTimeout(timeoutId);
          return _handleFetchError(err);
        });
    }

    // ── Response status handler ──
    function _handleResponse(response) {
      // 204 No Content (e.g. DELETE) → return null
      if (response.status === 204) {
        return null;
      }

      // Successful response → parse JSON
      if (response.ok) {
        return response.json();
      }

      // Non-OK status → parse error detail from body
      return response.json().then(function (errData) {
        var message =
          (errData && (errData.detail || errData.message)) ||
          response.statusText ||
          "Request failed";
        throw new ApiError(response.status, String(message));
      }).catch(function (parseErr) {
        // If parsing the error JSON itself failed, rethrow the parse error
        // only if it's NOT already an ApiError (we already constructed one above)
        if (parseErr instanceof ApiError) {
          throw parseErr;
        }
        throw new ApiError(
          response.status,
          response.statusText || "Request failed"
        );
      });
    }

    // ── Fetch-level error handler (network / abort) ──
    function _handleFetchError(err) {
      // AbortController timeout
      if (err.name === "AbortError") {
        throw new ApiError(0, "Request timed out");
      }

      // fetch() rejects with TypeError on network failure (DNS / connection refused)
      if (err instanceof TypeError && (
          err.message === "Failed to fetch" ||
          err.message.indexOf("fetch") !== -1 ||
          err.message.indexOf("NetworkError") !== -1
      )) {
        _reportNetworkError();
        throw new ApiError(0, "Network error: " + (err.message || "failed to fetch"));
      }

      // Any other unexpected rejection
      throw new ApiError(0, "Request failed: " + (err.message || String(err)));
    }

    // ── Idempotent GET retry ──
    // On network error, retry once after 1s delay.
    if (method === "GET") {
      return _attempt().catch(function (err) {
        if (err instanceof ApiError && err.status === 0) {
          // Network error — retry once after 1 second
          return new Promise(function (resolve) {
            setTimeout(resolve, 1000);
          }).then(function () {
            return _attempt();
          }).catch(function (retryErr) {
            // If retry also fails with network error, report it
            if (retryErr instanceof ApiError && retryErr.status === 0) {
              _reportNetworkError();
            }
            throw retryErr;
          });
        }
        // Non-network error or non-0 status — rethrow immediately
        throw err;
      });
    }

    // Non-GET: single attempt, no retry
    return _attempt();
  }


  // ── Convenience wrappers ──────────────────────────────────────────

  /**
   * Perform a GET request.
   * @param {string} path — URL path
   * @param {number} [timeout] — optional timeout override
   * @returns {Promise}
   */
  function _get(path, timeout) {
    return _request("GET", path, undefined, timeout);
  }

  /**
   * Perform a POST request with an optional JSON body.
   * @param {string} path — URL path
   * @param {object} [body] — request body (will be JSON-serialized)
   * @param {number} [timeout] — optional timeout override
   * @returns {Promise}
   */
  function _post(path, body, timeout) {
    return _request("POST", path, body, timeout);
  }

  /**
   * Perform a PATCH request with an optional JSON body.
   * @param {string} path — URL path
   * @param {object} [body] — request body
   * @returns {Promise}
   */
  function _patch(path, body) {
    return _request("PATCH", path, body);
  }

  /**
   * Perform a DELETE request.
   * @param {string} path — URL path
   * @returns {Promise}
   */
  function _delete(path) {
    return _request("DELETE", path);
  }


  // ── Public API methods ────────────────────────────────────────────

  var API = {

    // ════════════════════════════════════════════════════════════════
    //  Projects
    // ════════════════════════════════════════════════════════════════

    /**
     * List all projects with aggregated task stats.
     * GET /api/projects
     * @returns {Promise<Array>}
     */
    listProjects: function () {
      return _get("/api/projects");
    },

    /**
     * Create a new project.
     * POST /api/projects
     * @param {object} body — {project_id, name?, repo_type?, repo_path?, repo_url?}
     * @returns {Promise<object>} — project object (status 201)
     */
    createProject: function (body) {
      return _post("/api/projects", body);
    },

    /**
     * Get a single project with aggregated stats.
     * GET /api/projects/{id}
     * @param {string} id — project ID
     * @returns {Promise<object>}
     */
    getProject: function (id) {
      return _get("/api/projects/" + encodeURIComponent(id));
    },

    /**
     * Partially update a project.
     * PATCH /api/projects/{id}
     * @param {string} id — project ID
     * @param {object} body — fields to update (name, brief, priority, status)
     * @returns {Promise<object>}
     */
    patchProject: function (id, body) {
      return _patch("/api/projects/" + encodeURIComponent(id), body);
    },

    /**
     * Delete a project (cascade removes tasks + workspace).
     * DELETE /api/projects/{id}
     * @param {string} id — project ID
     * @returns {Promise<object>} — {success: true}
     */
    deleteProject: function (id) {
      return _delete("/api/projects/" + encodeURIComponent(id));
    },

    /**
     * List all tasks for a project.
     * GET /api/projects/{id}/tasks
     * @param {string} projectId — project ID
     * @returns {Promise<Array>}
     */
    listTasks: function (projectId) {
      return _get(
        "/api/projects/" + encodeURIComponent(projectId) + "/tasks"
      );
    },

    /**
     * Submit a project with a brief from meta conversation.
     * POST /api/projects/submit
     * @param {object} body — {project_id, brief, name?, repo_type?, ...}
     * @returns {Promise<object>}
     */
    submitProject: function (body) {
      return _post("/api/projects/submit", body);
    },

    /**
     * Retry a failed project.
     * POST /api/projects/{id}/retry
     * @param {string} id — project ID
     * @returns {Promise<object>}
     */
    retryProject: function (id) {
      return _post("/api/projects/" + encodeURIComponent(id) + "/retry");
    },

    /**
     * Re-run Researcher + Architect planning steps.
     * POST /api/projects/{id}/refresh-planning
     * @param {string} id — project ID
     * @returns {Promise<object>}
     */
    refreshPlanning: function (id) {
      return _post(
        "/api/projects/" + encodeURIComponent(id) + "/refresh-planning"
      );
    },

    // ════════════════════════════════════════════════════════════════
    //  Checkpoints
    // ════════════════════════════════════════════════════════════════

    /**
     * Get the current pending checkpoint for a project, if any.
     * GET /api/meta/{pid}/checkpoint
     * @param {string} pid — project ID
     * @returns {Promise<object|null>} — checkpoint data or null
     */
    getCheckpoint: function (pid) {
      return _get(
        "/api/meta/" + encodeURIComponent(pid) + "/checkpoint"
      );
    },

    /**
     * Approve a checkpoint and resume the pipeline.
     * POST /api/meta/{pid}/checkpoint/approve
     * @param {string} pid — project ID
     * @param {string} cp — checkpoint step ID (or empty string)
     * @param {string} [feedback] — optional approval feedback
     * @returns {Promise<object>}
     */
    approveCheckpoint: function (pid, cp, feedback) {
      var body = {
        project_id: pid,
        checkpoint: cp || "",
      };
      if (feedback) {
        body.feedback = feedback;
      }
      return _post(
        "/api/meta/" + encodeURIComponent(pid) + "/checkpoint/approve",
        body
      );
    },

    /**
     * Reject a checkpoint with required feedback.
     * POST /api/meta/{pid}/checkpoint/reject
     * @param {string} pid — project ID
     * @param {string} cp — checkpoint step ID (or empty string)
     * @param {string} feedback — required rejection reason
     * @returns {Promise<object>}
     */
    rejectCheckpoint: function (pid, cp, feedback) {
      return _post(
        "/api/meta/" + encodeURIComponent(pid) + "/checkpoint/reject",
        {
          project_id: pid,
          checkpoint: cp || "",
          feedback: feedback || "",
        }
      );
    },

    // ════════════════════════════════════════════════════════════════
    //  Meta / Conversation
    // ════════════════════════════════════════════════════════════════

    /**
     * Detect whether a user prompt is about a new project or existing code.
     * POST /api/meta/detect-intent
     * @param {string} prompt — user's initial prompt
     * @returns {Promise<object>} — {intent, reasoning}
     */
    detectIntent: function (prompt) {
      return _post("/api/meta/detect-intent", { prompt: prompt });
    },

    /**
     * Unified pre-project assessment: validate, detect intent, gather brief.
     * POST /api/meta/assess
     * @param {string} prompt — user's message
     * @param {Array} [history] — conversation history [{message, answer}, ...]
     * @returns {Promise<object>}
     */
    assessPrompt: function (prompt, history) {
      return _post("/api/meta/assess", {
        prompt: prompt,
        history: history || [],
      });
    },

    // ════════════════════════════════════════════════════════════════
    //  Runs & Traces
    // ════════════════════════════════════════════════════════════════

    /**
     * List all pipeline runs for a project.
     * GET /api/projects/{pid}/runs
     * @param {string} pid — project ID
     * @returns {Promise<object>} — {project_id, runs: [...]}
     */
    listRuns: function (pid) {
      return _get(
        "/api/projects/" + encodeURIComponent(pid) + "/runs"
      );
    },

    // ── Config-run generic surface ──────────────────────────────────

    /** GET /api/configs — all registered configs with manifests. */
    getConfigs: function () {
      return _get("/api/configs");
    },

    /** GET /api/configs/{name}/manifest */
    getConfigManifest: function (name) {
      return _get("/api/configs/" + encodeURIComponent(name) + "/manifest");
    },

    /** GET /api/runs — all config runs (optional {configName, status} filters). */
    listAllRuns: function (opts) {
      var path = "/api/runs";
      var params = [];
      if (opts && opts.configName) {
        params.push("config_name=" + encodeURIComponent(opts.configName));
      }
      if (opts && opts.status) {
        params.push("status=" + encodeURIComponent(opts.status));
      }
      if (params.length > 0) { path += "?" + params.join("&"); }
      return _get(path);
    },

    /** GET /api/runs/{runId} — full run detail incl. config manifest. */
    getRun: function (runId) {
      return _get("/api/runs/" + encodeURIComponent(runId));
    },

    /** POST /api/runs — start a run of any config. */
    startRun: function (body) {
      return _post("/api/runs", body);
    },

    /** GET /api/runs/{runId}/checkpoint */
    getRunCheckpoint: function (runId) {
      return _get("/api/runs/" + encodeURIComponent(runId) + "/checkpoint");
    },

    /** POST /api/runs/{runId}/checkpoint/approve */
    approveRunCheckpoint: function (runId, body) {
      return _post("/api/runs/" + encodeURIComponent(runId) + "/checkpoint/approve", body || {});
    },

    /** POST /api/runs/{runId}/checkpoint/reject */
    rejectRunCheckpoint: function (runId, body) {
      return _post("/api/runs/" + encodeURIComponent(runId) + "/checkpoint/reject", body || {});
    },

    /**
     * Read execution traces for a pipeline run.
     * GET /api/runs/{runId}/trace
     * @param {string} runId — run ID
     * @param {object} [opts] — optional filters
     * @param {number} [opts.stepInstanceId] — filter by step instance
     * @param {string} [opts.category] — filter by category (prompt, response, tool_call, error)
     * @param {number} [opts.limit] — max entries to return (default 100)
     * @returns {Promise<object>}
     */
    getRunTrace: function (runId, opts) {
      var path = "/api/runs/" + encodeURIComponent(runId) + "/trace";
      if (opts) {
        var params = [];
        if (opts.stepInstanceId !== undefined && opts.stepInstanceId !== null) {
          params.push(
            "step_instance_id=" + encodeURIComponent(opts.stepInstanceId)
          );
        }
        if (opts.category) {
          params.push("category=" + encodeURIComponent(opts.category));
        }
        if (opts.limit) {
          params.push("limit=" + encodeURIComponent(opts.limit));
        }
        if (params.length > 0) {
          path += "?" + params.join("&");
        }
      }
      return _get(path);
    },

    // ════════════════════════════════════════════════════════════════
    //  Workspace browsing
    // ════════════════════════════════════════════════════════════════

    /**
     * Get directory tree of a project's workspace.
     * GET /api/projects/{pid}/workspace/tree
     * @param {string} pid — project ID
     * @param {string} [root] — "dps" (pipeline staging, default) or "code" (project repo)
     * @param {string} [subdir] — optional subdirectory filter
     * @returns {Promise<object>} — {project_id, root, tree: [...]}
     */
    workspaceTree: function (pid, root, subdir) {
      var path = "/api/projects/" + encodeURIComponent(pid) + "/workspace/tree";
      var qs = [];
      if (root) { qs.push("root=" + encodeURIComponent(root)); }
      if (subdir) { qs.push("subdir=" + encodeURIComponent(subdir)); }
      if (qs.length) { path += "?" + qs.join("&"); }
      return _get(path);
    },

    /**
     * Read durable execution traces for a run (or project).
     * GET /api/runs/{runId}/trace
     * @param {string} runId — skillflow run UUID or project_id
     * @param {object} [opts] — {category, limit, stepInstanceId, afterSeq}
     * @returns {Promise<object>} — {run_id, count, traces, next_seq, has_more}
     */
    getTrace: function (runId, opts) {
      opts = opts || {};
      var qs = [];
      if (opts.category) { qs.push("category=" + encodeURIComponent(opts.category)); }
      if (opts.limit) { qs.push("limit=" + encodeURIComponent(opts.limit)); }
      if (opts.stepInstanceId != null) {
        qs.push("step_instance_id=" + encodeURIComponent(opts.stepInstanceId));
      }
      if (opts.afterSeq != null) {
        qs.push("after_seq=" + encodeURIComponent(opts.afterSeq));
      }
      var path = "/api/runs/" + encodeURIComponent(runId) + "/trace";
      if (qs.length) { path += "?" + qs.join("&"); }
      return _get(path);
    },

    /**
     * Read a file from the project workspace.
     * GET /api/projects/{pid}/workspace/file
     * @param {string} pid — project ID
     * @param {string} filePath — relative file path within workspace
     * @param {string} [root] — "dps" (default) or "code" (project repo)
     * @returns {Promise<object>} — {path, content}
     */
    workspaceFile: function (pid, filePath, root) {
      var path = "/api/projects/" + encodeURIComponent(pid) +
        "/workspace/file?path=" + encodeURIComponent(filePath);
      if (root) { path += "&root=" + encodeURIComponent(root); }
      return _get(path);
    },

    // ════════════════════════════════════════════════════════════════
    //  Settings
    // ════════════════════════════════════════════════════════════════

    /**
     * Get current scheduler configuration.
     * GET /api/settings/scheduler
     * @returns {Promise<object>} — {scheduler_type, scheduler_interval?, scheduler_cron?}
     */
    getSchedulerSettings: function () {
      return _get("/api/settings/scheduler");
    },

    /**
     * Update scheduler configuration.
     * POST /api/settings/scheduler
     * @param {object} body — {scheduler_type, scheduler_interval?, scheduler_cron?}
     * @returns {Promise<object>}
     */
    updateSchedulerSettings: function (body) {
      return _post("/api/settings/scheduler", body);
    },

    // ════════════════════════════════════════════════════════════════
    //  Session & Chat History
    // ════════════════════════════════════════════════════════════════

    /**
     * Create a new chat session.
     * POST /api/agent/session/create
     * @returns {Promise<object>} — {session_id}
     */
    createSession: function () {
      return _post("/api/agent/session/create");
    },

    /**
     * Get chat history for a session.
     * GET /api/agent/chat/history?session_id=...
     * @param {string} sessionId — session ID
     * @returns {Promise<object>} — {session_id, messages: [...]}
     */
    getChatHistory: function (sessionId) {
      return _get("/api/agent/chat/history?session_id=" + encodeURIComponent(sessionId));
    },

    /**
     * List chat sessions, optionally filtered by project.
     * GET /api/agent/sessions?project_id=...&limit=20
     * @param {string|null} projectId — optional project filter (null returns all)
     * @returns {Promise<object>} — {sessions: [...]}
     */
    listSessions: function (projectId) {
      var path = "/api/agent/sessions?limit=20";
      if (projectId) {
        path += "&project_id=" + encodeURIComponent(projectId);
      }
      return _get(path);
    },

    /**
     * Save a single chat message to the backend immediately.
     * POST /api/agent/chat/message
     * @param {object} body — {session_id, project_id, role, content}
     * @returns {Promise<object>} — {status: "saved"}
     */
    saveChatMessage: function (body) {
      return _post("/api/agent/chat/message", body);
    },

    // ════════════════════════════════════════════════════════════════
    //  Identity / write permission
    // ════════════════════════════════════════════════════════════════

    /** GET /api/me → { email, can_write, gate_enabled }. */
    me: function () {
      return _get("/api/me");
    },

    /** Toggle client-side read-only mode (writes short-circuit when false). */
    setCanWrite: function (v) {
      _canWrite = !!v;
    },

    /** Current write permission. */
    canWrite: function () {
      return _canWrite;
    },
  };


  // ── Expose globally ───────────────────────────────────────────────

  window.AItelier = window.AItelier || {};
  window.AItelier.API = API;
  window.AItelier.ApiError = ApiError;
})();
