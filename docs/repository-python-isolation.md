# Repository Python test isolation

`run_tests` sends its pytest command, package import smoke and JUnit output through
`core.repository_executor`. The implementation-time `focused_check` pytest probe
uses that same executor with its host-injected run ownership, narrow targets and
300-second maximum deadline; it never starts repository pytest in the backend.
Missing identity/facility and timeouts are non-passing; cancellation propagates
through the executor's existing cleanup. A transport timeout has no fabricated
exit code, and retained stdout/stderr share the probe's 12000-byte output cap. The declared repository gate runs in the same Docker
adapter, so `tools/godot_gate.py` and its own pytest child also run outside the
backend namespace/cgroup. Node-only npm checks retain their existing behavior.

SkillFlow's native `pytest` tool runs repository tests in the caller's own
process. The host serves that name instead: `api.dependencies.get_tool_loader`
builds `core.tool_routes.AItelierToolLoader`, the ONE loader every engine
dispatch resolves through (agent tools, inline tool steps, lifecycle hooks,
step validation and `{source: {tool: X}}` context sources), and its `load_fn`
returns `native_pytest`, which sends the command to the same executor with the
run's identity and the code root the engine decided. The route is applied after
the loader's cache, so `add_tools_dir` reloads (`register_tool`, `edit_tool`)
and a dynamic re-registration of the name cannot erase it. It keeps the native
`verdict`/`feedback` shape but not the native false greens: exit 5 (nothing
collected), an all-skipped session, a missing JUnit record, a non-Python
target, a missing run identity (the validation route supplies none), an
unavailable executor and a timeout are all non-passing, and the result carries
an explicit `passed` so a lifecycle hook no longer reads a failed verdict as a
pass.

Identity is the run's. On the agent path the host strips caller-supplied
`run_id`, `step_id`, `project_id`, `operation_id`, `config_name`,
`project_root` and `workspace_root` for every executor-routed tool
(`focused_check`, `run_tests`, `pytest`) before SkillFlow injects its own.
`AItelierSkillFlow.register_graph` (and the generated-pipeline validation that
precedes it) refuses a graph whose tool-step params, tool-hook params or
validation specs literally set `run_id`/`step_id`/`project_id`/`operation_id`,
or a non-resolver root (anything but `$VAR[/path]`) for an executor-routed tool;
tool-step and hook dispatch refuse the same literals for runs pinned to older
content. Nested business payloads are not examined.

Each invocation creates one UUID-named disposable container: `--rm --init`, network
none, 2 CPUs, 2 GiB, 512 PIDs, dropped capabilities and no new privileges. Four
kernel-lock slots bound simultaneous CPU tests. Only the repository (read-only),
trusted entry script and explicit report directories are mounted. Tests receive
an explicit environment and project-only PYTHONPATH. Authored Bash gates with
an owned report ticket additionally receive the fixed image-owned
`/opt/aitelier-gate-imports` export, containing only the `docker.godot.godot_harness`
module and its empty package initializers. This supplies the pure request-budget
helpers used by the gate without adding `/app` to its import roots.
The export's bytes are part of the operator-reviewed immutable CPU image; no
repository/request field chooses its source or destination, and no host mount is
added. Missing exports remain real import failures. No provider credentials,
backend secret mounts or Docker socket are passed into the test container.

A changed export requires rebuilding/reviewing the CPU image and updating the
operator's immutable `test_image` pin separately from installing the entry script.
Review its helper semantics against the selected frozen provider before use;
source parity is not proof that an image is installed or an engine request ran.
The provider, relay, network and admission contracts are unchanged.

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
