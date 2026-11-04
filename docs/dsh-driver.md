# Built-in DSH driver MVP

Each State DAG can run one resident DSH process. Start/stop it from **Driver
status** on the dashboard or project catalog. Repeated starts reuse the same
process, driver token, private notebook and persisted session. In the production
host runtime, backend restarts leave drivers running; the independent host
service resumes enabled drivers after its own restart and retries failed
processes with a delay. Starting a driver authorizes work within that project's
existing State rules, so do not start one alongside another controller doing the
same work.

State remains the authority for goals, claims, attempts and acceptance. The
driver uses DSH's native subagents and AItelier's existing MCP pipeline tools.
Native local children support `send_message` and automatic completion notices;
external providers retain their own one-shot or continuable behavior.

The runtime subscribes to State changes and the private driver inbox together,
persists both cursors, and wakes the DSH inbox automatically. There is no driver
`wait` tool: a final reply leaves the agent idle and subscribed. State changes,
including newly reported project issues, inbox messages and native worker
messages/completion wake it while idle or queue input at the next active step.
Normal transport timeouts spend no model turn. An active director receives a
task review after 15 idle minutes; running work, shutdown and explicitly stopped
goal phases do not generate review ticks. While this
resident process supervises its project it renews its own claims and attempts
every 20–30 minutes. Stopping it stops that supervision and renewal. Pipeline
runs remain governed by their existing State lifecycle; stopping the driver
does not silently cancel a pipeline.

DSH's automatic compactor is enabled. Only on the first model request and once
after each successful compaction, the runtime injects:

- Node names and current statuses, without full acceptance/goal text.
- The project's current driver-note index and the driver's own private index.

The director mandate is a native persisted DSH goal. Goal visibility, the State
protocol index and runtime operating instructions are separate system sections;
they are not copied into these project snapshots.

Each injection is a durable `aitelier-driver-state` message keyed to the last
successful compaction event. Ordinary State changes, inbox messages and tool
steps do not append another full snapshot. Process resume reuses its committed
snapshot; failed compaction adds none. Pressure compaction and overflow recovery
both receive fresh State before the next model request, including overflow
retries that do not run another pre-step. Normal event notifications continue;
query live State explicitly when needed between snapshots.

Note bodies stay addressable through State reads. `grep_chat_history(pattern)`
searches this driver's complete native raw session log, including messages
shadowed by compaction. It returns event sequence numbers, event types, matching
lines and excerpts. Literal search is the default; `regex`, `ignore_case`,
`after_seq` and `limit` support focused follow-up searches. There is no second
chat-history store or search index.

`send_user_message(request_key, subject, body, node_key?)` stores one durable
message from the driver to the project's user instead of leaving the reply inside
the session log. It is idempotent per (driver, request_key): the same payload
returns the same message id, and a different payload under the same key is
refused. The dashboard lists those messages newest first under **Driver status**
(`GET /api/dsh-drivers/<project>/messages`, reader permission), with the sending
driver and its time, and does not need that driver to be online or its run
records to exist. A message is presentation only: it never grants a permission,
answers a checkpoint, verifies a node or becomes State evidence.

The dashboard shows all non-retired registered drivers and their online count,
then the same counts per DAG, currently active DSH-native subagent activations
(including nested children), and running pipeline attempts bound to the DAG.
Stopped/failed managed processes count as offline immediately. Native subagent
counts come from paired runtime start/end events, not historical registration
rows. External agents' internal workers are not introspected by this MVP.
Runtime details use the existing private reader permission.

## Runtime and configuration

The production runtime is the user service `aitelier-dsh-host.service` (template
in `deploy/`). It runs `python -m core.dsh_host` on the host, using the same
`dsh_drivers` rows and per-driver state directories as the backend. The shared
`dsh-drivers/runtime.json` setting `{"mode":"host"}` disables backend process
launch/shutdown. Dashboard start/stop updates the existing enabled rows; the
host reconciles them every second. Atomic process markers expire after 15
seconds, so a dead host supervisor cannot leave workers counted online. An
exclusive flock prevents duplicate supervisors; systemd owns all child processes.
The backend retains its Docker deployment.

