# Repository host CPU launcher (operator guide)

## Why

Repository Python test gates must run in owned disposable CPU containers, not
in the production backend. The original Docker adapter
(``core/repository_executor``) works only where the operator already holds
the Docker CLI; a normal backend has neither the Docker CLI nor the Docker
socket, and must never gain them. This document describes the narrow,
AUTHORIZED product launch surface that closes that gap without inventing a
second runtime, ledger or queue framework.

## Architecture

```
backend (no docker) --local Unix socket 0600--> host launcher service
    core/repository_executor._launch_via_host        core/repository_host_launcher.serve
        one JSON request per connection                 validates policy, builds the ONLY
        honest non-pass on any failure                  Docker argv, runs it, cleans up

host launcher --> docker run --rm --init --network none --cpus 2 --memory 2g
                  --pids-limit 512 --cap-drop ALL --security-opt no-new-privileges
                  --user <owner>:<group> -v <repo>:ro -v <entry>:/executor.py:ro
                  --entrypoint python3 <reviewed test image> /executor.py <payload>
```

- The backend sets ``AITELIER_HOST_LAUNCHER_SOCKET`` to the launcher socket
  path. ``repository_executor.execute`` then routes over the socket; a
  connect failure, refusal, malformed reply or timeout raises
  ``IsolationUnavailable`` so ``run_tests`` records an honest non-pass
  (``infrastructure_unavailable``, ``skipped_because=cpu_executor_unavailable``)
  with **no in-backend pytest or Docker fallback**.
- The existing four global CPU execution slots, the container resource
  bounds (2 CPU / 2 GiB / 512 pids), ``--cap-drop ALL``,
  ``no-new-privileges``, ``--network none``, UUID container names and the
  per-invocation ``docker rm -f <own UUID name>`` cleanup are preserved.
  Cleanup never touches other containers.
- The existing relay host-ticket RPC is unchanged: the backend still creates
  the admission relay socket and passes its path; the container mounts it
  read-only and forwards the existing HTTP wire.
- No second database, runtime, ledger, heartbeat, GO token or general queue
  is introduced.

## Operator configuration

``/etc/aitelier/host-launcher.json`` (owner-readable):

```json
{
  "allowed_roots": ["/srv/aitelier/projects"],
  "report_roots": ["/srv/aitelier/reports"],
  "test_image": "aitelier-tests:2026.10",
  "executor_entry": "/srv/aitelier/repository_executor_entry.py",
  "allowed_uids": [998]
}
```

- ``allowed_roots``: canonical project/worktree roots the launcher may mount
  (``realpath``-resolved; symlink escapes are refused).
- ``report_roots``: where gate report directories and relay sockets may live.
- ``test_image``: the reviewed, immutable test image. **The image never comes
  from the request.**
- ``executor_entry``: the trusted in-container executor entry script.
- ``allowed_uids``: backend UIDs allowed to connect (checked via
  ``SO_PEERCRED``); omit to allow the socket file permissions alone to gate.

## Launch and systemd

```ini
# /etc/systemd/system/aitelier-cpu-launcher.service
[Unit]
Description=Aitelier repository CPU test container launcher
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/python3 -m core.repository_host_launcher \
    --socket /run/aitelier/cpu-launcher.sock \
    --config /etc/aitelier/host-launcher.json
WorkingDirectory=/srv/aitelier/app
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

The service binds ``/run/aitelier/cpu-launcher.sock`` with mode ``0600``
(forced after bind, independent of umask) and serves at most four
concurrent containers (the existing global bound).

## Security boundary (what is refused)

Every request is validated before any argv exists. Refused outright:

- any ``op`` other than ``launch``, or a non-JSON / oversized request;
- a ``repo``, gate script, writable dir, report dir or relay socket that
  does not canonically resolve inside the operator-allowed roots;
- a missing relay socket (the backend must create it first);
- any image other than the configured one — no arbitrary mounts, no
  attacker-chosen images, no privileged containers;
- any command form outside the three existing ones: ``pytest`` module runs,
  the import smoke (performed by the container entry via ``import_module``),
  and the authored gate script executed with ``bash`` from inside the repo;
- any token containing shell metacharacters or newline — no shell
  interpolation, no raw Docker argv, no daemon flags;
- connections from a UID not in ``allowed_uids``.

No symlink jail escape, host secret path, Docker socket or privileged
container is ever mounted; the repo is read-only, writable dirs and the
relay socket are the only writable/read attachments.

## Timeout, cancellation and disconnect

The launcher bounds each Docker run at ``timeout + 15`` seconds. On
container failure, timeout, backend cancellation or client disconnect, the
launcher removes only the UUID-named container it created for that request
and kills only its own Docker CLI child; unrelated runs and peers survive.

## Independent test plan (for the separately registered evaluator)

All tests below are **UNRUN** in this source step; this is a plan, not a
passing claim.

1. ``tests/unit/test_repository_host_launcher.py`` (authored, source-only):
   policy refusals (roots, symlinks, shell metacharacters, entrypoints,
   gate-script location, timeouts, config shape), argv pinning
   (image/bounds/flags), backend routing over a fake host socket, honest
   ``IsolationUnavailable`` without local fallback, timeout mapping.
2. Positive facility probe (owned evaluator, CPU host): start the service
   with a scratch config, run a trivial failing pytest repo through
   ``run_tests`` with ``AITELIER_HOST_LAUNCHER_SOCKET`` set; expect real
   bare RC, stdout/stderr and JUnit preserved and an honest FAIL verdict.
3. Genuinely failing fixture: assert the report preserves bare exit code,
   streams and JUnit, and that no green is synthesized.
4. Refusal inverse tests: malformed repo path, foreign image request,
   injection token, out-of-root report dir — each must yield
   ``ok: false`` and **no** container creation (inspect ``docker ps -a``
   before/after).
5. Cancel/disconnect: close the client mid-request; assert the launcher's
   own UUID container is gone and unrelated containers survive.
6. Missing facility: stop the service, run the gate — assert explicit
   non-pass with ``cpu_executor_unavailable`` and no in-backend pytest run.
7. Regression: existing ``run_tests`` and the authored Godot gate entry
   points behave unchanged when the launcher is healthy, and honestly
   non-pass when it is not.

No acceptance is claimed by this document; criteria stay FAIL/UNRUN until
the root non-author review plus an actual facility run through the normal
shipped path.
