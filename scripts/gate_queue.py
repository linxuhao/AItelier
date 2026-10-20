#!/usr/bin/env python3
"""Host-side FIFO queue in front of the Godot gate's globally exclusive render lock.

WHY. A full gate run on the wuxia game takes ~55 minutes and the sidecar's render
lock is globally exclusive: exactly one engine run at a time. Today every subagent
starts its own gate with `gate_run.sh` and then SITS THERE waiting, so N agents on
one lock burn N full LLM contexts per wake and buy nothing.

So: `submit` writes a ticket to disk and returns IMMEDIATELY without touching the
engine — the calling process can exit and the gate still runs to completion — and a
single host-side daemon drains the queue FIFO, one engine run at a time.

NOT a pipeline. A pipeline run is scheduler-owned and core/scheduler.py advances one
project per tick, so the gate would queue behind every other run; and a tool step
writing into the code tree deadlocks against skillflow's clean-worktree guard.

This WRAPS `~/.AItelier/bin/gate_run.sh` (battle-tested: rc 64 not a git repo,
rc 65 dirty tree, rc 66 tree changed under the gate, throwaway container so a
container recreation cannot kill the gate). It does not replace it.

Durability: the queue IS the filesystem. Unowned v1 tickets retain their legacy
restart behavior. Bound v2 tickets keep permanent admission/invocation identity;
uncertain recovery holds and never automatically relaunches admitted work.

States:
  queued   ticket on disk, engine not yet touched
  held     bound ticket needs explicit disposition/recheck; no automatic State polling
  running  admitted launcher invocation (may still be waiting for the render lock)
  done     the gate delivered a verdict; see `verdict` (passed/failed) + report_dir
  failed   no verdict was produced (not a repo, dirty tree, poisoned tree, infra)
  cancelled  taken out of the queue by `cancel` before it ever started; see
           `cancellation`. Not done/failed: no exit code, no verdict, no report.

`done` is about DELIVERY, not about the game passing. Read `verdict`.
Ordinary submit requires --registrations/--purpose and the existing CLI venv plus
GATE_QUEUE_AITELIER_ROOT and own AITELIER_DRIVER_ID. Explicit submit-unowned and
read-only/v1 commands remain stdlib-only. See docs/host-gate-queue.md.

CANCEL. `cancel TICKET --expected-sha SHA --reason TEXT [--actor NAME]` withdraws ONE
queued ticket that never started (nothing in it but what `submit` wrote, no report
directory). It takes mutation.lock, non-blocking, for the short read/check/write
transition (rc 4 if busy). The daemon keeps daemon.lock for single-instance ownership
but releases mutation.lock while a gate runs. Cancel never signals or stops anything,
and a refusal writes nothing. The ticket keeps every original field; the
exact pre-cancel ticket.json is kept in `cancellation.original_text` with its sha256 in
`cancellation.original_sha256`. rc: 0 cancelled or already-cancelled, 3 unknown ticket,
4 mutation lock held, 5 refused (wrong sha, not queued, started, malformed...).
A cancelled ticket is terminal; resubmit explicitly if work is wanted again.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Preserve the launcher's environment before cli.client's lazy .env import.
_LAUNCH_ENV = dict(os.environ)
for _name in ("AITELIER_DRIVER_ID", "AITELIER_DRIVER_TOKEN", "AITELIER_DRIVER_TOKEN_FILE",
              "GATE_QUEUE_AITELIER_ROOT"):
    _LAUNCH_ENV.pop(_name, None)

HOME = Path(os.environ.get("HOME", Path.home()))
ROOT = Path(os.environ.get("GATE_QUEUE_DIR", HOME / ".AItelier/gate-queue"))
REPORTS = Path(os.environ.get("GATE_QUEUE_REPORTS", HOME / ".AItelier/gate-reports"))
GATE_RUN = Path(os.environ.get("GATE_RUN_SH", HOME / ".AItelier/bin/gate_run.sh"))
TICKETS = ROOT / "tickets"
LOCK = ROOT / "daemon.lock"
MUTATION_LOCK = ROOT / "mutation.lock"
PIDFILE = ROOT / "daemon.pid"
STOPFILE = ROOT / "stop"
DAEMON_LOG = ROOT / "daemon.log"
SUFFIX = "gate"  # gate_run.sh writes <SUFFIX>.gate.{log,exit,head}
POLL_SECONDS = float(os.environ.get("GATE_QUEUE_POLL", "2"))
TICKET_ID = re.compile(r"gq-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")  # what cmd_submit mints
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
# Exactly the fields cmd_submit writes. run_one() adds started_at, so any ticket the
# daemon ever picked up -- even one reconcile() put back to queued -- has more.
SUBMITTED_KEYS = frozenset(("ticket", "worktree", "sha", "label", "state", "submitted_at",
                            "report_dir", "gate_exit_code", "verdict", "error"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = "%s %s\n" % (now(), message)
    ROOT.mkdir(parents=True, exist_ok=True)
    with DAEMON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    sys.stderr.write(line)


# --- tickets ----------------------------------------------------------------

@contextmanager
def mutation_lock():
    """Serialize only ticket transitions, never a gate's execution or adoption wait."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with MUTATION_LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def ticket_path(ticket: str) -> Path:
    return TICKETS / ticket / "ticket.json"


def read_ticket(ticket: str) -> dict:
    return json.loads(ticket_path(ticket).read_text(encoding="utf-8"))


