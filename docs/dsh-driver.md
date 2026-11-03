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
tries OpenCode free → OpenCode Go DeepSeek V4.1 Flash → DeepSeek official.
Free models use `/zen/v1`; Go uses `/zen/go/v1`, both with `GO_API_KEY`.
Free endpoints can refuse third-party clients or exhaust quota; their normal
errors trigger fallback. Provider keys stay in the host gateway, outside DSH.
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

## Checks

Run backend tests only in a throwaway container as required by AGENTS.md.
`integrations/dsh-driver` has `npm test` after `npm ci`: it includes a real pinned
DSH process, a local fake model/MCP/State server, native child execution,
automatic compaction, fresh private-state injection, precompact history grep,
and unified waiting. It makes no paid model calls and touches no live project.
