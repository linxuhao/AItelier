# Built-in DSH driver MVP

Each State DAG can run one resident DSH process. Start/stop it from **Driver
status** on the dashboard or project catalog. Repeated starts reuse the same
process, driver token, private notebook and persisted session. A normal backend
restart resumes enabled drivers. A failed process remains visibly failed; stop
and start it to retry. Starting a driver authorizes work within that project's
existing State rules, so do not start one alongside another controller doing the
same work.

State remains the authority for goals, claims, attempts and acceptance. The
driver uses DSH's native subagents and AItelier's existing MCP pipeline tools.
Native local children support `send_message` and automatic completion notices;
external providers retain their own one-shot or continuable behavior.

The runtime subscribes to State changes and the private driver inbox together,
persists both cursors, wakes the DSH inbox, and exposes a native **wait()**. Normal
wait timeouts stay inside the runtime and spend no model turn. While this
resident process supervises its project it renews its own claims and attempts
every 20–30 minutes. Stopping it stops that supervision and renewal. Pipeline
runs remain governed by their existing State lifecycle; stopping the driver
does not silently cancel a pipeline.

DSH's automatic compactor is enabled. On startup, resume and after compaction,
before the next model request, the runtime injects:

- Node names and current statuses, without full acceptance/goal text.
- The project's current driver-note index and the driver's own private index.
- The State driver skill/index and the resident-driver instructions.

Note bodies stay addressable through State reads. `grep_chat_history(pattern)`
searches this driver's complete native raw session log, including messages
shadowed by compaction. It returns event sequence numbers, event types, matching
lines and excerpts. Literal search is the default; `regex`, `ignore_case`,
`after_seq` and `limit` support focused follow-up searches. There is no second
chat-history store or search index.

The dashboard shows all non-retired registered drivers and their online count,
then the same counts per DAG, currently active DSH-native subagent activations
(including nested children), and running pipeline attempts bound to the DAG.
Stopped/failed managed processes count as offline immediately. Native subagent
counts come from paired runtime start/end events, not historical registration
rows. External agents' internal workers are not introspected by this MVP.
Runtime details use the existing private reader permission.

## Runtime and configuration

The Docker image installs `@deepseek-ai/dsh@0.2.1-rc.5` and its lockfile outside
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
The bridge preserves text, reasoning and tool-call history; attachments are
represented as handles for native file tools, not sent as model vision input.
An explicitly selected `deepseek-official` provider can still use the mounted
`DEEPSEEK_API_KEY` in DSH's private credential store. The route chosen at
creation is kept on subsequent starts.

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

`integrations/dsh-driver/goal.md` supplies each project's standing director
mandate as `driver_goal` in the required context. Startup, session resume and
post-compaction context include the same project-specific goal. The director
maintains State/note/inboxes, checks issues and unfinished delivery beyond the
frontier, prioritizes critical defects, and aims for four independent pipelines
and four subagents when actionable work exists. The owner's push/publish scope,
model preferences and cleanup policy are part of that mandate.

Native subagents accept a working `cwd`, but this pinned version records the
parent's immutable workspace boundary in local child session headers. Use
independent clones (including their own `.git`) under the driver's workspace;
setting `cwd` to an external shared worktree does not grant write access. This profile
mounts native Spawn/Fork only. Claude Code and Codex external subagent providers
are not mounted by this MVP; naming a preferred model in the goal does not
install a provider or change the host adapter's default model.

The State subscription requests a 240-second wait, below Node fetch's default
300-second response-header timeout. Ordinary timeouts stay inside the runtime
without model turns. Transport failures retry there too, include the underlying
error code/cause in the status, and clear the displayed error after a successful
subscription response, including an empty timeout response.

## Checks

Run backend tests only in a throwaway container as required by AGENTS.md.
`integrations/dsh-driver` has `npm test` after `npm ci`: it includes a real pinned
DSH process, a local fake model/MCP/State server, native child execution,
automatic compaction, fresh private-state injection, precompact history grep,
and unified waiting. It makes no paid model calls and touches no live project.

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
combined State/inbox subscription wakes its native wait when a message arrives.

The wait notification is an edge, not a queue of wake credits. The runtime
checks the native DSH next-step inbox before and after sleeping. Messages that
were already claimed during active work cannot wake a later idle wait; new
messages arriving during a model call remain visible to the following wait.
Native child completion uses DSH's own inbox delivery. Empty or resolved-only
subscription replies advance cursors without starting a model turn.