def durable_write(path, raw, exclusive=False):
    with path.open("xb" if exclusive else "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_ticket(record: dict) -> None:
    path = ticket_path(record["ticket"])
    raw = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    if len(raw) > 1024 * 1024:
        raise QueueRefusal("ticket exceeds 1 MiB; refusing rather than truncating")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    durable_write(tmp, raw)
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def all_tickets() -> list[dict]:
    out = []
    for d in sorted(TICKETS.glob("*/ticket.json")):
        try:
            out.append(json.loads(d.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("submitted_at", ""))


def report_parent(ticket: str) -> Path:
    return REPORTS / ticket


# --- submit -----------------------------------------------------------------

def submitted_record(args) -> dict:
    worktree = Path(args.worktree).resolve()
    ticket = "gq-%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                           uuid.uuid4().hex[:8])
    return {
        "ticket": ticket,
        "worktree": str(worktree),
        "sha": args.sha,
        "label": args.label or "",
        "state": "queued",
        "submitted_at": now(),
        "report_dir": None,
        "gate_exit_code": None,
        "verdict": None,
        "error": None,
    }


def cmd_submit_unowned(args) -> int:
    require(args.unowned_reason.strip() and len(args.unowned_reason) <= 4000,
            "--unowned-reason must be nonempty, at most 4000 characters")
    # Keep the exact v1 ticket fields so the accepted cancellation validator stays unchanged.
    record = submitted_record(args)
    with mutation_lock():
        write_ticket(record)
    print(record["ticket"])
    if not daemon_alive():
        print("warning: no gate-queue daemon is running — this ticket stays "
              "queued until one starts (%s daemon)" % Path(__file__).name,
              file=sys.stderr)
    return 0


# --- status -----------------------------------------------------------------

def cmd_status(args) -> int:
    try:
        record = read_ticket(args.ticket)
    except OSError:
        print("unknown ticket: %s" % args.ticket, file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    print("ticket:   %s" % record["ticket"])
    print("state:    %s" % record["state"])
    print("worktree: %s" % record["worktree"])
    print("sha:      %s" % record["sha"])
    print("submitted:%s" % record["submitted_at"])
    for key in ("started_at", "finished_at", "gate_exit_code", "verdict", "error"):
        if record.get(key) not in (None, ""):
            print("%-10s%s" % (key + ":", record[key]))
    if record["state"] == "done":
        print("report_dir: %s" % record["report_dir"])
    elif record["state"] == "queued":
        ahead = [r for r in all_tickets()
                 if r["state"] == "queued"
                 and r["submitted_at"] < record["submitted_at"]]
        print("ahead_in_queue: %d" % len(ahead))
    elif record["state"] == "cancelled":
        info = record.get("cancellation") or {}
        print("cancelled: %s by %s -- %s" % (info.get("cancelled_at"), info.get("actor"),
                                             info.get("reason")))
        print("preimage: sha256 %s" % info.get("original_sha256"))
    return 0


def cmd_list(args) -> int:
    for r in all_tickets():
        print("%-8s %-28s %s %s" % (r["state"], r["ticket"], r["sha"][:12],
                                    r.get("label", "")))
    return 0


# --- cancel -----------------------------------------------------------------

def cancel_preimage_problem(record: dict) -> str | None:
    """Why a `cancelled` ticket carries no verifiable cancellation, or None.

    The ticket keeps every original field; `cancellation.original_text` is the exact
    ticket.json it replaced and `original_sha256` is that file's hash, so the
    pre-cancel bytes can be re-derived and checked from the ticket alone."""
    info = record.get("cancellation")
    if record.get("state") != "cancelled" or not isinstance(info, dict):
        return "state %r carries no cancellation record" % (record.get("state"),)
    text = info.get("original_text")
    if (not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != info.get("original_sha256")):
        return "the recorded preimage does not match its sha256"
    try:
        original = json.loads(text)
    except ValueError:
        return "the recorded preimage is not JSON"
    kept = {k: v for k, v in record.items() if k not in ("state", "cancellation")}
    if (not isinstance(original, dict) or original.get("state") != "queued"
            or kept != {k: v for k, v in original.items() if k != "state"}
            or info.get("expected_sha") != record.get("sha")):
        return "the ticket's fields differ from its recorded preimage"
    return None


def cancel_unstarted_problem(record: dict) -> str | None:
    """Why this is not a queued ticket that never started, or None.

    `cmd_submit` writes exactly SUBMITTED_KEYS with every result field null. Anything the
    daemon does adds a key or fills one, and a started ticket also has a report directory."""
    if record.get("state") != "queued":
        return "state is %r, not 'queued'" % (record.get("state"),)
    if set(record) != SUBMITTED_KEYS:
        return "fields differ from what `submit` writes: %s" % sorted(set(record) ^ SUBMITTED_KEYS)
    for key in ("report_dir", "gate_exit_code", "verdict", "error"):
        if record[key] is not None:
            return "%s is already set (%r)" % (key, record[key])
    parent = report_parent(record["ticket"])
    if os.path.lexists(parent):
        return "a report directory already exists: %s" % parent
    extra = sorted(p.name for p in ticket_path(record["ticket"]).parent.iterdir()
                   if p.name not in ("ticket.json", "ticket.tmp"))
    if extra:
        return "the ticket directory holds other files: %s" % extra
    return None


def cmd_cancel(args) -> int:
    """Withdraw ONE queued, never-started ticket, keeping its history.

    Decided under mutation.lock (flock, non-blocking; its inode is never replaced).
    Admission and finalization share this short lock. daemon.lock remains exclusively
    the daemon's lifetime lock. A refusal writes nothing; nothing is stopped."""
    def refuse(message: str, rc: int = 5) -> int:
        print("refused: %s (nothing was changed)" % message, file=sys.stderr)
        return rc

    reason = args.reason.strip()
    if not TICKET_ID.fullmatch(args.ticket):
        return refuse("not a ticket id: %r" % args.ticket)
    if not FULL_SHA.fullmatch(args.expected_sha):
        return refuse("--expected-sha must be a full lowercase 40-hex sha, got %r" % args.expected_sha)
    if not reason:
        return refuse("--reason must not be empty")
    if not ROOT.is_dir():
        print("unknown ticket: %s (no queue at %s)" % (args.ticket, ROOT), file=sys.stderr)
        return 3
    fd = os.open(MUTATION_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return refuse("another ticket transition holds %s" % MUTATION_LOCK, 4)
        # Everything below is read, checked and written while holding the lock.
        path = ticket_path(args.ticket)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            print("unknown ticket: %s" % args.ticket, file=sys.stderr)
            return 3
        if not stat.S_ISREG(mode) or path.parent.is_symlink():
            return refuse("ticket.json is not a regular file in a real ticket directory")
        raw = path.read_bytes()
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError is one
            return refuse("ticket.json is malformed")
        if not isinstance(record, dict):
            return refuse("ticket.json is not a JSON object")
        if record.get("ticket") != args.ticket:
            return refuse("the record names ticket %r, not %r" % (record.get("ticket"), args.ticket))
        if record.get("sha") != args.expected_sha:
            return refuse("ticket sha is %r, not the expected %s" % (record.get("sha"),
                                                                    args.expected_sha))
        if record.get("state") == "cancelled":
            problem = cancel_preimage_problem(record)
            if problem:
                return refuse("ticket is 'cancelled' but %s" % problem)
            info = record["cancellation"]  # someone's decision already stands: report it, write nothing
            print("already-cancelled %s by %s at %s: %s" % (args.ticket, info.get("actor"),
                                                            info.get("cancelled_at"),
                                                            info.get("reason")))
            return 0
        problem = cancel_unstarted_problem(record)
        if problem:
            return refuse(problem)
        try:
            os_user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            os_user = "uid:%d" % os.getuid()
        info = {"cancelled_at": now(), "actor": args.actor.strip() or os_user, "os_user": os_user,
                "reason": reason, "expected_sha": args.expected_sha, "original_state": "queued",
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "original_text": raw.decode("utf-8")}
        write_ticket(dict(record, state="cancelled", cancellation=info))
        log("cancelled %s @%s by %s: %s (original ticket sha256 %s)"
            % (args.ticket, args.expected_sha[:12], json.dumps(info["actor"], ensure_ascii=False),
               json.dumps(reason, ensure_ascii=False), info["original_sha256"]))
        print("cancelled %s sha=%s original_sha256=%s" % (args.ticket, args.expected_sha,
                                                          info["original_sha256"]))
        return 0
    finally:
        os.close(fd)


# --- State-owned tickets (the client is imported only on these paths) ---------

MAX_BYTES = 1024 * 1024
SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CONTEXT_FIELDS = ("project_id", "node_key", "node_revision", "contract_hash", "attempt_id",
                  "context_hash", "owner_driver_id", "owner_fence")
OBSERVATION_FIELDS = frozenset(("attempt_id", "observation_id", "expected_version", "context_hash",
                              "status", "report_ref", "report_sha256", "quiescent", "artifact",
                              "artifact_kind", "detail", "fence"))


class QueueRefusal(Exception):
    def __init__(self, message, rc=5):
        super().__init__(message)
        self.rc = rc


class StateUnknown(QueueRefusal):
    def __init__(self, message, receipt=None):
        super().__init__(message, 7)
        self.receipt = receipt


def require(condition, message):
    if not condition:
        raise QueueRefusal(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def receipt_input(filename):
    """Cooperative same-uid boundary; never follow a receipt symlink or truncate it."""
    path = Path(filename)
    require(path.is_absolute(), "receipt path must be absolute")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(),
            "receipt must be a regular nonsymlink file owned by this uid")
    require(info.st_size <= MAX_BYTES, "receipt exceeds 1 MiB")
    raw = path.read_bytes()
    require(len(raw) <= MAX_BYTES, "receipt exceeds 1 MiB")
    value = json.loads(raw)
    return value, raw


def preserve(record, category, raw):
    """Content addressed siblings, never raw ticket histories nested in JSON."""
    require(len(raw) <= MAX_BYTES, "receipt exceeds 1 MiB")
    sha = hashlib.sha256(raw).hexdigest()
    path = ticket_path(record["ticket"]).parent / category / (sha + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == raw, "immutable receipt changed")
    else:
        durable_write(path, raw, exclusive=True)
    return {"path": str(path), "sha256": sha}


def transition(record, event):
    raw = ticket_path(record["ticket"]).read_bytes()
    history = list(record.get("history", []))
    require(len(history) < 128, "ticket transition history is full")
    history.append(dict(preserve(record, "history", raw), event=event, at=now()))
    record["history"] = history
    record["ticket_revision"] += 1
    write_ticket(record)


@contextmanager
def client_mutation():
    ROOT.mkdir(parents=True, exist_ok=True)
    with MUTATION_LOCK.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QueueRefusal("mutation.lock busy; no State observation forwarded", 4) from None
        yield


def _state_adapter(expected_driver_id):
    root_text = os.environ.get("GATE_QUEUE_AITELIER_ROOT", "")
    root = Path(root_text)
    recipe = ("use the recipe interpreter, the existing AItelier .venv/bin/python, with explicit "
              "GATE_QUEUE_AITELIER_ROOT and your own AITELIER_DRIVER_ID")
    require(root.is_absolute() and root.is_dir()
            and all((root / file).is_file() for file in ("cli/client.py", "core/driver_credentials.py")),
            "adapter-unavailable: root; " + recipe)
    root = root.resolve()
    sys.path.insert(0, str(root))
    try:
        import cli.client as client_module
        import core.driver_credentials as credentials
    except ImportError as exc:
        raise QueueRefusal("adapter-unavailable: %s not importable by %s; %s"
                           % (exc.name or "client", sys.executable, recipe)) from None
    for module in (client_module, credentials):
        require(Path(module.__file__).resolve().is_relative_to(root),
                "adapter-unavailable: shadowed client module; " + recipe)
    try:
        client = client_module.APIClient()
    except credentials.CredentialError as exc:
        raise QueueRefusal("adapter-unavailable: " + str(exc)) from None
    headers = client._client.headers
    require("X-AItelier-Driver-Token" in headers and "X-AItelier-Admin-Token" not in headers,
            "adapter-unavailable: no-own-driver-credential; " + recipe)
    try:
        response = client._client.get("/api/drivers/me")
        response.raise_for_status()
        me = response.json()
    except Exception as exc:
        raise QueueRefusal("adapter-unavailable: WHOAMI " + type(exc).__name__) from None
    require(isinstance(me, dict) and me.get("enabled") is True and me.get("kind") == "driver"
            and me.get("driver_id") == expected_driver_id
            and me.get("actor") == "driver:" + expected_driver_id
            and "is_admin" in me,
            "adapter-unavailable: WHOAMI does not match own controller")
    provenance = dict(python=sys.executable, python_version=sys.version.split()[0],
                      aitelier_root=str(root), me_actor=me["actor"],
                      cli_client_py_sha256=hashlib.sha256(Path(client_module.__file__).read_bytes()).hexdigest(),
                      driver_credentials_py_sha256=hashlib.sha256(Path(credentials.__file__).read_bytes()).hexdigest(),
                      platform_modules_loaded=sorted(name for name in sys.modules
                                                     if name.split(".")[0] in ("cli", "core", "api")))
    return StateAdapter(client, provenance)


class StateAdapter:
    def __init__(self, client, provenance):
        self.client = client
        self.provenance = provenance

    def query(self, action, arguments, timeout=10):
        response = None
        try:
            response = self.client._client.post("/api/state/query/" + action,
                                                json=arguments, timeout=timeout)
            require(len(response.content) <= MAX_BYTES, "State query response exceeds 1 MiB")
            response.raise_for_status()
            value = response.json()
            if isinstance(value, dict) and "result" in value:
                raise ValueError("wrapped State response")
            return value
        except Exception as exc:
            receipt = dict(action=action, arguments=arguments, error=type(exc).__name__)
            if response is not None:
                receipt.update(http_status=response.status_code,
                               response_sha256=hashlib.sha256(response.content).hexdigest())
                if len(response.content) <= MAX_BYTES:
                    receipt["response_text"] = response.content.decode("utf-8", errors="replace")
            raise StateUnknown("State query %s unknown (%s)" % (action, type(exc).__name__), receipt) from None

    def snapshot(self, contexts):
        deadline = time.monotonic() + 30
        out = []
        for context in contexts:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StateUnknown("State batch exceeded 30 seconds")
                value = self.query("get_node", {"project_id": context["project_id"],
                                                "node_key": context["node_key"]}, min(10, remaining))
                result = normalize_context(context, value)
                out.append(dict(context_id=context["attempt_id"], **result, snapshot=value))
            except (QueueRefusal, KeyError, TypeError, ValueError) as exc:
                out.append(dict(context_id=context["attempt_id"], eligibility="unknown",
                                reason=str(exc) if isinstance(exc, QueueRefusal) else "malformed State context",
                                failure_receipt=getattr(exc, "receipt", None)))
        return out


def normalize_context(bound, reply):
    require(isinstance(reply, dict), "node response is not an object")
    node, attempts, claims = reply.get("node"), reply.get("attempts"), reply.get("claims")
    require(isinstance(node, dict) and isinstance(attempts, list) and attempts
            and isinstance(claims, list), "missing node/attempts/claims")
    require(all(isinstance(a, dict) and type(a.get("seq")) is int for a in attempts)
            and all(attempts[i]["seq"] > attempts[i + 1]["seq"] for i in range(len(attempts) - 1)),
            "attempts are not ordered by seq DESC")
    if attempts[0].get("attempt_id") != bound["attempt_id"]:
        return dict(eligibility="stale", reason="newest attempt differs")
    require(node.get("project_id") == bound["project_id"] and node.get("node_key") == bound["node_key"]
            and node.get("revision") == bound["node_revision"]
            and node.get("contract_hash") == bound["contract_hash"], "node identity changed")
    require(isinstance(node.get("status"), str) and node["status"], "missing node status")
    if node.get("status") == "SUPERSEDED":
        return dict(eligibility="stale", reason="node superseded")
    current = attempts[0]
    require(type(current.get("owner_fence")) is int and type(node.get("revision")) is int,
            "attempt/node numeric identities required")
    require(all(current.get(field) == bound[field] for field in CONTEXT_FIELDS)
            and isinstance(current.get("context"), dict)
            and current["context"].get("base_sha") == bound["base_sha"], "attempt identity changed")
    require(type(current.get("observation_version")) is int, "missing observation version")
    claim = next((c for c in claims if isinstance(c, dict) and c.get("claim_id") == bound["claim_id"]), None)
    require(claim is not None and type(claim.get("fence")) is int and claim.get("driver_id") == bound["owner_driver_id"]
            and claim.get("fence") == bound["claim_fence"]
            and claim.get("node_revision") == bound["node_revision"]
            and claim.get("status") == "live" and claim.get("lease_state") == "healthy",
            "node claim is not live and matching")
    status = current.get("status")
    require(status in ("running", "paused", "unknown", "candidate", "failed"), "unknown attempt status")
    if status in ("candidate", "failed"):
        require(current.get("lease_state") is None and current.get("terminal_observation_id"),
                "terminal attempt fields inconsistent")
    if status == "running" and current.get("lease_state") == "healthy":
        eligibility = "healthy"
    elif status == "failed" and current.get("terminal_observation_id"):
        eligibility = "failed"
    else:
        eligibility = "candidate" if status == "candidate" else "inactive"
    return dict(eligibility=eligibility, reason="attempt " + status,
                observation_version=current["observation_version"], current=current)


def binding_digest(record):
    return digest({k: record[k] for k in ("controller_driver_id", "source_sha", "purpose", "contexts")})


def owned_ticket(args, adapter):
    require(TICKET_ID.fullmatch(args.ticket), "invalid ticket id")
    path = ticket_path(args.ticket)
    if not path.exists():
        raise QueueRefusal("unknown ticket", 3)
    record, _ = receipt_input(str(path))
    require(isinstance(record, dict) and record.get("ticket") == args.ticket
            and record.get("schema_version") == 2, "not this bound ticket")
    require(record.get("binding_sha256") == binding_digest(record), "binding digest changed")
    require(adapter.provenance["me_actor"] == "driver:" + record["controller_driver_id"],
            "not this ticket's controller")
    require(args.expected_sha == record["source_sha"] == record["sha"]
            and FULL_SHA.fullmatch(args.expected_sha)
            and args.binding_sha256 == record["binding_sha256"], "Source/binding mismatch")
    return record


def check_cas(record, args):
    require(type(args.expected_revision) is int and args.expected_revision == record["ticket_revision"],
            "ticket revision changed; read status --json")


def request_entry(record, request_id, body):
    require(SAFE_ID.fullmatch(request_id), "invalid request id")
    entries = record.setdefault("requests", {})
    old = entries.get(request_id)
    if old:
        require(old["body_sha256"] == digest(body), "request id reused with changed bytes")
    else:
        require(len(entries) < 32, "ticket request limit reached")
    return old


def cmd_submit_owned(args):
    require(args.registrations and args.purpose and args.purpose.strip(),
            "ordinary submit requires --registrations and --purpose; explicit unowned work uses submit-unowned")
    require(len(args.purpose) <= 4000 and FULL_SHA.fullmatch(args.sha), "invalid purpose or Source SHA")
    driver = os.environ.get("AITELIER_DRIVER_ID", "")
    require(bool(driver), "own AITELIER_DRIVER_ID is required")
    adapter = _state_adapter(driver)
    registration, raw = receipt_input(args.registrations)
    require(isinstance(registration, dict) and isinstance(registration.get("source"), dict)
            and registration["source"].get("artifact") == args.sha, "registration Source mismatch")
    rows = registration.get("registrations")
    require(isinstance(rows, list) and 1 <= len(rows) <= 32, "require 1..32 registrations")
    contexts = []
    for row in rows:
        attempt, claim = row["attempt"], row["claim"]
        context = {key: attempt[key] for key in CONTEXT_FIELDS}
        context.update(base_sha=attempt["context"]["base_sha"], claim_id=claim["claim_id"],
                       claim_fence=claim["fence"])
        require(row["node"] == context["node_key"] and context["base_sha"] == args.sha
                and context["owner_driver_id"] == driver == claim["driver_id"],
                "registration node/Source/controller mismatch")
        require(type(context["owner_fence"]) is int and type(context["claim_fence"]) is int,
                "attempt and claim fences required")
        contexts.append(context)
    ids = [c["attempt_id"] for c in contexts]
    require(len(set(ids)) == len(ids), "duplicate registration")
    snapshot = adapter.snapshot(contexts)
    require(all(s["eligibility"] == "healthy" for s in snapshot), "registration authority is not current and healthy")
    worktree = Path(args.worktree).resolve()
    require(subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True,
                           text=True).stdout.strip() == args.sha, "worktree HEAD differs from Source")
    record = submitted_record(args)
    record.update(schema_version=2, ticket_revision=0, controller_driver_id=driver,
                  source_sha=args.sha, purpose=args.purpose, contexts=contexts,
                  active_context_ids=ids, excluded_context_ids=[], hold=None, admitted_at=None,
                  invocation_identity=None, decisions=[], failures=[], requests={}, history=[],
                  state_delivery_by_context={}, adapter_provenance=adapter.provenance)
    record["binding_sha256"] = binding_digest(record)
    with mutation_lock():
        record["registration_input"] = preserve(record, "inputs", raw)
        write_ticket(record)
    print(record["ticket"])
    return 0


def checked_snapshot(adapter, contexts, allow_terminal=False):
    snapshot = adapter.snapshot(contexts)
    allowed = ("healthy", "failed", "candidate") if allow_terminal else ("healthy",)
    require(all(s["eligibility"] in allowed for s in snapshot), "context authority unknown or changed")
    return snapshot


def validate_failure(record, decision, adapter):
    require(isinstance(decision, dict), "failure decision must be an object")
    for name in ("decision_id", "source_project_id", "source_node_key", "source_criterion_id",
                 "source_artifact_kind", "source_artifact_ref", "source_report_ref",
                 "source_report_sha256", "applicability_reason"):
        require(isinstance(decision.get(name), str) and decision[name].strip(), "missing failure " + name)
    require(SAFE_ID.fullmatch(decision["decision_id"]), "invalid failure decision id")
    require(decision.get("irreversible_for_attempt") is True
            and decision.get("controller_driver_id") == record["controller_driver_id"]
            and decision.get("source_sha") == record["source_sha"]
            and decision.get("ticket") == record["ticket"]
            and decision.get("binding_sha256") == record["binding_sha256"]
            and decision.get("original_purpose") == record["purpose"], "failure binding/scope mismatch")
    affected = decision.get("affected_context_ids")
    require(isinstance(affected, list) and affected and len(set(affected)) == len(affected)
            and set(affected) <= {c["attempt_id"] for c in record["contexts"]}, "invalid affected contexts")
    require(len(decision["applicability_reason"]) <= 4000, "failure reason too long")
    require(decision.get("disposition", "hold") in ("hold", "cancel"),
            "failure disposition must be hold or cancel; keep requires an explicit decide")
    report, raw = receipt_input(decision["source_report_ref"])
    require(hashlib.sha256(raw).hexdigest() == decision["source_report_sha256"], "source report hash changed")
    require(isinstance(report, dict) and report.get("status") == "completed"
            and report.get("settled") is True and report.get("usable") is True
            and report.get("verdict") == "fail" and report.get("criterion_id") == decision["source_criterion_id"]
            and report.get("artifact") == decision["source_artifact_ref"]
            and report.get("source_sha") == record["source_sha"], "not a completed governing failure report")
    source_id = decision.get("source_attempt_id_or_null")
    if source_id is not None:
        source = adapter.query("get_attempt", {"attempt_id": source_id})
        require(isinstance(source, dict) and source.get("attempt_id") == source_id
                and source.get("project_id") == decision["source_project_id"]
                and source.get("node_key") == decision["source_node_key"]
                and source.get("node_revision") == decision.get("source_node_revision")
                and source.get("context_hash") == decision.get("source_context_hash_or_null")
                and source.get("artifact_kind") == decision["source_artifact_kind"]
                and source.get("artifact_ref") == decision["source_artifact_ref"]
                and source.get("context", {}).get("base_sha") == record["source_sha"]
                and any(c.get("id") == decision["source_criterion_id"]
                        for c in source.get("context", {}).get("acceptance", [])), "source proof scope mismatch")
    else:
        require(type(decision.get("source_node_revision")) is int, "source revision required")
    evidence_id = decision.get("source_evidence_id_or_null")
    if evidence_id is not None:
        require(source_id is not None, "evidence needs a source attempt")
        evidence = adapter.query("evidence", {"attempt_id": source_id})
        require(isinstance(evidence, list), "evidence response must be a list")
        row = next((e for e in evidence if isinstance(e, dict) and e.get("evidence_id") == evidence_id), None)
        require(row is not None and row.get("attempt_id") == source_id and row.get("verdict") == "fail"
                and row.get("criterion_id") == decision["source_criterion_id"]
                and row.get("artifact_ref") == decision["source_artifact_ref"]
                and row.get("report_ref") == decision["source_report_ref"]
                and row.get("report_sha256") == decision["source_report_sha256"], "failure evidence mismatch")
    return raw


def apply_failure(record, decision, raw, report_raw):
    fingerprint = digest(decision)
    previous = next((d for d in record["failures"] if d["decision_id"] == decision["decision_id"]), None)
    if previous:
        require(previous["fingerprint"] == fingerprint, "failure decision id reused with changed bytes")
        return previous["outcome"]
    require(len(record["failures"]) < 32, "failure decision limit reached")
    excluded = set(record["excluded_context_ids"]) | set(decision["affected_context_ids"])
    record["excluded_context_ids"] = [c["attempt_id"] for c in record["contexts"] if c["attempt_id"] in excluded]
    record["active_context_ids"] = [i for i in record["active_context_ids"] if i not in excluded]
    if record.get("admitted_at"):
        outcome = "admitted-finish"
    elif record["state"] == "cancelled":
        outcome = "cancelled"
    else:
        outcome = "cancelled" if decision.get("disposition") == "cancel" else "held"
        record.update(state=outcome, hold="needs-disposition" if outcome == "held" else None)
    record["failures"].append(dict(decision_id=decision["decision_id"], fingerprint=fingerprint,
                                   affected_context_ids=decision["affected_context_ids"], outcome=outcome,
                                   input=preserve(record, "inputs", raw),
                                   source_report=preserve(record, "inputs", report_raw)))
    transition(record, "failure-disposition")
    return outcome


def physical_quiescence(record, context_id, filename):
    if not record.get("admitted_at") or record.get("spawn_failed_no_execution"):
        require(record["state"] in ("held", "cancelled", "failed"), "unstarted queued work can still launch")
        return dict(outcome="no-execution", ticket=record["ticket"], source_sha=record["source_sha"])
    if not filename or record["state"] == "running" or record.get("recovery_blocks_admission"):
        return None
    receipt, raw = receipt_input(filename)
    require(isinstance(receipt, dict) and receipt.get("ticket") == record["ticket"]
            and receipt.get("source_sha") == record["source_sha"]
            and receipt.get("invocation_identity") == record["invocation_identity"]
            and receipt.get("outcome") == "owned-quiescent"
            and context_id in receipt.get("report_scope", {}).get("attempt_ids", []), "physical receipt scope mismatch")
    refs = receipt.get("actual_ownership_refs_and_sha256")
    require(isinstance(refs, list) and refs, "physical ownership evidence required")
    for ref in refs:
        _, content = receipt_input(ref["path"])
        require(hashlib.sha256(content).hexdigest() == ref["sha256"], "ownership evidence hash mismatch")
    require(record.get("invocation_exit") is not None and not invocation_alive(record["invocation_identity"]),
            "physical invocation has no exact exit receipt")
    exit_receipt, exit_raw = receipt_input(record["invocation_exit"]["path"])
    require(hashlib.sha256(exit_raw).hexdigest() == record["invocation_exit"]["sha256"]
            and exit_receipt.get("invocation_identity") == record["invocation_identity"], "invocation exit mismatch")
    return dict(input=preserve(record, "inputs", raw), outcome="owned-quiescent")


def cmd_report_context(args):
    adapter = _state_adapter(os.environ.get("AITELIER_DRIVER_ID", ""))
    observation, observation_raw = receipt_input(args.observation)
    require(isinstance(observation, dict) and set(observation) <= OBSERVATION_FIELDS, "unsupported observation fields")
    require(observation.get("attempt_id") == args.context and SAFE_ID.fullmatch(observation.get("observation_id", "")),
            "observation context/id mismatch")
    require(observation.get("status") in ("running", "paused", "unknown", "candidate", "failed"), "invalid observation status")
    require(observation.get("status") != "failed" or args.failure_decision, "failed observation requires --failure-decision")
    if observation.get("status") == "failed":
        require("artifact" not in observation and "artifact_kind" not in observation, "failed observations omit artifact fields")
    if observation["status"] in ("candidate", "failed"):
        require(observation.get("quiescent") is True, "terminal observation must declare truthful scoped quiescence")
        _, report_raw = receipt_input(observation["report_ref"])
        require(hashlib.sha256(report_raw).hexdigest() == observation.get("report_sha256"), "observation report hash mismatch")
    decision, decision_raw = receipt_input(args.failure_decision) if args.failure_decision else (None, None)
    physical, physical_raw = receipt_input(args.physical_receipt) if args.physical_receipt else (None, None)
    body = dict(observation_sha256=hashlib.sha256(observation_raw).hexdigest(), context=args.context,
                failure_sha256=hashlib.sha256(decision_raw).hexdigest() if decision_raw else None,
                physical_sha256=hashlib.sha256(physical_raw).hexdigest() if physical_raw else None)
    with client_mutation():
        record = owned_ticket(args, adapter)
        old = request_entry(record, args.request_id, body)
        prior_failure = None
        if decision:
            require(isinstance(decision, dict), "failure decision must be an object")
            prior_failure = next((f for f in record["failures"] if f["decision_id"] == decision.get("decision_id")), None)
            if prior_failure:
                require(prior_failure["fingerprint"] == digest(decision), "failure decision id reused with changed bytes")
        if old and old["state_delivery"] in ("acknowledged", "deferred-live"):
            print(json.dumps(old))
            return old["rc"]
        if not old:
            check_cas(record, args)
        context = next((c for c in record["contexts"] if c["attempt_id"] == args.context), None)
        require(context is not None, "context is not bound to ticket")
        require(observation.get("context_hash") == context["context_hash"]
                and observation.get("fence") == context["owner_fence"], "observation context/fence mismatch")
        snapshot = checked_snapshot(adapter, [context], allow_terminal=True)[0]
        if old:
            # A deliberate exact-body replay resolves an unknown ACK, never invents a new observation.
            current = adapter.query("get_attempt", {"attempt_id": args.context})
            require(isinstance(current, dict) and current.get("attempt_id") == args.context, "delivery read-back malformed")
            if current.get("terminal_observation_id") != observation["observation_id"]:
                require(current.get("observation_version") == observation.get("expected_version"), "observation version changed")
        else:
            require(snapshot["observation_version"] == observation.get("expected_version"), "observation version changed")
            require(not any(d.get("state_delivery") in ("unknown", "sending", "refused")
                            and d.get("request", {}).get("observation_id") != observation["observation_id"]
                            for d in record["requests"].values() if d.get("context") == args.context),
                    "resolve/replay the earlier unknown observation first")
        if decision:
            if prior_failure:
                outcome = prior_failure["outcome"]
            else:
                report_raw = validate_failure(record, decision, adapter)
                require(args.context in decision["affected_context_ids"], "failure must govern this context")
                outcome = apply_failure(record, decision, decision_raw, report_raw)
        else:
            outcome = "admitted-finish" if record.get("admitted_at") else record["state"]
        terminal = observation["status"] in ("candidate", "failed")
        if terminal and observation["status"] == "candidate":
            require(args.context in record["active_context_ids"], "excluded context cannot receive candidate gate evidence")
        quiescence = physical_quiescence(record, args.context, args.physical_receipt) if terminal else {}
        delivery = "deferred-live" if terminal and quiescence is None else "sending"
        rc = 6 if delivery == "deferred-live" else 0
        entry = dict(body_sha256=digest(body), context=args.context, request=observation,
                     request_input=preserve(record, "inputs", observation_raw), state_delivery=delivery,
                     outcome=outcome, rc=rc, quiescence=quiescence, adapter_provenance=adapter.provenance)
        record["requests"][args.request_id] = entry
        record["state_delivery_by_context"][args.context] = dict(request_id=args.request_id, state_delivery=delivery)
        transition(record, "report-prepared")
    if delivery == "deferred-live":
        print(json.dumps(entry))
        return 6
    # Local durable disposition and intent precede the first State write, outside the short lock.
    response_raw = None
    try:
        response = adapter.client._client.post("/api/state/commands/report_external_attempt", json=observation)
        require(len(response.content) <= MAX_BYTES, "State response exceeds 1 MiB")
        response_raw = response.content
        if response.status_code >= 400:
            result = dict(state_delivery="refused", http_status=response.status_code, rc=7)
        else:
            value = response.json()
            require(isinstance(value, dict) and "result" not in value, "malformed report response")
            result = dict(state_delivery="acknowledged", response=value, rc=0)
    except Exception as exc:
        result = dict(state_delivery="unknown", error=type(exc).__name__, rc=7)
    with mutation_lock():
        current = read_ticket(args.ticket)
        if response_raw is not None:
            result["response_input"] = preserve(current, "responses", response_raw)
        current["requests"][args.request_id].update(result)
        current["state_delivery_by_context"][args.context] = dict(request_id=args.request_id,
                                                               state_delivery=result["state_delivery"])
        transition(current, "report-delivery")
        entry = current["requests"][args.request_id]
    print(json.dumps(entry))
    return result["rc"]


def cmd_decide(args):
    adapter = _state_adapter(os.environ.get("AITELIER_DRIVER_ID", ""))
    require(args.reason.strip() and len(args.reason) <= 4000, "reason required, at most 4000 characters")
    retain, _ = receipt_input(args.retain_contexts) if args.retain_contexts else ([], None)
    ack, _ = receipt_input(args.ack_failures) if args.ack_failures else ([], None)
    body = dict(action=args.action, reason=args.reason, reuse_purpose=args.reuse_purpose,
                retain_contexts=retain, ack_failures=ack, baseline_authority=args.baseline_authority,
                expected_sha=args.expected_sha, binding_sha256=args.binding_sha256)
    with client_mutation():
        record = owned_ticket(args, adapter)
        previous = request_entry(record, args.request_id, body)
        if previous:
            print(json.dumps(previous))
            return previous["rc"]
        check_cas(record, args)
        require(record["state"] != "cancelled", "cancelled tickets are final; submit a deliberate new ticket")
        require(len(record["decisions"]) < 32, "decision limit reached")
        if args.action == "cancel":
            if record.get("admitted_at"):
                record["excluded_context_ids"] = [c["attempt_id"] for c in record["contexts"]]
                record["active_context_ids"] = []
                outcome, rc = "admitted-finish", 6
            else:
                record.update(state="cancelled", hold=None)
                outcome, rc = "cancelled", 0
        else:
            require(not record.get("admitted_at"), "admission is permanent; existing invocation must finish")
            require(isinstance(retain, list) and len(set(retain)) == len(retain)
                    and isinstance(ack, list), "keep lists must be arrays")
            failures = [f["fingerprint"] for f in record["failures"]]
            if args.action == "recheck":
                require(not failures and not record.get("keep"), "failure/keep needs a new explicit keep decision")
                retain = record["active_context_ids"]
                contexts = [c for c in record["contexts"] if c["attempt_id"] in retain]
                snapshot = checked_snapshot(adapter, contexts)
            else:
                require(sorted(ack) == sorted(failures), "acknowledge the exact failure fingerprints")
                if args.reuse_purpose == "continue-contexts":
                    require(retain and set(retain) <= set(record["active_context_ids"]), "retain only useful original healthy contexts")
                    contexts = [c for c in record["contexts"] if c["attempt_id"] in retain]
                    snapshot = checked_snapshot(adapter, contexts)
                else:
                    require(args.reuse_purpose and args.reuse_purpose.startswith("baseline-evidence:")
                            and args.reuse_purpose.removeprefix("baseline-evidence:").strip() and not retain,
                            "baseline requires a reason and zero acceptance contexts")
                    authority = next((c for c in record["contexts"] if c["attempt_id"] == args.baseline_authority), None)
                    require(authority and any(authority["attempt_id"] in f["affected_context_ids"] for f in record["failures"]),
                            "baseline needs an exact failed authority context")
                    snapshot = checked_snapshot(adapter, [authority], allow_terminal=True)
                    require(snapshot[0]["eligibility"] in ("healthy", "failed", "candidate"), "baseline authority unavailable")
                record["keep"] = dict(purpose=args.reuse_purpose, acknowledged_failures=ack,
                                      retain_context_ids=retain, baseline_authority=args.baseline_authority,
                                      binding_sha256=record["binding_sha256"],
                                      original_purpose=record["purpose"], source_sha=record["source_sha"])
            record["active_context_ids"] = retain
            record["excluded_context_ids"] = [c["attempt_id"] for c in record["contexts"] if c["attempt_id"] not in retain]
            record.update(state="queued", hold=None, authority_snapshot=snapshot)
            outcome, rc = "queued", 0
        entry = dict(body_sha256=digest(body), outcome=outcome, rc=rc, at=now(), request_id=args.request_id)
        record["decisions"].append(dict(entry, **body))
        record["requests"][args.request_id] = entry
        transition(record, "decide-" + args.action)
    print(json.dumps(entry))
    return rc


def admission_snapshot(record, adapter):
    require(adapter.provenance["me_actor"] == "driver:" + record["controller_driver_id"],
            "daemon identity does not match this physical controller")
    require(record["binding_sha256"] == binding_digest(record) and record["source_sha"] == record["sha"],
            "immutable ticket binding changed")
    keep = record.get("keep")
    if keep:
        require(keep["binding_sha256"] == record["binding_sha256"] and keep["source_sha"] == record["source_sha"]
                and keep["original_purpose"] == record["purpose"]
                and sorted(keep["acknowledged_failures"]) == sorted(f["fingerprint"] for f in record["failures"]),
                "keep no longer acknowledges exact binding/failures")
    if keep and keep["purpose"].startswith("baseline-evidence:"):
        ids = [keep["baseline_authority"]]
        allowed = ("healthy", "failed", "candidate")
    else:
        ids = record["active_context_ids"]
        allowed = ("healthy",)
        require(ids, "no eligible acceptance contexts")
    contexts = [c for c in record["contexts"] if c["attempt_id"] in ids]
    snapshot = adapter.snapshot(contexts)
    record["authority_snapshot"] = snapshot
    # An authoritative closed attempt invalidates admission without inventing a
    # product criterion failure or a terminal-report endpoint.
    for item in snapshot:
        if item["eligibility"] == "failed" and not any(
                item["context_id"] in f["affected_context_ids"] for f in record["failures"]):
            current = item["current"]
            fact = dict(context_id=item["context_id"], context_hash=current["context_hash"],
                        owner_fence=current["owner_fence"], status="failed",
                        terminal_observation_id=current["terminal_observation_id"])
            fingerprint = digest(fact)
            require(len(record["failures"]) < 32, "failure decision limit reached")
            record["failures"].append(dict(decision_id="state-" + fingerprint, fingerprint=fingerprint,
                                           affected_context_ids=[item["context_id"]],
                                           outcome="held", authoritative_State_failure=fact))
    if keep:
        require(sorted(keep["acknowledged_failures"]) == sorted(f["fingerprint"] for f in record["failures"]),
                "new governing failure needs disposition")
    require(snapshot and all(s["eligibility"] in allowed for s in snapshot),
            "owner_unknown" if any(s["eligibility"] == "unknown" for s in snapshot) else "needs-disposition")
    return snapshot


def process_birth(pid):
    try:
        fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (OSError, IndexError):
        return None


def invocation_alive(identity):
    return bool(identity and identity.get("birth") and process_birth(identity["pid"]) == identity["birth"])


def invocation_exit(record, rc):
    value = dict(invocation_identity=record["invocation_identity"], runner_rc=rc, exited_at=now())
    record["invocation_exit"] = preserve(record, "exits", canonical(value))


def reconcile_owned(record):
    identity = record.get("invocation_identity")
    if invocation_alive(identity):
        log("adopting exact owned invocation %s" % record["ticket"])
        while invocation_alive(identity):
            time.sleep(POLL_SECONDS)
        with mutation_lock():
            current = read_ticket(record["ticket"])
            exitf = report_parent(record["ticket"]) / (SUFFIX + ".gate.exit")
            if exitf.exists():
                invocation_exit(current, None)
                _finalize(current)
            else:
                current.update(state="failed", error="exact owned invocation exited without verdict", finished_at=now())
                transition(current, "recovery-no-verdict")
        return
    with mutation_lock():
        current = read_ticket(record["ticket"])
        if identity and identity.get("birth") and current.get("invocation_exit"):
            value, raw = receipt_input(current["invocation_exit"]["path"])
            if (hashlib.sha256(raw).hexdigest() == current["invocation_exit"]["sha256"]
                    and value.get("invocation_identity") == identity):
                _finalize(current)
                return
        current.update(state="held", hold="recovery_unknown", recovery_blocks_admission=True)
        transition(current, "recovery-unknown")


# --- daemon -----------------------------------------------------------------

def daemon_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def daemon_alive() -> bool:
    return daemon_pid() is not None


def container_alive(ticket: str) -> bool:
    """Is a throwaway gate container still running? (the queue serializes, so
    at most one exists, and it belongs to the one running ticket.)"""
    name = "wuxia-gate-%s-" % SUFFIX
    out = subprocess.run(["docker", "ps", "--filter", "name=" + name,
                          "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def finalize(record: dict, runner_rc: int | None = None) -> dict:
    """Finish only a fresh running record; stale selections cannot restore a cancel."""
    with mutation_lock():
        current = read_ticket(record["ticket"])
        if current["state"] != "running":
            return current
        if record.get("error"):
            current["error"] = record["error"]
        return _finalize(current, runner_rc)


def _finalize(record: dict, runner_rc: int | None = None) -> dict:
    """Read the exit FILE and flatten while the caller holds mutation.lock."""
    parent = report_parent(record["ticket"])
    exitf = parent / ("%s.gate.exit" % SUFFIX)
    raw = exitf.read_text().strip() if exitf.exists() else ""
    inner = sorted(parent.glob("wuxia-godot-gate-*"))
    if len(inner) > 1:
        # A test inside the gated tree can leave its own wuxia-godot-gate-* dirs
        # here (tests/test_repeatability_gate.py did, 2026-09-23..25: 5 green gates
        # discarded). The gate names its own report dir on its log's first line.
        logf = parent / ("%s.gate.log" % SUFFIX)
        first = logf.read_text(errors="replace").splitlines()[:1] if logf.exists() else []
        named = first[0].split("gate reports:", 1)[1].strip() if first and "gate reports:" in first[0] else ""
        inner = [d for d in inner if str(d) == named]
        if inner:
            record["stray_report_dirs"] = sorted(str(d) for d in parent.glob("wuxia-godot-gate-*") if str(d) != named)
    if len(inner) == 1:
        for item in inner[0].iterdir():  # promote the report to <ticket>/
            shutil.move(str(item), str(parent / item.name))
        inner[0].rmdir()
    manifest = parent / "manifest.json"
    if raw in ("0", "1") and manifest.exists():
        record.update(state="done", gate_exit_code=int(raw),
                      verdict={"0": "passed", "1": "failed"}[raw],
                      report_dir=str(parent))
    else:
        record.update(state="failed", gate_exit_code=raw or runner_rc,
                      error=record.get("error") or
                      ("no verdict: exit=%r manifest=%s"
                       % (raw, manifest.exists())))
    record["finished_at"] = now()
    if record.get("schema_version") == 2:
        transition(record, "finalized")
    else:
        write_ticket(record)
    return record


def run_one(record: dict, adapter=None) -> bool:
    with mutation_lock():
        # Selection may predate a cancellation or failure. Always re-read here.
        record = read_ticket(record["ticket"])
        if record["state"] != "queued":
            return False
        owned = record.get("schema_version") == 2
        if owned:
            require(adapter is not None, "owned admission requires the recipe adapter")
            try:
                record["authority_snapshot"] = admission_snapshot(record, adapter)
            except QueueRefusal as exc:
                record.update(state="held", hold=str(exc), admission_provenance=adapter.provenance)
                transition(record, "admission-held")
                return False
        head = subprocess.run(["git", "-C", record["worktree"], "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head != record["sha"]:
            record.update(state="failed", finished_at=now(),
                          error="HEAD is %s, ticket asked for %s — refusing to gate a tree that is not the submitted commit"
                          % (head or "<unreadable>", record["sha"]))
            if owned:
                transition(record, "source-refused-no-execution")
            else:
                write_ticket(record)
            return not owned
        parent = report_parent(record["ticket"])
        parent.mkdir(parents=True, exist_ok=True)
        record.update(state="running", started_at=now(), error=None)
        launch_env = dict(_LAUNCH_ENV)
        if owned:
            record.update(admitted_at=now(), admitted_context_ids=list(record["active_context_ids"]),
                          admitted_binding_sha256=record["binding_sha256"],
                          admission_provenance=adapter.provenance,
                          invocation_identity=dict(nonce=uuid.uuid4().hex, pid=None, birth=None))
            launch_env.update(GATE_QUEUE_TICKET=record["ticket"],
                              GATE_QUEUE_INVOCATION_NONCE=record["invocation_identity"]["nonce"])
            transition(record, "admitted-before-spawn")
        else:
            write_ticket(record)
        try:
            proc = subprocess.Popen([str(GATE_RUN), record["worktree"], str(parent), SUFFIX],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, env=launch_env)
        except OSError as exc:
            record.update(state="failed", finished_at=now(), error="launcher did not start: " + type(exc).__name__)
            if owned:
                record["spawn_failed_no_execution"] = True
                transition(record, "spawn-failed-no-execution")
            else:
                write_ticket(record)
            return True
        if owned:
            record["invocation_identity"].update(pid=proc.pid, birth=process_birth(proc.pid))
            transition(record, "spawn-identity")
    log("running %s %s @%s" % (record["ticket"], record["worktree"], record["sha"][:12]))
    _, stderr = proc.communicate()
    # Never copy a stale admission snapshot over concurrent failure exclusions.
    with mutation_lock():
        current = read_ticket(record["ticket"])
        if current["state"] == "running":
            if proc.returncode in (64, 65, 66) or stderr.strip():
                current["error"] = "gate_run.sh rc=%d %s" % (proc.returncode, stderr.strip()[:400])
            if owned:
                invocation_exit(current, proc.returncode)
            record = _finalize(current, proc.returncode)
    log("finished %s state=%s exit=%s" % (record["ticket"], record["state"], record["gate_exit_code"]))
    return True


def reconcile() -> None:
    """A daemon restart must not lose work. Adopt or requeue in-flight tickets."""
    with mutation_lock():
        records = all_tickets()
    for selected in records:
        with mutation_lock():
            record = read_ticket(selected["ticket"])
            if record["state"] != "running":
                continue
        if record.get("schema_version") == 2:
            reconcile_owned(record)
            continue
        exitf = report_parent(record["ticket"]) / ("%s.gate.exit" % SUFFIX)
        if exitf.exists():
            log("adopting finished %s" % record["ticket"])
            finalize(record)
        elif container_alive(record["ticket"]):
            log("adopting in-flight %s — waiting for its exit file"
                % record["ticket"])
            while container_alive(record["ticket"]) and not exitf.exists():
                time.sleep(POLL_SECONDS)
            finalize(record)
        else:
            log("requeueing %s — nothing is running it" % record["ticket"])
            with mutation_lock():
                current = read_ticket(record["ticket"])
                if current["state"] == "running":
                    current.update(state="queued", started_at=None)
                    write_ticket(current)


def cmd_daemon(args) -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    TICKETS.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another gate-queue daemon holds %s (pid %s)"
              % (LOCK, daemon_pid()), file=sys.stderr)
        return 4
    STOPFILE.unlink(missing_ok=True)
    PIDFILE.write_text("%d\n" % os.getpid())
    stopping = {"now": False}

    def on_signal(signum, frame):
        stopping["now"] = True
        log("signal %d — will stop after the current gate" % signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("daemon up pid=%d queue=%s reports=%s" % (os.getpid(), ROOT, REPORTS))
    try:
        reconcile()
        adapter = None
        while not stopping["now"] and not STOPFILE.exists():
            with mutation_lock():
                records = all_tickets()
                if any(r.get("recovery_blocks_admission") for r in records):
                    log("recovery_unknown: possible live consumer; admission blocked")
                    return 7
                pending = [r for r in records if r["state"] == "queued"]
            if not pending:
                if args.once:
                    break
                time.sleep(POLL_SECONDS)
                continue
            selected = pending[0]  # all_tickets() sorts by submitted_at
            if selected.get("schema_version") == 2:
                if adapter is None:
                    # Configuration refusal leaves selected ticket byte-identical queued.
                    adapter = _state_adapter(selected["controller_driver_id"])
                require(adapter.provenance["me_actor"] == "driver:" + selected["controller_driver_id"],
                        "adapter-unavailable: daemon identity differs from selected controller")
            admitted = run_one(selected, adapter)
            if args.once and admitted:
                break
    except QueueRefusal as exc:
        log(str(exc))
        return exc.rc
    finally:
        log("daemon down pid=%d" % os.getpid())
        PIDFILE.unlink(missing_ok=True)
        STOPFILE.unlink(missing_ok=True)
    return 0


def cmd_stop(args) -> int:
    """Stop via the daemon's own control path — never pkill."""
    ROOT.mkdir(parents=True, exist_ok=True)
    STOPFILE.write_text(now() + "\n")
    pid = daemon_pid()
    if pid is None:
        print("no daemon running; stop flag left for the next one")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("asked daemon pid %d to stop after the current gate" % pid)
    return 0


def cmd_daemon_status(args) -> int:
    pid = daemon_pid()
    print("daemon: %s" % ("running pid %d" % pid if pid else "not running"))
    counts: dict[str, int] = {}
    for r in all_tickets():
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("tickets: %s" % (counts or "none"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    for command, function in (("submit", cmd_submit_owned), ("submit-unowned", cmd_submit_unowned)):
        p = sub.add_parser(command, help="queue one gate; ordinary submit requires State registrations")
        p.add_argument("worktree")
        p.add_argument("sha")
        p.add_argument("--label", default="")
        if command == "submit":
            p.add_argument("--registrations")
            p.add_argument("--purpose")
        else:
            p.add_argument("--unowned-reason", required=True)
        p.set_defaults(func=function)

    for command, function in (("decide", cmd_decide), ("report-context", cmd_report_context)):
        p = sub.add_parser(command)
        p.add_argument("ticket")
        p.add_argument("--expected-sha", required=True)
        p.add_argument("--binding-sha256", required=True)
        p.add_argument("--expected-revision", required=True, type=int)
        p.add_argument("--request-id", required=True)
        if command == "decide":
            p.add_argument("--action", required=True, choices=("cancel", "keep-reuse", "recheck"))
            p.add_argument("--reason", required=True)
            p.add_argument("--reuse-purpose")
            p.add_argument("--retain-contexts")
            p.add_argument("--ack-failures")
            p.add_argument("--baseline-authority")
        else:
            p.add_argument("--context", required=True)
            p.add_argument("--observation", required=True)
            p.add_argument("--failure-decision")
            p.add_argument("--physical-receipt")
        p.set_defaults(func=function)

    p = sub.add_parser("status", help="queued / running / done / failed")
    p.add_argument("ticket")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("cancel", help="withdraw one queued, never-started ticket "
                                      "(takes the short mutation.lock; never stops running work)")
    p.add_argument("ticket")
    p.add_argument("--expected-sha", required=True,
                   help="the full commit sha the ticket was submitted for")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="",
                   help="who is cancelling (free text; default: the OS user, always "
                        "recorded as os_user too)")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("daemon", help="drain the queue, one engine run at a time")
    p.add_argument("--once", action="store_true",
                   help="run at most one ticket, then exit")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("stop", help="ask the daemon to stop after the current gate")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("daemon-status")
    p.set_defaults(func=cmd_daemon_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except QueueRefusal as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return exc.rc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # No exception payload/HTTP request (which could carry credentials) is printed.
        print("refused: malformed or unreadable input (%s)" % type(exc).__name__, file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
