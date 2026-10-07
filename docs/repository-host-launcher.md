# Repository host CPU launcher (operator guide)

The backend has no Docker authority. Its existing repository_executor client
sends one JSON launch request to an operator-enabled host Unix socket. The host
launches an owned disposable container and returns the entry script's actual
stdout, stderr and bare return code. This source is unactivated; normal facility
acceptance still needs independent review and a separately authorized real probe.

## Operator configuration and trust

Example configuration (substitute existing absolute deployment paths and the
reviewed image's actual digest):

```json
{
  "allowed_roots": ["/home/linxuhao/.AItelier/worktrees", "/home/linxuhao/.AItelier/projects"],
  "aitelier_home": "/home/linxuhao/.AItelier",
  "state_db": "/home/linxuhao/.AItelier/aitelier.db",
  "test_image": "sha256:<64 lowercase hexadecimal characters>",
  "executor_entry": "/home/linxuhao/AItelier/core/repository_executor_entry.py",
  "allowed_uids": [1000]
}
```

Only a full SHA256 image reference or repository@sha256 digest is accepted;
mutable tags are refused. Configuration and executor entry are operator-owned
trusted product inputs, never repository/request inputs. state_db is the EXISTING
host business database: the launcher opens it mode=ro to read run_isolation,
without constructing DBManager/SkillFlow, migrating tables or making another
owner ledger. allowed_roots bound source reads, not writable authority.

The service runs as the trusted operator/backend UID. Its socket is mode0600,
and SO_PEERCRED checks the kernel UID, not the PID. Socket ownership and the
configured backend UID must agree before activation. The trusted backend may
submit normal run identities; arbitrary programs using that same UID are inside
this OS trust boundary, not authenticated as a separate restricted principal.
Untrusted repository containers receive neither launcher nor Docker sockets,
backend secrets or host credentials. Do not grant those mounts to make a probe pass.

## Normal ownership and mount binding

run_tests carries its existing framework run_id to pytest and authored-gate
requests. The host requires an existing supported run_isolation row and exact
matching owned repository (worktree/read_snapshot, or recorded direct source).
Caller-supplied repository/root/image fields cannot create new ownership.

Normal report tickets live at gate-reports/SHA256(run_id)/ticket. Only one direct
ticket directory under that run root may be writable; source-directory overlap,
ancestor/child source mounts, foreign-run tickets and report aliases/symlinks are
refused. report_dir must be that same ticket. Only the relay moves to the short
shared filesystem namespace aitelier_home/r (owner UID, mode0700). Its filename
is the complete independently defined BLAKE2s-128 identifier of [run_id,ticket]
plus .s; the full SHA256 report/run namespace is never clipped or changed. The
backend and host use the same pure mapping, so a foreign run/ticket socket is
refused even in that private namespace. The real socket is mode0600 and mounted
read-only at the SAME absolute path in the netnone child. The existing identical
HOME/.AItelier mount makes that namespace visible to backend and host; neither
backend-only /tmp nor abstract Unix sockets provide that cross-namespace path.
A genuinely too-long operator home yields a typed non-pass before bind, never a
silent fallback. Existing listeners are never replaced; cleanup removes only
the exact bound socket inode/ctime. Godot admission/queue rules are unchanged.
Unknown request fields are refused.

The host alone takes the existing four kernel-lock CPU slots from
aitelier_home/cpu-test-slots for socket requests. A backend client does not hold
a second slot while waiting for the host. Direct operator Docker execution keeps
its existing kernel admission. The service additionally bounds request threads
to four and request reads to10seconds; that semaphore is not claimed as global
container ownership. Every Docker launch is --rm --init, networknone,
2CPU/2GiB/512PIDs, cap-dropALL and no-new-privileges. Container labels carry the
existing run/project identity for observation; no new acceptance/quiescence
coverage is inferred merely from labels.

## Operator launch and lifecycle

```bash
python3 -m core.repository_host_launcher --socket /run/aitelier/cpu-launcher.sock --config /etc/aitelier/host-launcher.json
```

The operator supplies ordinary supervised service activation separately, with
this repository available on the host, reviewed dependencies/image installed,
and the backend mounting ONLY this narrow socket at AITELIER_HOST_LAUNCHER_SOCKET.
No compose/systemd activation was performed in this source phase. The source
refuses any already-existing socket path, including a live listener; it does not
unlink a peer's listener to replace it. A stale path needs deliberate operator
diagnosis and cleanup, not automatic ownership inference.

A selector watches the client's connection and the owned Docker CLI output while
execution runs. Disconnect/EOF cancels the operation promptly; protocol write
half-close is explicitly cancellation (one complete request then reply).
Timeout/cancel/failure cleanup addresses only the invocation's UUID container and
its own Docker CLI child, preserving unrelated peers. Missing facility, malformed
result and failed cleanup are non-pass; there is no backend pytest fallback.

CPU source checks with controlled local children do not prove a normal activated
Docker facility. Acceptance still requires actual run_tests and authored-gate
passing/failing fixtures, namespaces/cgroups/mounts, real timeout/disconnect and
an unrelated peer surviving, followed by independent final review. Frozen whole
criteria remain FAIL/UNRUN until that proof exists.