The host installation at `~/.local/share/aitelier-dsh/releases/<fingerprint>/`
uses Node 24 and the pinned DSH dependencies from the same Dockerfile stage as
the image. The service connects to the
backend via `http://127.0.0.1:4444`, retains per-driver tokens, and uses the host's
existing GitHub secret-file credential helper. Provider API keys remain in the
backend model gateway. No Docker socket is mounted into the backend.

The Docker image also installs `@deepseek-ai/dsh@0.2.1-rc.5` and its lockfile outside
the live source mount, together with Node 24. Rebuild the image to install this
runtime; restarting an old image does not install it. No production deployment
is performed by the implementation tests.

Driver state lives under the configured AItelier data directory's `dsh-drivers/`:
one private directory per generated driver identity. Tokens are mode 0600 and
never appear in dashboard responses. DSH sessions and the cursor file survive
backend restarts. Output/errors go to the driver's private `runtime.log`.

The initial route defaults to `aitelier / driver`. Root, native workers and
compaction use the host's existing `AIGateway`: one sticky gateway per native
session, with normal failover and secret-file resolution. The `driver` route
tries OpenCode Go DeepSeek V4.1 Flash → DeepSeek official. Go uses
`https://opencode.ai/zen/go/v1` with `GO_API_KEY`. Free candidates were removed
after direct calls and a client-compatible request probe both returned 429.
The existing `flash` rotate pool is unchanged. Provider keys stay in the host
gateway, outside DSH.
The bridge preserves text, reasoning and tool-call history. Image blocks are
forwarded as model vision input: each image attachment is read through DSH's
durable attachment service and sent as an OpenAI-style `image_url` data URL
(`integrations/dsh-driver/model.mjs`); an image that cannot be resolved fails the
request instead of being silently dropped.
An explicitly selected `deepseek-official` provider can still use the mounted
`DEEPSEEK_API_KEY` in DSH's private credential store. The route chosen at
creation is kept on subsequent starts.

## Install and update the host service

The repository installer is `deploy/install_dsh_host.py`; the `.service` beside
it is a template and must be rendered rather than copied directly. It needs
Linux, Python 3.12+ with venv/pip (`python3.12-venv` on Ubuntu/Debian), Git,
Docker, and a systemd user session. It can
run before any AItelier container exists. No global Node, npm or DSH install is
required: `docker build --target dsh-runtime` installs the committed npm lockfile,
then a temporary, never-started container exports Node and its dependencies.
The temporary container is removed. Versioned installation directories avoid
overwriting modules used by active drivers.

