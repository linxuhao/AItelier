# Host and initialized runtime deployment observation

The normal host CLI restart/redeploy guard now fetches original runtime facts
from POST /api/admin/deployment-runtime-observation before its existing host
OS/Docker/sidecar measurement. The endpoint is an explicit owner audit: it may
record observed-lost ownership through the existing live SDK, but does not retire
owners, construct a runtime, register graphs, authorize deployment or start work.

The endpoint requires the existing off-tunnel CLI admin token. Cloudflare
requests, including a writer identity or replayed admin token, cannot obtain this
raw inventory. The existing MCP observer remains a writer-authorized redacted
observation with an original digest; that projection is never used here.

The host CLI uses the existing admin header, a loopback HTTP backend URL and the
normal compose service/container identity. Original runtime facts carry their
canonical digest, time, boot ID and live process namespace identity. The host
checks the boot ID, snapshot age (30 seconds), schema and Docker backend PID/mount
namespaces before composing them with its own OS and shared-sidecar observations.
Freshness is checked again after host probes. Missing runtime/host facts, stale,
malformed, projected or mismatched observations remain unavailable or blocking.
There is no competing runtime, raw operation-table scan, new daemon, credential,
or automatic owner release. Existing cutover fence, blocker classification,
override rules and journal finalization are unchanged.

A stopped/uninitialized/unreachable backend cannot provide live runtime facts:
its normal guard refuses explicitly rather than constructing a substitute and
calling it quiet. This change is SOURCE only until independently reviewed and
installed; it does not perform a restart or alter production configuration.
