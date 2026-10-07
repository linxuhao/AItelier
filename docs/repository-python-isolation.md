# Repository Python test isolation

`run_tests` sends its pytest command, package import smoke and JUnit output through
`core.repository_executor`. The declared repository gate runs in the same Docker
adapter, so `tools/godot_gate.py` and its own pytest child also run outside the
backend namespace/cgroup. Node-only npm checks retain their existing behavior.

Each invocation creates one UUID-named disposable container: `--rm --init`, network
none, 2 CPUs, 2 GiB, 512 PIDs, dropped capabilities and no new privileges. Four
kernel-lock slots bound simultaneous CPU tests. Only the repository (read-only),
trusted entry script and explicit report directories are mounted. Tests receive
an explicit environment and project-only PYTHONPATH; no provider credentials,
backend secret mounts or Docker socket are passed into the test container.

The authored gate still reaches its existing Godot admission relay. Its HTTP
requests cross a per-ticket Unix socket through a loopback bridge in the isolated
container; the relay retains admission, render queue and observation ownership.
This bridge is not general network access. No engine calls are bypassed.

Missing Docker CLI/daemon/image, full slots, malformed result, timeout or cancelled
call is non-passing, with no local pytest fallback. Cleanup addresses only that
invocation's random container name. Source and JUnit/node-id baseline parsing are
preserved. Missing-name library introspection is no longer attempted in the
backend: its interpreter is not the tested image.

Deployment is separate: the current production backend has no Docker execution
facility and therefore refuses these commands after this source is installed.
One AUTHORIZED product launch surface now exists for it: when the backend sets
`AITELIER_HOST_LAUNCHER_SOCKET`, `repository_executor.execute` sends a validated
launch request over that 0600 local Unix socket to the operator-owned host
launcher service (`core/repository_host_launcher`), which alone builds and runs
the bounded Docker argv from operator configuration (allowed roots, reviewed
immutable test image, trusted entry) and still refuses on any failure with no
in-backend fallback. See `docs/repository-host-launcher.md`; activation and
compose/systemd wiring remain an operator, root-read action.
Do not mount backend secrets or a daemon socket into the test container. Test-image
project dependencies must be supplied in a reviewed image before launch: network
none deliberately prevents automatic dependency provisioning during tests.