Follow [the clean-checkout README steps](../README.md#resident-dsh-drivers-install-from-a-clean-checkout-linux).
`--no-start` prepares `.venv` (including `pip install -e .`), the runtime, shared
state, a new identity pepper if needed, `.env` and the generated user unit.
Start the backend with that `.env`, then enable/start the user unit. For a
backend that already uses driver identity, omit `--no-start` to enable/start
the service immediately. This does not restart an already-running service or
any Docker service; a modified unit takes effect on its next restart.

```bash
python3.12 deploy/install_dsh_host.py --help
python3.12 deploy/install_dsh_host.py
systemctl --user status aitelier-dsh-host.service
# After active work has settled, apply a changed unit/runtime:
systemctl --user restart aitelier-dsh-host.service
```

Paths are resolved from the checkout and installing user's `HOME`; neither the
author's username nor `~/AItelier` is required. `--repo`, `--runtime-dir`,
`--api-url` and `--service-name` support explicit installation locations and
isolated rehearsals. `AITELIER_SECRETS_DIR` follows the invoking environment or
`.env`, defaulting to `~/.aitelier-secrets`; it must be a host path. The data
root follows the Compose contract, `$HOME/.AItelier`. Preserve the original
`AITELIER_DRIVER_TOKEN_PEPPER` when restoring an existing identity database:
the installer refuses to silently replace it. Model and GitHub keys are never
printed or embedded in the unit; Git uses the repository's secret-file helper.

For an existing container-mode installation, stop its managed drivers through
the dashboard before running the installer. It refuses to switch an enabled
container controller into host mode, preventing duplicate roots. Existing
driver tokens, sessions, notes and cursors stay in place and the same drivers
can then be started on the host. If driver identity was previously disabled,
recreate the backend with the updated `.env` before starting project drivers.

Boot startup needs an enabled user unit and lingering:
`loginctl enable-linger "$USER"` (may require administrator authorization on
your distribution). Docker manages backend startup separately. A manually
stopped DSH service is not started by `aitelier server`; use `systemctl --user
start aitelier-dsh-host.service`. Stopping the user service preserves enabled
driver rows, so its next start resumes those sessions. To keep one driver off,
stop that project through the dashboard instead.

Existing authenticated writers can use:

```text
GET  /api/dsh-drivers
POST /api/dsh-drivers/{project_id}/start   {"provider":"...", "model":"..."}
POST /api/dsh-drivers/{project_id}/stop
```

The runtime-only context/activity/heartbeat endpoints authenticate the exact
managed driver token. For a local development installation, `AITELIER_DSH_BIN`
can select an installed DSH executable and `AITELIER_DSH_API_URL` overrides the
default loopback backend URL. Use a complete `dsh` installation from this pinned
version, not an older globally installed command.

## Standing director goal and connection recovery

`integrations/dsh-driver/goal.md` seeds each project's standing director mandate
as a native DSH goal when its session has no current goal. The native goal starts
the first director round. When that round ends, the runtime disarms immediate
native continuation without changing the durable active phase, objective or
round counters. Automatic notifications and idle reviews resume work instead of
holding a wait tool open or generating an immediate loop. A stable root-only
system section reads the actual persisted objective, making it visible after
pressure/overflow compaction without duplicating it in State snapshots. Protocol
and runtime tool instructions have their own system sections; native children
keep their assigned task instead of inheriting the director's goal.
Retained project-inbox messages are delivered after the first native goal round
is admitted, so bootstrap messages cannot displace that round.

Session resume preserves goal identity, objective, counters and the existing
project snapshot. The enabled resident host explicitly re-arms an active goal
after restart, while paused, blocked, completed and explicitly cleared goals remain unchanged.
Native round limits remain in force; restart does not reset them or bypass a
blocker. No additional goal store or scheduler is introduced. The director
maintains State/note/inboxes, checks issues and unfinished delivery beyond the
frontier, prioritizes critical defects, and aims for four independent native
subagents when actionable work exists. The owner's push/publish scope,
model preferences and cleanup policy are part of that mandate. At each scheduling
round, new task or worker completion, the director checks ready work and free
workers, dispatches bounded implementation/investigation/verification by default,
and focuses on prioritization, acceptance and integration. An assignment is a
frozen todo plan expanded from a repository template (State attempt, source
base, owned checkout, goal and the template's params) plus a native child; see
“Todo plans”. `run_pipeline`
stays available but is no longer the default. The director continues
independent work and refills available slots after review.
Small fixes, urgent mitigation and work requiring the director's current context
can still be handled directly. Concurrency follows real independent work.

Host drivers use `danger-full-access` and `approval=never`. Startup explicitly
updates resumed sessions through DSH's permission preset API, replacing the old
workspace-write pin. Native children inherit full access and can work in their
own external worktrees with writable Git metadata. Use native file/bash tools
for development, fetch/merge/commit/push and Docker. Each writer still needs an
independent checkout, and AItelier pytest runs only in disposable offline
2-CPU/2-GiB containers, never directly on the host or in production. The backend
binding `/app` maps to the host deployment checkout; other source paths are
unchanged. This profile mounts native Spawn/Fork only. Claude Code and Codex external subagent providers
are not mounted by this MVP; naming a preferred model in the goal does not
install a provider or change the host adapter's default model.

Native Spawn and Fork subagents use the `worker` model route; the director keeps
its configured model (normally `driver`). The completion bridge also identifies
children by their native session id, so resumed children with an old inherited
model selection still use `worker`. Each child binds one gateway: successive
children round-robin over `worker.rotate`, while all tool turns in that child
stay on the same provider unless an endpoint fails. The example pool contains
OpenCodeGo and Ollama Cloud DeepSeek V4.1 Flash, using the existing `GO_API_KEY`
and `OLLAMA_API_KEY` secret files. Both endpoints need configured credentials.

The State subscription requests a 240-second wait, below Node fetch's default
300-second response-header timeout. Ordinary timeouts stay inside the runtime
without model turns. Transport failures retry there too, include the underlying
error code/cause in the status, and clear the displayed error after a successful
subscription response, including an empty timeout response.

## Project coding for the driver and native workers

The former container runtime's `project_work` bridge is not mounted for host
drivers. Old history describing read-only Git, absent Docker or mandatory
`project_work` is superseded by the injected host execution context.

For container-mode installations, `project_work` is a host-scoped native tool, available to the root and every
native child. It sends the actual caller session id with the driver's own token.
The backend validates the assigned State attempt owner/fence and resolves Source
from the project's State binding. Each session/attempt gets a separate existing
Coding Mode workspace: `open`, `read`, `write`, `edit`, `bash`, `deliver`, `push`.
No planning run or additional approval is created. The existing Code Mode
controller still supplies write admissions, conflict-safe Source delivery and
path-scoped commits; its host launcher supplies isolated CPU shells/tests.

For concurrent DSH workers, unrelated Source HEAD movement is allowed during
commit only when every delivered path still matches its original HEAD/index
before-state. The commit CAS uses the current HEAD under the existing admission.
Interactive Coding Mode keeps its original default behavior.

`deliver` accepts `commit_message` and returns `source_head`; `push` requires that
exact `expected_head` and pushes normally to Source's configured origin/current
branch. No caller URL, force push, owner token or Docker socket is exposed.
State project ids also work as `run_pipeline.against_project`: a missing run id
resolves the authenticated State source binding. Exact retained reviews continue
to use their original execution project id.

## Tool discovery

The driver and native Spawn/Fork children keep native coding and coordination
tools directly available; there is no wait tool. MCP definitions stay in DSH's live registry but
are omitted from ordinary model requests. Two runtime tools provide access:

- `search_tools(query, limit=5)` searches tool names/descriptions and returns the
  matching full parameter schemas, up to 10 tools. Use an exact name to inspect
  one known tool. Newly connected, changed or removed MCP tools are reflected
  immediately in the caller's catalog.
- `call_tool(name, arguments)` invokes that exact tool through DSH's existing
  execution chain. For example, after searching for `list_pipelines`, call
  `call_tool(name="mcp__aitelier__list_pipelines", arguments={})`.

Local AgentMCP (`http://127.0.0.1:9003/mcp`), devmcp (`:7676/mcp`) and zvec
(`:7999/mcp`) are connected by default as optional capabilities. A service being
down does not block driver startup. Set `AITELIER_DSH_MCP_AGENTMCP`,
`AITELIER_DSH_MCP_DEVMCP` or `AITELIER_DSH_MCP_ZVEC` in the host service's
environment to override a URL; an empty value disables that server. Restart the
host supervisor after changing this configuration, with native workers settled.
The driver never starts these external services itself.

The standing instructions include an AgentMCP example: search for
`agentmcp image` (or OCR/PDF/audio/video), inspect the returned parameter schema,
then call the exact returned tool name with those fields. Media-tool definitions
remain deferred for both the driver and native children.

The caller's actual agent scope owns lookup, restrictions and execution; the
root's local registrations/restrictions are not substituted for a child. This
changes presentation, not permissions. Known names do not require a prior search.
The wrapper preserves tool validation, cancellation, rendered text/images,
errors and additional context. Its durable call records the target name and
arguments, and its result records the target's rendered output.

Search results enter history only when requested. Compaction needs no catalog
injection or persistent loaded-tool list: search again for a schema when needed.
The native tool prefix stays fixed across searches, which also works with the
host adapter's ordinary function calling and multi-provider failover. Merely
setting DSH's `deferLoading` flag or selecting PTC would not provide this: the
current host adapter does not support incremental tool declarations, and PTC's
generated SDK would still contain the complete visible tool catalog.

## Todo plans

Dispatch is a frozen todo plan plus a native child. A plan is never authored per
dispatch: it is expanded from a reviewed repository template. The plan store,
the templates and every predicate live in Python (`core/todo_cli.py`,
`todo_templates/`); `runtime.mjs` only shells out to
`$AITELIER_TODO_PYTHON -m core.todo_cli <command>` in `$AITELIER_TODO_CWD`, with
`AITELIER_DSH_STATE_DIR` set, parses its stdout JSON and returns it. Values are
passed as `--flag=value`, so a note or goal starting with `-` stays a value.
Exit 2 is returned as a refusal (`refused`), not thrown; any other non-zero
exit, or a call past its bound, is a tool error. `todo_done` and `todo_finish`
are bounded by the plan's own budget from `status` (`timeout_budget_seconds`
for the item, `finish_budget_seconds` for finish) plus 60 s; every other call,
and a plan without the key, gets 60 s. The CLI runs as the leader of its own
process group. On a timeout, or when the tool call is aborted, the group gets
SIGTERM, so the CLI stops the predicates it runs in their own sessions; after 5 s
it gets SIGKILL. No test run or docker client outlives the call. Every
plan result carries `brief`: the CLI's own, or the plan's `status` brief when the
command printed none (`null` only when the plan does not exist).

| Variable | Set by | Meaning |
|---|---|---|
| `AITELIER_TODO_PYTHON` | `core/dsh_driver.py`: `sys.executable` | interpreter that runs the CLI |
| `AITELIER_TODO_CWD` | `core/dsh_driver.py`: the repository root | checkout containing `core/todo_cli.py` and `todo_templates/` |
| `AITELIER_DSH_STATE_DIR` | `core/dsh_driver.py`: the driver's private state directory | plan store `<state dir>/todo/<attempt_id>.json` |
| `AITELIER_TODO_TEMPLATES` | unset (tests only) | read by the CLI: overrides the template directory `todo_templates/` |
| `AITELIER_DSH_IDLE_REVIEW_MS` | unset (tests only) | overrides the 15-minute idle review interval |

Templates. `todo_templates/<name>.yaml` declares `name`, `version`,
`description`, `params` (each with `type` in `str`, `int`, `list[str]`,
`required`, `default`) and `items`, whose predicate args reference a param by
whole value (`$param`) and whose titles may use `${param}`. The CLI validates the
params, expands the items, appends `deliver`, freezes the plan and records the
template's name, version and file sha256 with the params. A template changes only
through a reviewed code change, like the predicate library. When no template
fits a task, add one that way; never improvise items.

Director only, registered on the root agent's scope (children never see them):

- `todo_templates()` → `templates`: the templates with name, version,
  description and params.
- `todo_plan(template, params, goal, attempt_id, node_key, context_hash,
  base_sha, workspace)` → `create --template --params --attempt --node-key
  --context-hash --base-sha --workspace --goal`, with `context_hash` from
  `start_external_attempt`. Any other argument (for example `items`) is refused
  as `unknown_arguments` before the CLI runs; a missing, unknown or wrongly typed
  param is the CLI's refusal.
- `todo_finish(attempt_id)` → `finish --out <state dir>/todo/finish/<attempt_id>`:
  re-evaluates every item and, only if all pass, writes the git bundle, its
  sha256 and the candidate report for `report_external_attempt`. An attempt_id
  that is not a plain file name is refused before any path is built.

Root and every native child, registered host-wide like `project_work`; the
caller is the executing agent (`exec.agent.session.id`), never the root:

- `todo_status(attempt_id)` → `status`: brief, goal, next, done, remaining.
- `todo_begin(attempt_id, item_id)` → `begin --session <caller session>`: binds
  the plan to the calling child and captures the item's pre-state. The CLI
  refuses, under the plan lock, a plan bound to another session
  (`bound_to_other_session`, naming `bound_session`). The runtime retries with
  `--rebind` only when that session is verified dead (not the root, not a live
  child of this process); otherwise the refusal is the result.
- `todo_done(attempt_id, item_id)` → `done`: the harness evaluates the item's
  predicates now (`pass|fail|absent`); a fail keeps the item open.
- `todo_report(attempt_id, item_id, note)` → `report --note`: a free-text note.

DSH's own `todo_write` (from `dsh-base`) stays visible to the root and children.
It is a private checklist with no harness behind it; the plan's `todo_*` tools are
the work contract and the only progress the director sees.

Workers do not judge their own work. In the real `aitelier` MCP server every
State write is an `action` value of one tool, `mcp__aitelier__state_graph_write`.
For every session other than the root, a tool guard installed by tool discovery
applies an allowlist. `state_graph_write` is allowed only with `action`
`report_issue`. Every other action is refused, as is a missing or non-string
`action`. Refused actions include:

- `record_evidence`, `verify_node`, `report_external_attempt`, `start_external_attempt`, `claim_node`;
- `take_over_claim`, `take_over_attempt`, `abandon_external_attempt`, `release_claim`, `heartbeat`;
- `revise_node`, `add_nodes`, `link_issue`, `resolve_issue`;
- any action added later.

Two writes are separate MCP tools, not `state_graph_write` actions, and are
refused for children by name:

- `mcp__aitelier__declare_driver_presence`: marking the director offline would
  let other drivers take over its claims.
- `mcp__aitelier__answer_checkpoint`: a checkpoint is a human decision.

The five judging writes are also refused as bare tool names
(`mcp__aitelier__record_evidence`, `verify_node`, `report_external_attempt`,
`start_external_attempt`, `claim_node`) in case a server ever exposes them. The
aitelier server registers no separate `take_over_*`, `abandon_*` or `release_*`
tool; those exist only as `state_graph_write` actions. The guard applies through
`call_tool` and to direct calls. Its message is “workers do not judge their own
work; the director records State evidence (a subagent may only state_graph_write
report_issue)”. The root is unchanged and keeps everything.

Deliberately left open to children:

- the pipeline tools (`run_pipeline`, `stop_pipeline`, `generate_pipeline`, …);
- the pipeline, role, template and tool edit tools (`edit_*`, `import_pipeline`, `archive_pipeline`);
- the provider and model tools;
- the read tools;
- every other MCP server, the driver inbox tools among them, since they are not `state_graph_write`.

This is a rule on the tool surface, not a security boundary. A child's bash
inherits the driver token (`AITELIER_DRIVER_TOKEN`, `AITELIER_DRIVER_TOKEN_FILE`),
so a direct HTTP call to the State API is not
covered. Per-child tokens are a separate owner decision (design D9).

Handback is produced by the runtime, not by the child's last message. When a
native child ends (`subagent/end`) and a plan whose bound `child_session` is that
exact child still has remaining items, the runtime asks the CLI (`handback`) and
steers the root with an `aitelier-runtime-event` message
`{reason: "todo_unfinished", attempt_id, done, next, remaining, last_observations}`.
`last_observations` is the CLI's object keyed by every item that has not passed,
each with its latest predicate results (`[]` for an item never observed). It is
an object, not the array named in the node goal. If the CLI refuses or fails, the
steer carries `todo_error` instead of empty fields. If the plans cannot be listed
at all, it is `{reason: "todo_handback_failed", child_session, todo_error}`. The
idle review payload adds `todo_unfinished` (plans with remaining items and no
live child), or `todo_error` when the plans cannot be read.

Compaction. The root's `aitelier-driver-state` snapshot (bootstrap and after each
successful compaction) adds `todo: [{attempt_id, node_key, next, remaining_count,
child_alive}]` for unfinished plans. When there are none, the key is absent, and
finished plans drop out. A todo CLI failure never fails the snapshot: it renders
`todo_error` instead.

