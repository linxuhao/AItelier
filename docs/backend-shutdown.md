# Backend shutdown

The Docker image command, Compose service command and explicit
`aitelier server --no-docker` entry point give Uvicorn a **5 second request
drain budget**. SSE heartbeats and MCP streams can otherwise remain open
indefinitely. After the budget expires Uvicorn cancels the remaining HTTP
tasks, then sends the application lifespan shutdown event. The existing MCP
session-manager teardown and scheduler shutdown still run.

Five seconds lets ordinary short requests finish while leaving cleanup time
inside Docker's default 10 second stop window. This bounds request drain,
not arbitrary application cleanup or external worker execution. Clients must
reconnect after restart; interrupted persisted claims use the existing
singleton startup recovery, including its unsettled-operation checks.

Compose explicitly sets the command so a newly created container using an
existing image and the live source mount gets the same timeout. An existing
container retains its configured command across `docker restart`; applying
this startup change to that container requires recreation. No deployment is
performed by this source change. Direct custom Uvicorn invocations must also
include `--timeout-graceful-shutdown 5`.