A host-scoped `agent/pre-step` hook does fire for native children:

- The pinned `compaction-basic` compacts children through the same host-level hook.
- `runtime.todo.test.mjs` observes a child's `compaction/end` followed by the brief in that child's next model request.

After each successful compaction of a child bound to a plan, the runtime appends
one user message `todo brief: …` (source kind `aitelier-todo-brief`, newlines
folded) to that child's next step. If the brief cannot be read, the step proceeds
without it. The brief in every todo result remains the primary recovery channel.

Director flow:

1. Claim the node, then `start_external_attempt`.
2. Pick a template with `todo_templates` and bind its params.
3. Call `todo_plan`.
4. Spawn the child with its attempt_id and the `todo_*` tools.
5. On completion or a `todo_unfinished` handback, re-dispatch or report failed.
6. When the plan is complete, call `todo_finish`, then `report_external_attempt` with its report.
7. Review the delivery and `record_evidence` a separate report.

Deploying a changed `goal.md`. `bindDriverGoal` seeds the native goal only when
a session has none and otherwise keeps the durable objective. A running driver
therefore keeps its old dispatch instructions after a restart, while `driver.md`
(reloaded every start) has the new ones. The restart must re-seed each live
driver's goal without resetting its counters:

1. Render `integrations/dsh-driver/goal.md` with `{{project_id}}` replaced.
2. Run `/goal edit <that text>` on the driver's root session. This keeps the goal
   id, phase and round counters.
3. Confirm the persisted objective with `get_goal`.

Do not use `/goal clear`: a cleared goal is deliberately not recreated on restart.

## Checks

Run backend tests only in a throwaway container as required by AGENTS.md.
`integrations/dsh-driver` has `npm test` after `npm ci`: it includes a real pinned
DSH process, a local fake model/MCP/State server, native child execution,
automatic compaction, fresh private-state injection, precompact history grep,
and automatic idle/busy notifications, issue delivery and idle task reviews.
`runtime.todo.test.mjs` drives two `code_change` template plans through native
children against a fake `core.todo_cli` (set `AITELIER_TODO_TEST_PYTHON`/
`AITELIER_TODO_TEST_CWD` to use a real checkout). It covers:

- the template listing;
- the refusal of free items and of a wrongly typed param;
- a full walk and a runtime handback;
- the `state_graph_write` action refusal;
- the real idle review payload, using a short interval;
- briefs across child and root compaction;
- `todo_finish`;
- a broken CLI degrading the snapshot;
- a dead child's plan rebound by a re-dispatched child;
- budget-derived bounds, and a hung predicate's grandchild stopped with the CLI's process group (SIGTERM, then SIGKILL).

Tool-discovery checks cover parent/child MCP lookup and
dispatch, live registry updates, caller restrictions, errors, cancellation and
unchanged model tool prefixes. It makes no paid model calls and touches no live project.

## Dashboard records and messages

Driver rows are ordered running, waiting, starting, stopping, failed, stopped,
then projects without a managed driver. “Run history” opens the selected driver's
native root session: messages, tool calls/results and compaction events, including
precompact records. Pages hold up to 50 records, with older-page navigation;
individual event excerpts are bounded and marked when truncated.

Enabled drivers publish new records through the existing authenticated private
SSE channel. The view merges sequence numbers to avoid duplicates and reloads the
persisted tail after reconnect. Reading older pages pauses the live view; Refresh
returns to the latest records. History reads use the pinned native persistence
backend with read access; they never start an agent or take its write lease.

The message box sends through the existing driver inbox. Authenticated browser
owners send as owners, without borrowing a registered driver's identity; the
inbox request audit retains that actor. “Sent to driver inbox” confirms delivery,
while the driver's response appears in the live records. The runtime's existing
combined State/inbox subscription steers each arriving message into the agent's
native DSH inbox; there is no native wait tool to wake.

An idle agent starts a turn on that input; a busy one receives it at its next
step, so a message arriving during a model call is not lost. Native child
completion uses DSH's own inbox delivery. Empty or resolved-only subscription
replies advance cursors without starting a model turn.
