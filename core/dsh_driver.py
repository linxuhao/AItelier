"""One owned DSH process per State project; State remains the task authority."""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
import signal
import sys
import time
import uuid

from core.datadir import owner_secrets_dir
from core.driver_credentials import read_token_file
from core.dsh_todo_hooks import CLI_ENV as TODO_CLI_ENV, TODO_ROOT
from core.off_loop_sqlite import retry_while_locked
from core.state_graph import STATE_WRITE_BUSY_CODE, STATE_WRITE_BUSY_RETRY_AFTER, StateWriteBusy, now
from core.state_schema_migration import add_column_if_missing

SCHEMA = """
CREATE TABLE IF NOT EXISTS dsh_drivers (
    project_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'stopped',
    provider TEXT NOT NULL, model TEXT NOT NULL,
    session_id TEXT NOT NULL,
    subagents_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Which models actually answered each driver session. The completion endpoint
-- chose the endpoint (gateway.active_model) and then kept nothing: the child's
-- own session file only ever recorded the route alias, so a review criterion
-- keyed on the writer's model family had no measured value to cite.
--
-- `served_by` is the LAST endpoint this session could NAME - not "the endpoint
-- of the last turn". Those are different facts the moment a turn cannot name
-- one (the gateway answered but could not attribute the answer, or handed back
-- the route argument it was given): such a turn leaves the known endpoint in
-- place, because one unattributable turn is not evidence that the earlier
-- attribution was wrong, and overwriting it would erase the only measured
-- identity the session ever had. NULL means this session has named NO endpoint
-- yet - never a placeholder, and never `route` (an alias in this column would
-- read as if `worker` had answered).
--
-- `served_by_seen` is every endpoint the session named, as JSON text: first-seen
-- order, no duplicates, capped at SERVED_BY_SEEN_LIMIT distinct values (a
-- session that exhausted the cap keeps the FIRST ones and `served_by` still
-- names the latest). One column would answer "who spoke last" for a session
-- that failed over mid-life and so mis-report which model family wrote it; the
-- list makes that a property of the session rather than of its final turn.
--
-- One row per (project, session): a later call moves `last_seen` and `calls`,
-- `first_seen` stays.
CREATE TABLE IF NOT EXISTS dsh_driver_session_models (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    route TEXT NOT NULL,
    served_by TEXT,
    served_by_seen TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, session_id)
);
"""

# How many distinct endpoints one session's `served_by_seen` keeps. Small on
# purpose: the question it answers is "which model families wrote this", and a
# session that named more than a handful of endpoints is a routing story, not a
# list anybody reads. The FIRST endpoints are kept (that is the order the column
# promises) even after the cap; `served_by` still names the latest one.
SERVED_BY_SEEN_LIMIT = 8

# How long the driver's own stamp write waits for a busy database, counted in
# ATTEMPTS rather than in one longer wait. Each attempt opens a fresh connection
# and so spends that connection's full busy timeout again (5 s on
# `sqlite3.connect`'s default, which the host database here uses), which makes
# the patience ~15.4 s across the three; a single attempt loses the stamp to any
# write lock held longer than the timeout.
#
# THREE, not two, and measured rather than argued: releasing a write lock in WAL
# mode is not instantaneous for the connection that wins next. Against a 7.0 s
# lock the stamp returned in 7.05 s and 7.07 s on two runs and 9.68 s on a third,
# because the holder's own commit landed 1.3-2.6 s after it stopped holding (one
# probe: lock acquired at 0.00 s, holder's sleep over at 7.00 s, commit visible
# at 8.29 s). Two attempts budget 10.2 s, i.e. 0.5 s of margin in the worst
# measured case - a coin flip; three budget 15.4 s and keep ~5.7 s.
#
# The pause between attempts is short on purpose - it exists so the retry is not
# a spin, not as a second budget.
#
# Because the patience is this long, WHOEVER WRITES MUST NOT WAIT FOR IT ON THE
# EVENT LOOP. `update` is synchronous sqlite by design; the coroutines below
# therefore call it as `await asyncio.to_thread(self.update, ...)`. Called
# directly, one held lock froze the whole driver runtime for the full budget
# (~15.4 s) - every request, every monitor, every other project's lifecycle in
# the same process. Off the loop the lock costs ONE thread, and `update`'s
# contract is unchanged: same single statement, same retries, and the original
# error still re-raised after the bound.
STAMP_LOCK_ATTEMPTS = 3
STAMP_LOCK_PAUSE = 0.2
# How long ONE attempt waits for the lock, in milliseconds. It is the value
# `sqlite3.connect` already applies when no timeout is passed (`DBManager._connect`
# passes none), named here for two reasons: the stamp's patience is then stated
# once instead of inherited from a stdlib default, and a witness can shrink it to
# observe a refusal inside a test rather than by really waiting the ~15.4 s
# budget out. It is a PRAGMA on the attempt's own connection, so it never
# outlives the attempt.
STAMP_BUSY_TIMEOUT_MS = 5000


class DriverStampBusy(StateWriteBusy, sqlite3.OperationalError):
    """A driver stamp whose write lock outlived the whole stamp budget.

    The refused stamp is RETRYABLE and it wrote NOTHING - the failed statement is
    one `UPDATE`, so nothing was half-applied - but it is still, deliberately,
    a `sqlite3.OperationalError`: every caller that already treats a locked
    database as an operational failure (the log line, the test that pins the
    bounded give-up) keeps seeing exactly that, with SQLite's own wording in
    the message.

    What is NEW is the second half: it is also a `StateWriteBusy`, the
    repository's typed busy refusal, so a transport answers it as the retryable
    thing it is. Without that, `POST /api/dsh-drivers/<project>/activity` let
    `sqlite3.OperationalError('database is locked')` escape as an untyped HTTP
    500 (`iss-89b4c2a1e8c048b3`) while the SAME busy condition was answered 503 +
    `Retry-After` on the State command routes: one condition, two answers, and
    the caller of the stamp had no way to tell "retry in a second" from "the
    request is broken".
    """


def _is_lock_error(error):
    """True only for the two messages SQLite uses to mean "somebody else is writing".

    Anything else that arrives as `OperationalError` (a missing column, a
    malformed statement) must not be retried: it would not become true on a
    second look, and retrying it would only hide the real error.
    """
    return isinstance(error, sqlite3.OperationalError) and any(
        text in str(error) for text in ("database is locked", "database table is locked"))


# DSH built-in tool plugins the driver profile switches off (see the patch list in
# _provision): measured unused, or shadowing the runtime's own tools.
UNUSED_BUILTIN_TOOL_PLUGINS = ("tool-subagent-fork", "tool-workflow", "plan-mode", "tool-todo")
# Live native children per director (DSH's maxActiveSubagents; its default is 8).
# Written into the profile for DSH and handed to the runtime as
# AITELIER_DSH_MAX_ACTIVE_SUBAGENTS, so dsh-director's capacity guard (plugin step 9)
# and DSH's own admission limit are the same number by construction.
MAX_ACTIVE_SUBAGENTS = 8
# The provider the runtime registers for its completion bridge (model.mjs); native
# subagents ask it for the worker route.
SUBAGENT_PROVIDER = "aitelier"
# The actors that start a driver on the RUNTIME's own initiative rather than on a
# user's: boot recovery (`resume`) and the host supervisor's retry arm
# (core/dsh_host.py). Both act on a row they read earlier, so neither may turn a
# row the owner disabled in the meantime back on - see `DshDrivers.start`. A
# request actor is derived from an identity (`owner:...`, `driver:...`), so no
# user request can name one of these.
RECOVERY_ACTORS = frozenset({"dsh-host-recovery", "dsh-runtime-recovery"})

# Optional local MCP services a director can be given. NONE is registered by
# default: each is registered only when the host service's environment names
# its URL in AITELIER_DSH_MCP_<NAME> (e.g. AITELIER_DSH_MCP_AGENTMCP). A built-in
# port would aim every install at whatever else happens to listen there; the
# ports one deployment uses are an operator's systemd drop-in (docs/dsh-driver.md).
DSH_EXTRA_MCP_SERVERS = (
    # These four are the DIRECTOR's capabilities. The steward role gets none of
    # them (see `_environment`): its role presents no tool of any of these
    # services, so the entries bought it nothing - while the pwmcp one aimed a
    # steward start at the SHARED browser and evicted a director mid-playtest.
    # devmcp: files, bash, subagent jobs; zvec: semantic code search.
    "devmcp",
    "zvec",
    # Media (asr/ocr/pdf/web + media + video). Definitions stay behind
    # caller-scoped search_tools/call_tool for the driver and native children.
    "agentmcp",
    # The persistent Playwright (autoshot) browser. Registering it is what makes
    # a UI claim checkable at all: without it every dashboard delivery can only
    # say "the strings are in the bundle and the component tests pass", which
    # cannot see a layout that renders nothing (measured 2026-11-13: the service
    # answered initialize on its port while search_tools found 0 browser tools).
    # The proxy in front of the browser answers MCP itself, so the URL is the
    # proxy, not the Playwright CLI's own port behind it. The per-driver port
    # mapping (below) rewrites this URL's port, so it needs a base URL too.
    "pwmcp",
)

# One Playwright MCP instance holds ONE client session: two drivers pointed at
# the same URL do not share a browser, the second `initialize` evicts the first,
# and the evicted driver's next call answers `-32001 Session not found` while
# the service log stays silent (measured 2026-11-13). The instances already run
# one per driver (`~/.AItelier/browser/run-pwmcp.sh <driver> <port>`); what was
# missing is which port belongs to whom. This is that mapping, and it is the
# only server with a per-driver override:
#
#     AITELIER_DSH_MCP_PWMCP_DRIVER_PORTS="claude=8933,grok=8932"
#
# Unset or empty means "no mapping" - every driver keeps the shared default
# above, exactly as before. A local service is not a prerequisite, so this knob
# never changes failOnStartupError.
PWMCP_DRIVER_PORTS_ENV = "AITELIER_DSH_MCP_PWMCP_DRIVER_PORTS"


def pwmcp_driver_ports(value=None):
    """Parse `<driver_id>=<port>` pairs into `{driver_id: port}`.

    Empty entries (a trailing comma, blank segment) are ignored, since "no
    mapping" and "mapping with nothing in it" are the same statement. Whitespace
    around an entry is allowed.

    A malformed entry RAISES. Falling back to the shared default would restore
    precisely the failure this mapping exists to prevent - two drivers taking
    turns evicting each other - and it would leave no symptom to trace it back
    from: the evicted driver only ever sees `-32001 Session not found`.
    """
    raw = os.environ.get(PWMCP_DRIVER_PORTS_ENV, "") if value is None else value
    ports = {}
    for segment in raw.split(","):
        entry = segment.strip()
        if not entry:
            continue
        driver_id, separator, port_text = entry.partition("=")
        driver_id, port_text = driver_id.strip(), port_text.strip()
        if not separator or not driver_id or not port_text.isdigit():
            raise ValueError(
                f"{PWMCP_DRIVER_PORTS_ENV} entry {segment!r} is not a "
                "<driver_id>=<port> pair")
        port = int(port_text)
        if not 1024 <= port <= 65535:
            raise ValueError(
                f"{PWMCP_DRIVER_PORTS_ENV} entry {segment!r} has port {port}, "
                "outside 1024..65535")
        ports[driver_id] = port
    return ports


# The director/worker/steward role table. integrations/dsh-driver/roles.mjs parses
# the same file with the same rules (a test compares both parses and both
# refusals); unknown keys are refused at every level. `steward` is optional: a
# table without it is the director/worker table it always was.
ROLES_FILE = Path(__file__).resolve().parents[1] / "integrations" / "dsh-director" / "roles.json"
ROLE_NAMES = ("director", "worker", "steward")
_REQUIRED_ROLES = ("director", "worker")
_ROLE_KEYS = ("route", "reasoning_effort", "compaction", "prompt", "presented", "forbidden")
# Optional in every role: true refuses any tool the role does not present.
# `catalog: "host"` (step 24.1a): the role finds and calls its further tools only in the
# catalog its host gives for the turn (the steward's own connections).
# `child_route` (step 24.2c): the route the root's child sessions take instead of the
# worker route. `image_retain` (step 24.2c): how many already-seen images the root session
# keeps in its history (count and base64 bytes, each with its offload quantum).
_OPTIONAL_ROLE_KEYS = ("presented_only", "catalog", "child_route", "image_retain")
_CATALOGS = ("host",)
_IMAGE_RETAIN_KEYS = ("max_images", "count_quantum", "max_base64_bytes", "byte_quantum")
_COMPACTION_KEYS = ("thresholdRatio", "retainTokens")
_EFFORTS = ("off", "low", "high", "max")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_]+")
_PROMPT_NAME = re.compile(r"[A-Za-z0-9_.-]+\.md")


def _exact_keys(value, keys, where, required=None):
    if not isinstance(value, dict):
        raise ValueError(f"roles: {where} must be an object")
    for key in value:
        if key not in keys:
            raise ValueError(f"roles: unknown key {where}.{key}")
    for key in keys if required is None else required:
        if key not in value:
            raise ValueError(f"roles: missing key {where}.{key}")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _tool_names(value, where):
    if not isinstance(value, list) or any(
            not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) for name in value):
        raise ValueError(f"roles: {where} must be a list of tool names")
    if len(set(value)) != len(value):
        raise ValueError(f"roles: {where} names a tool twice")
    return value


def validate_roles(table):
    """Validate a parsed role table; returns it unchanged."""
    _exact_keys(table, ("version",) + ROLE_NAMES, "table", ("version",) + _REQUIRED_ROLES)
    if not _is_number(table["version"]) or table["version"] != 1:
        raise ValueError("roles: version must be 1")
    for role in ROLE_NAMES:
        if role not in table:
            continue  # only an optional role can be absent: _exact_keys required the others
        value = table[role]
        _exact_keys(value, _ROLE_KEYS + _OPTIONAL_ROLE_KEYS, role, _ROLE_KEYS)
        if not isinstance(value["route"], str) or not value["route"]:
            raise ValueError(f"roles: {role}.route must be a non-empty string")
        if value["reasoning_effort"] not in _EFFORTS:
            raise ValueError(f"roles: {role}.reasoning_effort must be one of {', '.join(_EFFORTS)}")
        compaction = value["compaction"]
        _exact_keys(compaction, _COMPACTION_KEYS, role + ".compaction", ("thresholdRatio",))
        ratio = compaction["thresholdRatio"]
        if not _is_number(ratio) or not 0 < ratio <= 1:
            raise ValueError(f"roles: {role}.compaction.thresholdRatio must be a number in (0, 1]")
        retain = compaction.get("retainTokens")
        if retain is not None and not (_is_number(retain) and float(retain).is_integer() and retain >= 0):
            raise ValueError(f"roles: {role}.compaction.retainTokens must be a non-negative integer")
        if not isinstance(value["prompt"], str) or not _PROMPT_NAME.fullmatch(value["prompt"]):
            raise ValueError(f"roles: {role}.prompt must be a .md file name next to roles.json")
        presented = _tool_names(value["presented"], role + ".presented")
        forbidden = _tool_names(value["forbidden"], role + ".forbidden")
        both = [name for name in presented if name in forbidden]
        if both:
            raise ValueError(f"roles: {role} both presents and forbids {', '.join(both)}")
        if "presented_only" in value and not isinstance(value["presented_only"], bool):
            raise ValueError(f"roles: {role}.presented_only must be a boolean")
        if "catalog" in value and value["catalog"] not in _CATALOGS:
            raise ValueError(f"roles: {role}.catalog must be one of {', '.join(_CATALOGS)}")
        if "child_route" in value and not (isinstance(value["child_route"], str) and value["child_route"]):
            raise ValueError(f"roles: {role}.child_route must be a non-empty string")
        if "image_retain" in value:
            retain_images = value["image_retain"]
            _exact_keys(retain_images, _IMAGE_RETAIN_KEYS, role + ".image_retain")
            for key in _IMAGE_RETAIN_KEYS:
                count = retain_images[key]
                if not (_is_number(count) and float(count).is_integer() and count > 0):
                    raise ValueError(f"roles: {role}.image_retain.{key} must be a positive integer")
    return table


def load_roles(path=None):
    """Load and validate roles.json (read on every call: it is small, and a host
    restart must pick up the committed table)."""
    return validate_roles(json.loads(Path(path or ROLES_FILE).read_text()))


# State projects whose driver runs as a role other than `director`. The steward
# (plugin step 12) is bound to the one reserved project; every other project's
# driver is a director, exactly as before.
RESERVED_ROLES = {"aitelier-home": "steward"}
# The memory owner the steward's runtime is started for (AITELIER_DSH_MEMORY_USER).
STEWARD_MEMORY_USER = "owner"
# Step 24.2 (design-steward 7.8.12 "存储", 7.8.12a piece 3): the media library's root on this host,
# outside every container mount and outside the memory's git repository. A host may move it
# with AITELIER_MEDIA_ROOT (an absolute path); each writer keeps its own subdirectory.
MEDIA_ROOT_DEFAULT = "~/.local/share/dsh-director/media"


def media_root() -> str:
    configured = os.environ.get("AITELIER_MEDIA_ROOT", "").strip()
    if configured.startswith("/"):
        return configured
    return str(Path(MEDIA_ROOT_DEFAULT).expanduser())
# The steward's chat channel plugins (step 19, design-steward 7.2): each is loaded only
# when the secret files it reads exist under ~/.aitelier-secrets (the plugins read them
# from the same home), so a host without them runs exactly as before.
CHANNEL_PLUGINS = {
    "dsh-channel-slack": ("slack-app-token", "slack-bot-token"),
    "dsh-channel-wechat": ("wechat-bridge-token",),
}


def enabled_channel_plugins(checkout=None):
    """The channel plugin entries the steward's profile inserts: those whose secret files
    all exist (and whose plugin is in this checkout - a secret alone must not name a
    plugin DSH cannot load)."""
    checkout = Path(checkout or Path(__file__).resolve().parents[1])
    # Fixed to ~/.aitelier-secrets on purpose: the channel plugins read their tokens there
    # (step 19 contract C, D), whatever AITELIER_SECRETS_DIR says for the rest of the DSH
    # host (deploy/install_dsh_host.py). Moving one means moving all three together.
    secrets = owner_secrets_dir()
    entries = []
    for plugin, files in CHANNEL_PLUGINS.items():
        entry = checkout / "integrations" / plugin / "index.mjs"
        if entry.is_file() and all((secrets / name).is_file() for name in files):
            entries.append({"id": plugin, "name": str(entry)})
    return entries


def role_for_project(project_id):
    """The role a project's root session runs as: reserved ids by the table above,
    every other project `director`."""
    return RESERVED_ROLES.get(project_id, "director")


def _root_entry(table, project_id):
    """The roles.json entry for a project's root session. A reserved project whose
    role the table lacks is refused: it never falls back to the director entry."""
    role = role_for_project(project_id)
    if role not in table:
        raise ValueError(f"roles: project {project_id} runs as {role}, and roles.json has no {role} entry")
    return table[role]


@functools.lru_cache(maxsize=1)
def _serving_roles():
    """The table the completion endpoint routes by, read once per backend process:
    the backend sees the live checkout, and a route change takes effect with a
    backend rebuild/restart, never mid-session through an edited file."""
    return load_roles()


def completion_route(row, session_id, roles=None):
    """The model route a completion for `session_id` asks for.

    The root session is the project's role (the director, or the steward for its
    reserved project): the project row's model (the owner's per-project choice)
    overrides that role's route in the table. Every other session is a child: it
    gets the root role's `child_route` when the table gives one (step 24.2c: the
    steward's children take the steward route), else the table's worker route -
    never anything the row says."""
    table = roles or _serving_roles()
    if session_id == row["session_id"]:
        return row["model"] or _root_entry(table, row.get("project_id"))["route"]
    return _root_entry(table, row.get("project_id")).get("child_route") or table["worker"]["route"]


# Any session id that is not the row's root session id: completion_route sends
# every such session to the same child route, so one stands for all children.
_ANY_CHILD_SESSION = object()


def route_modalities(route, routes=None):
    """What a completion on `route` may be sent: `['text', 'image']` only when
    every candidate takes images (ModelRoutes.accepts_image), else `['text']`.
    A name the table cannot resolve is text only - it is not image-capable."""
    from core.model_routes import get_routes
    try:
        image = (routes or get_routes()).accepts_image(route)
    except RuntimeError:
        image = False
    return ["text", "image"] if image else ["text"]


def input_measured(routes=None):
    """Whether the route table has an `_input` at all. Until it has (no
    candidate measured yet) nothing is refused for carrying an image and the
    adapter keeps declaring image input, as before step 24.2b; only the new
    wake images stay placeholders."""
    from core.model_routes import get_routes
    return (routes or get_routes()).measures_input


def route_input(row, roles=None, routes=None):
    """Step 24.2b (design-steward 7.8.12a "路线能力"): what the root session and
    the child sessions may each be sent, by the same completion_route the
    completion endpoint routes by. `measured: false` is added when the table
    has no `_input` (input_measured): the adapter then keeps today's image
    handling instead of reading both routes as text-only."""
    out = {"root": route_modalities(completion_route(row, row["session_id"], roles), routes),
           "child": route_modalities(completion_route(row, _ANY_CHILD_SESSION, roles), routes)}
    if not input_measured(routes):
        out["measured"] = False
    return out


def compaction_config(row, roles=None):
    """compaction-basic config: one policy per role, values from the table.

    The root policy is the project's role's (the director's, or the steward's for
    its reserved project). AITELIER_DSH_COMPACT_RATIO still overrides the root
    ratio (clamped).
    The worker policy keys on the native subagents' provider/route; the top-level
    ratio (any other route) is the worker's too. When the director's own route IS
    the worker route, only the director policy is written: DSH refuses duplicate
    provider/model policies at load, and the director's applied there before."""
    table = roles or load_roles()
    root = _root_entry(table, row.get("project_id"))
    director, worker = root["compaction"], table["worker"]["compaction"]
    ratio = os.environ.get("AITELIER_DSH_COMPACT_RATIO", str(director["thresholdRatio"])).strip()
    try:
        ratio = min(0.8, max(0.2, float(ratio)))
    except ValueError:
        ratio = director["thresholdRatio"]
    # The same model the completion route uses for the director: the row's, or the
    # table's director route when the row names none (never a null model here).
    model = row["model"] or root["route"]
    policies = [{"provider": row["provider"], "model": model, **director, "thresholdRatio": ratio}]
    worker_target = (SUBAGENT_PROVIDER, table["worker"]["route"])
    if (row["provider"], model) != worker_target:
        policies.append({"provider": worker_target[0], "model": worker_target[1], **worker})
    return {"thresholdRatio": worker["thresholdRatio"], "modelPolicies": policies}


class DshDrivers:
    def __init__(self, db, registry, root: Path, *, command=None, base_url=None, host_controller=False):
        self.db, self.registry = db, registry
        self.root = Path(root)
        self.command = command or [os.getenv("AITELIER_DSH_BIN", "/opt/aitelier-dsh/node_modules/.bin/dsh")]
        self.base_url = base_url or os.getenv("AITELIER_DSH_API_URL", "http://127.0.0.1:4444")
        # The steward's gateway is served on its own port, published on the host's
        # loopback only (api/steward_gateway.py, director 487) - not under base_url.
        self.steward_gateway_url = (os.getenv("AITELIER_STEWARD_GATEWAY_URL", "").strip()
                                    or "http://127.0.0.1:4445/api/steward/mcp")
        self.processes = {}
        self.monitors = set()
        self.gateways = {}
        self.lock = asyncio.Lock()
        self.closing = False
        self.host_controller = host_controller
        with db.get_connection() as conn:
            conn.executescript(SCHEMA)
            # Additive: `CREATE TABLE IF NOT EXISTS` leaves an existing table
            # alone, so the column that records EVERY endpoint a session named
            # has to be added to a database that predates it. The rows already
            # there keep the one endpoint they named; they are not re-derived.
            # `add_column_if_missing` is safe against another constructor doing
            # the same check at the same moment: only the call that actually
            # added the column returns True, so the one-time backfill runs once.
            if add_column_if_missing(conn, "dsh_driver_session_models",
                                     "served_by_seen", "TEXT NOT NULL DEFAULT '[]'"):
                # A row written before the column existed named exactly one
                # endpoint (or none) - `served_by` is that list of length <= 1.
                for row in conn.execute("SELECT project_id,session_id,route,served_by "
                                        "FROM dsh_driver_session_models "
                                        "WHERE served_by IS NOT NULL AND served_by<>route").fetchall():
                    conn.execute("UPDATE dsh_driver_session_models SET served_by_seen=? "
                                 "WHERE project_id=? AND session_id=?",
                                 (json.dumps([row["served_by"]]), row["project_id"], row["session_id"]))
            # The invariant this column enforces also has to hold for what is
            # already stored - and on EVERY start, not only when the column is
            # added: a row whose `served_by` IS the route alias claims the alias
            # answered, which is exactly the defect, and a build older than this
            # one can still write such a row into this database (a rollback to an
            # older image, or a second deployment sharing the file). Measured
            # consequence of skipping it on an already-migrated database: the
            # alias is read back as if it were an endpoint
            # (repro/probe_alias_cleanup_boundary.py, kept with the delivery).
            conn.execute("UPDATE dsh_driver_session_models SET served_by=NULL "
                         "WHERE served_by=route")
            conn.commit()

    @property
    def external_host(self):
        """The shared installation setting selects the host controller, not uvicorn."""
        if self.host_controller:
            return False
        try:
            return json.loads((self.root / "runtime.json").read_text()).get("mode") == "host"
        except FileNotFoundError:
            return False

    def running(self, project_id):
        if not self.external_host:
            proc = self.processes.get(project_id)
            return proc is not None and proc.returncode is None
        row = self.get(project_id)
        if row is None or not row["enabled"]:
            return False
        try:
            state = json.loads((self.root / row["driver_id"] / "host-process.json").read_text())
            age = time.time() - state["seen_at"]
            return state["session_id"] == row["session_id"] and bool(state["pid"]) and 0 <= age < 15
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def rows(self):
        with self.db.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM dsh_drivers ORDER BY project_id")]

    def get(self, project_id):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM dsh_drivers WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def update(self, project_id, *, clear_restart_markers_of=None, **fields):
        """Stamp one driver row, tolerating a lock that outlives a single wait.

        A single attempt waits only its own connection's busy timeout, so a write
        lock held a moment longer lost the activity tick with an error. The
        patience is bounded from the OUTSIDE - `STAMP_LOCK_ATTEMPTS` fresh
        connections, each still waiting the full timeout - and not by shortening
        the timeout, which would break the short locks that already worked.
        A database that stays busy is still a failure the caller must see, never a
        silent success and never a load on the writer that outlasts the bound -
        but the failure is now TYPED and RETRYABLE (`DriverStampBusy`): it is
        still a `sqlite3.OperationalError` naming the locked database, and it is
        also the repository's busy refusal, so an HTTP transport answers it 503
        with `Retry-After` instead of an untyped 500 the caller cannot act on.
        Nothing is written by a refused stamp: the statement is one `UPDATE` and
        it never ran.
        """
        allowed = {"enabled", "phase", "subagents_json", "last_error"}
        if not fields.keys() <= allowed:
            raise ValueError("unsupported driver update")
        fields["updated_at"] = now()
        statement = ("UPDATE dsh_drivers SET " + ",".join(k + "=?" for k in fields) +
                     " WHERE project_id=?")
        params = (*fields.values(), project_id)
        for attempt in range(1, STAMP_LOCK_ATTEMPTS + 1):
            try:
                # A FRESH connection per attempt: the busy timeout is spent by
                # the connection that waited it out, so retrying on the failed
                # one would retry with no patience left.
                with self.db.get_connection() as conn:
                    # Stated, not inherited from `sqlite3.connect`'s default.
                    conn.execute(f"PRAGMA busy_timeout={int(STAMP_BUSY_TIMEOUT_MS)}")
                    conn.execute(statement, params)
                    if clear_restart_markers_of:
                        # Step 24.1a: spawning the steward's process deletes its fallback
                        # restart markers in the SAME transaction (the new process reads
                        # the whole tool list anyway). No table yet: no markers.
                        from core.state_steward_connections import clear_restart_markers
                        clear_restart_markers(conn, clear_restart_markers_of)
                    conn.commit()
                return
            except sqlite3.OperationalError as error:
                if not _is_lock_error(error):
                    raise
                if attempt == STAMP_LOCK_ATTEMPTS:
                    raise DriverStampBusy(
                        f"{STATE_WRITE_BUSY_CODE}: another writer held the write lock on "
                        f"{getattr(self.db, 'db_path', 'the database')} for the whole "
                        f"{STAMP_LOCK_ATTEMPTS} x {STAMP_BUSY_TIMEOUT_MS / 1000:g} s stamp "
                        f"budget (sqlite said {str(error)!r}); nothing was written, so retry "
                        f"the same request after {STATE_WRITE_BUSY_RETRY_AFTER} s",
                        STATE_WRITE_BUSY_RETRY_AFTER) from error
            time.sleep(STAMP_LOCK_PAUSE)

    def _discard_provision(self, project_id, driver_id, directory, actor):
        """Undo a provisioning attempt that lost the insert race.

        Best effort and never fatal: the caller has already established that
        another driver owns this project, so a failure to tidy up must not turn
        into a startup error. The directory goes last, after the membership, so a
        partial cleanup leaves a registered identity WITHOUT a usable token
        rather than a token belonging to nobody.
        """
        try:
            self.registry.set_membership(project_id, driver_id, "removed", 1,
                                         "lost DSH provisioning race", actor=actor,
                                         project_exists=lambda _: True)
        except Exception:
            pass
        shutil.rmtree(directory, ignore_errors=True)

    def _provision(self, project_id, provider, model, actor):
        if self.registry is None:
            raise ValueError("driver identity must be enabled to start a DSH driver")
        with self.db.get_connection() as conn:
            if not conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (project_id,)).fetchone():
                raise ValueError("State project does not exist")
        row = self.get(project_id)
        if row:
            return row
        # Do not derive a driver id from a possibly long or non-ASCII project id.
        driver_id = "dsh-" + uuid.uuid4().hex[:20]
        directory = self.root / driver_id
        directory.mkdir(parents=True, mode=0o700)
        # `role` is what the server-side steward restriction reads
        # (core/state_commands.execute, api/authz.write_denial_reason).
        registered = self.registry.register(driver_id, f"DSH · {project_id}"[:200],
                                            capabilities={"runtime": "dsh", "project_id": project_id,
                                                          "role": role_for_project(project_id)},
                                            actor=actor)
        token = directory / "token"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(registered["token"] + "\n")
        self.registry.set_membership(project_id, driver_id, "member", 0,
                                     "Managed per-project DSH driver", actor=actor,
                                     project_exists=lambda _: True)
        try:
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,updated_at) "
                             "VALUES(?,?,?,?,?,?)", (project_id, driver_id, provider, model,
                                                    "aitelier-" + driver_id, now()))
                conn.commit()
        except sqlite3.IntegrityError:
            # The check-then-insert above is not atomic across processes: the host
            # supervisor and a host-mode backend can both provision a project whose
            # row does not exist yet. The primary key picks a winner, but the loser
            # has already created a directory, written a token and registered a
            # second identity - an orphan holding a token for a project another
            # driver id already manages. Undo our own side effects and hand back
            # the winner's row: this is cleanup, not a contract change, and the
            # winner is untouched.
            self._discard_provision(project_id, driver_id, directory, actor)
            row = self.get(project_id)
            if row is None:  # the winner's row vanished too; report honestly
                raise
            return row
        return self.get(project_id)

    def _environment(self, row):
        directory = self.root / row["driver_id"]
        token = directory / "token"
        read_token_file(token)  # Explicit refusal rather than silently using owner credentials.
        profile = directory / "home" / "profiles" / "driver"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        (profile / "package.json").write_text(json.dumps({"private": True, "dsh": {
            "profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}))
        source = Path(__file__).resolve().parents[1] / "integrations" / "dsh-driver" / "runtime.mjs"
        director_source = Path(__file__).resolve().parents[1] / "integrations" / "dsh-director" / "index.mjs"
        # JSON is YAML too; the runtime installs its own native State/pipeline tools.
        # Local MCP services the driver may use, each switchable independently:
        # the URL is the switch. An unset or blank URL means "do not register it", so an
        # operator can take one away without editing code, and a host that does
        # not run these services keeps exactly the tool set it had before.
        #
        # The browser is the one exception: it is per-driver, because one
        # Playwright instance serves one client. See PWMCP_DRIVER_PORTS_ENV.
        #
        # failOnStartupError stays False for these: they are capabilities, not
        # prerequisites. A service that is down must cost the driver its tools,
        # not its ability to start - which is why the State MCP above is the only
        # one that fails hard.
        #
        # The mapping is parsed unconditionally: a malformed value is a config
        # error whether or not this driver's entry is the one that would be used.
        driver_ports = pwmcp_driver_ports()
        role = role_for_project(row["project_id"])
        # The steward gets NONE of these, and the rewrite strips them from a
        # profile written before this rule existed (the file is rewritten whole
        # on every start). Its role is `presented_only` for pwmcp, so it cannot
        # call one pwmcp tool - but pwmcp holds ONE client session and a second
        # `initialize` silently evicts the first, so a steward start pointing at
        # the SHARED 8931 URL kicked the director out of its browser mid-playtest
        # (its next call answered -32001 Session not found). devmcp (a shell),
        # zvec and agentmcp are unused by the role for the same reason: not
        # presented, so never callable. A director keeps all four, mapped port
        # and all.
        extra = []
        if role != "steward":
            for server in DSH_EXTRA_MCP_SERVERS:
                url = os.environ.get("AITELIER_DSH_MCP_" + server.upper(), "").strip()
                if not url:
                    # Unset or blank: not registered; the per-driver mapping
                    # must not resurrect a service the operator did not name.
                    continue
                if server == "pwmcp":
                    port = driver_ports.get(row["driver_id"])
                    if port is not None:
                        url = f"http://127.0.0.1:{port}/mcp"
                extra.append({"id": "mcp-" + server, "name": "@deepseek-ai/dsh-mcp-client", "config": {
                    "serverName": server, "transport": "streamable-http", "url": url,
                    "failOnStartupError": False, "toolCallTimeoutMs": 960000}})
        else:
            # Step 24.1a (design-steward 7.8.5): the steward's OWN connections all come
            # through ONE fixed entry, the backend's aggregating gateway; adding or
            # changing a connection never touches this profile (the gateway announces
            # tools/list_changed instead). `sw` keeps the public names short; the retry
            # count is large because the client gives up for good after it.
            extra.append({"id": "mcp-sw", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                "serverName": "sw", "transport": "streamable-http",
                "url": self.steward_gateway_url,
                "headers": {"X-AItelier-Driver-Token": "__DRIVER_TOKEN__"},
                "failOnStartupError": False, "toolCallTimeoutMs": 180000,
                "reconnect": {"maxAttempts": 1000000}}})
        # Auto-compact fires at `min(W * thresholdRatio, W - O - B)`; the shipped
        # ratio is 0.8. It is a RATIO of the model's own window, not a token
        # count: a quarter is 250k on a 1M window and 50k on a 200k one.
        #
        # The values are roles.json's (compaction_config). The director's own route
        # compacts near-clear (retainTokens 0 keeps only the last balanced step):
        # its work survives compaction by design - the State snapshot is
        # re-injected after every successful compaction - and the verbatim tail was
        # most of what a compaction left behind (measured 2026-11-06: 245-316k left
        # at a 500k trigger with the shipped 16% tail, ~50k after idle compaction,
        # which retains nothing). Workers have no such snapshot: they keep the
        # previous policy (half the window, DSH's default 16% verbatim tail), now
        # written as their own policy rather than left to the default.
        roles = load_roles()
        # dsh-base's tool-result-pruner runs first at pressure and trims old tool
        # outputs in place. Measured 2026-11-06 (wuxia-myth, 253k): a 21.5k trim
        # brought the context back under the trigger, so the summary never ran,
        # and the rewrite mid-history cost a 165k uncached re-prefill; the session
        # then hovers just under the trigger. Without it, pressure goes straight
        # to the summary (the director's near-clear above).
        patches = [
            {"id": "hmr", "disabled": True},
            {"id": "tool-result-pruner", "disabled": True},
            {"id": "compaction-basic", "config": compaction_config(row, roles)},
            {"id": "subagent", "config": {"maxActiveSubagents": MAX_ACTIVE_SUBAGENTS}},
            {"id": "tool-subagent", "config": {"provider": "spawn", "toolName": "subagent",
                "agentOptions": {"provider": SUBAGENT_PROVIDER, "model": roles["worker"]["route"]}}},
            # Built-in tools no driver used in a measured week (2026-11-07..14, ~83k
            # tool calls across two directors and 589 children): subagent_fork,
            # workflow, exit_plan_mode, and todo_write, which also shadows the
            # runtime's own todo_* plans. Off, not merely unlisted, so neither a
            # director nor a child can reach them. Rarely used tools stay loaded and
            # deferred instead (integrations/dsh-driver/tool-discovery.mjs).
            *[{"id": plugin, "disabled": True} for plugin in UNUSED_BUILTIN_TOOL_PLUGINS],
            # dsh-director (the generic director plugin) first, then this adapter,
            # which provides its `directorHost` service (plugin design step 2, MF1).
            # The steward has no State MCP wiring: its adapter and runtime use HTTP
            # only, and its token is refused at /mcp anyway (api/authz.py).
            {"insert": [{"id": "dsh-director", "name": str(director_source)},
                {"id": "aitelier-driver", "name": str(source)}] + ([] if role == "steward" else [
                {"id": "mcp-aitelier", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                    "serverName": "aitelier", "transport": "streamable-http",
                    "url": self.base_url.rstrip("/") + "/mcp",
                    "headers": {"X-AItelier-Driver-Token": "__DRIVER_TOKEN__"},
                    "failOnStartupError": True, "toolCallTimeoutMs": 960000}}]) + extra
                # The steward's chat channels, each only when its secrets exist.
                + (enabled_channel_plugins() if role == "steward" else [])},
        ]
        import yaml
        (profile / "cordis.patch.yml").write_text(yaml.safe_dump(patches, sort_keys=False).replace(
            "__DRIVER_TOKEN__", "!!js process.env.AITELIER_DRIVER_TOKEN"))
        env = dict(os.environ)
        for name in ("AITELIER_ADMIN_TOKEN", "AITELIER_DRIVER_TOKEN", "AITELIER_DRIVER_ID",
                     "AITELIER_DRIVER_TOKEN_FILE", "DSH_HOME", "AITELIER_DSH_ROLE", "AITELIER_DSH_MEMORY_USER"):
            env.pop(name, None)
        env.update(DSH_HOME=str(directory / "home"), AITELIER_DRIVER_ID=row["driver_id"],
                   AITELIER_DRIVER_TOKEN=read_token_file(token),
                   AITELIER_DRIVER_TOKEN_FILE=str(token), AITELIER_DSH_PROJECT=row["project_id"],
                   AITELIER_DSH_SESSION=row["session_id"], AITELIER_DSH_PROVIDER=row["provider"],
                   AITELIER_DSH_MODEL=row["model"], AITELIER_URL=self.base_url,
                   AITELIER_DSH_STATE_DIR=str(directory),
                   AITELIER_DSH_ENTRY=str(Path(self.command[0]).resolve()),
                   # The runtime's todo_* tools run `python -m dsh_todo` from the package's
                   # directory, with this checkout's hooks (core/dsh_todo_hooks.py).
                   AITELIER_TODO_PYTHON=sys.executable,
                   AITELIER_TODO_CWD=str(TODO_ROOT), **TODO_CLI_ENV,
                   AITELIER_DSH_MAX_ACTIVE_SUBAGENTS=str(MAX_ACTIVE_SUBAGENTS),
                   # Step 24.2 (design-steward 7.8.12a piece 3): the media root, given by this host to
                   # every driver process - a driver's own `~` is DSH_HOME's world under ~/.AItelier.
                   AITELIER_MEDIA_ROOT=media_root(),
                   # Step 24.2c (design-steward 7.8.12b "三"): the python that runs the steward's
                   # PDF reader (integrations/dsh-director/pdf_read.py) - this venv's, with pypdfium2.
                   AITELIER_MEDIA_PYTHON=sys.executable)
        if role == "steward":
            # The plugin's root role (HostConfig.rootRole); a director gets no
            # variable at all. The memory owner is the one owner for now.
            env.update(AITELIER_DSH_ROLE=role, AITELIER_DSH_MEMORY_USER=STEWARD_MEMORY_USER,
                       # Step 24.2 (7.8.12): the gateway the steward's media fetch goes through.
                       AITELIER_STEWARD_GATEWAY_URL=self.steward_gateway_url)
        if self.host_controller:
            env.update(DSH_PERMISSION_MODE="danger-full-access", AITELIER_DSH_EXECUTION="host",
                       AITELIER_DSH_BACKEND_SOURCE=str(Path(__file__).resolve().parents[1]))
        return env

    async def start(self, project_id, *, provider="aitelier", model=None, actor):
        # Before anything is provisioned: a reserved project whose role roles.json
        # lacks is refused here (ValueError -> 422), never started as a director,
        # whether or not the caller named a model.
        root = _root_entry(load_roles(), project_id)
        model = model or root["route"]
        async with self.lock:
            if self.closing:
                raise ValueError("driver manager is shutting down")
            # Only an explicit user action may ENABLE a driver. A recovery start
            # continues a row its owner left enabled; it is not consent to enable
            # one. The host supervisor reads every row once per tick and calls
            # back here from that snapshot (core/dsh_host.py), so a stop that lands
            # in between arrives as an enabled=0 row - and writing enabled=1 over
            # it would silently undo the user's stop and spawn a process nobody
            # asked for (measured 2026-11-17 on the deployed host runtime). The row
            # is therefore re-read here, under the same lock `stop` writes under,
            # and a disabled row is handed back exactly as it stands: no
            # enabled=1, no phase rewrite, no spawn.
            if actor in RECOVERY_ACTORS:
                current = self.get(project_id)
                if current is not None and not current["enabled"]:
                    return self.public(current)
            if self.external_host:
                row = self._provision(project_id, provider, model, actor)
                if not row["enabled"]:
                    await asyncio.to_thread(self.update, project_id, enabled=1, phase="starting",
                                            last_error="", subagents_json="[]")
                return self.public(self.get(project_id))
            proc = self.processes.get(project_id)
            if proc is not None and proc.returncode is None:
                return self.public(self.get(project_id))
            row = self._provision(project_id, provider, model, actor)
            directory = self.root / row["driver_id"]
            try:
                env = self._environment(row)
                await asyncio.to_thread(self.update, project_id, enabled=1, phase="starting",
                                        last_error="", subagents_json="[]",
                                        clear_restart_markers_of=row["driver_id"])
                log_fd = os.open(directory / "runtime.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(log_fd, "ab") as log:
                    proc = await asyncio.create_subprocess_exec(*self.command, "--profile", "driver",
                        env=env, cwd=directory, stdout=log, stderr=log, start_new_session=True)
                self.processes[project_id] = proc
                task = asyncio.create_task(self._observe(project_id, proc))
                self.monitors.add(task)
                task.add_done_callback(self.monitors.discard)
            except DriverStampBusy:
                # The CONTROL-PLANE stamp itself was refused, so nothing was
                # started and there is no failed start to record: this refusal is
                # retryable ("the lock was busy"), not a ValueError describing a
                # broken request. The clause sits FIRST because
                # `DriverStampBusy` is a `ValueError` (through `StateGraphError`),
                # so the branch below would otherwise answer it 422 and stamp
                # `enabled=0, phase="failed"` - claiming a start that was never
                # attempted, and relabelling a retryable condition as invalid.
                raise
            except (OSError, ValueError) as exc:
                # A start the RUNTIME made on its own (crash recovery, a fallback restart's
                # respawn) that cannot spawn is a failed start, not the owner's stop: the row
                # keeps enabled=1 so the host supervisor retries it with its backoff, and the
                # panel shows phase=failed with the error (design-steward 7.8.10a "起不来").
                # Writing enabled=0 here turned one transient error into a permanent stop.
                # A user's start that cannot spawn still answers that request with enabled=0.
                if actor in RECOVERY_ACTORS:
                    await asyncio.to_thread(self.update, project_id, phase="failed", last_error=str(exc))
                else:
                    await asyncio.to_thread(self.update, project_id, enabled=0, phase="failed",
                                            last_error=str(exc))
                raise ValueError(str(exc)) from exc
            return self.public(self.get(project_id))

    async def _observe(self, project_id, proc):
        code = await proc.wait()
        if self.processes.get(project_id) is proc:
            self.processes.pop(project_id, None)
            row = self.get(project_id)
            # The row can be deleted between the exit and this read. Reading
            # through None raised inside a task whose only consumer is
            # `add_done_callback(self.monitors.discard)`, so nobody retrieved the
            # exception: it surfaced only as "Task exception was never
            # retrieved" while the project kept a phase nobody updated. A deleted
            # project has no phase to set, so there is nothing to do.
            if row is not None and row["enabled"] and not self.closing:
                await asyncio.to_thread(self.update, project_id, phase="failed", subagents_json="[]",
                                        last_error=f"DSH exited with code {code}; see runtime.log")

    async def stop(self, project_id, *, preserve_enabled=False):
        async with self.lock:
            row = self.get(project_id)
            if row is None:
                raise ValueError("this project has no managed DSH driver")
            if self.external_host:
                await asyncio.to_thread(self.update, project_id, enabled=0, phase="stopping")
                # The host acknowledges shutdown by removing its process marker.
                marker = self.root / row["driver_id"] / "host-process.json"
                for _ in range(100):
                    if not marker.exists():
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise ValueError("host driver shutdown is still pending; inspect the host service")
                self.gateways.pop(project_id, None)
                await asyncio.to_thread(self.update, project_id, phase="stopped", subagents_json="[]")
                return self.public(self.get(project_id))
            # A restart (`preserve_enabled`) never writes `enabled`: an owner's stop that
            # landed meanwhile keeps its 0, so nothing respawns a driver its owner stopped
            # (design-steward 7.8.10a). Only an owner stop writes enabled=0.
            await asyncio.to_thread(self.update, project_id, phase="stopping",
                                    **({} if preserve_enabled else {"enabled": 0}))
            proc = self.processes.get(project_id)
            if proc is not None and proc.returncode is None:
                # The process has a private session/group. Never signal other drivers.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except ProcessLookupError:
                    await proc.wait()
            self.processes.pop(project_id, None)
            self.gateways.pop(project_id, None)
            await asyncio.to_thread(self.update, project_id, phase="stopped", subagents_json="[]")
            from core.driver_presence import declare
            # A stop with `preserve_enabled` is a RESTART, not an owner stop: the
            # panel restart path and `close()` for an enabled driver kill the
            # process only to bring it straight back. The offline declaration is
            # cleared solely by an `online` declaration, i.e. the director-only
            # State MCP tool `declare_driver_presence`, which a driver without
            # State MCP (the aitelier-home steward) never reaches - so writing it
            # here would leave that driver reading as offline for good while it
            # keeps running. Only `preserve_enabled=False` declares offline.
            if self.registry and not preserve_enabled:
                declare(self.db, row["driver_id"], "offline", "Driver stopped", None,
                        actor="driver:" + row["driver_id"])
            return self.public(self.get(project_id))

    async def resume(self):
        if self.external_host:
            return
        for row in self.rows():
            if row["enabled"]:
                try:
                    await self.start(row["project_id"], actor="dsh-runtime-recovery")
                except ValueError:
                    pass  # Failure remains visible on the dashboard; other projects still start.

    async def close(self):
        self.closing = True
        if self.external_host:
            return  # Backend restarts must not stop the host's resident sessions.
        for project_id in list(self.processes):
            await self.stop(project_id, preserve_enabled=bool(self.get(project_id)["enabled"]))
        if self.monitors:
            await asyncio.gather(*self.monitors)

    def history(self, project_id, *, before_seq=9007199254740991, limit=50):
        import subprocess
        row = self.get(project_id)
        if row is None:
            raise ValueError("this project has no managed DSH driver")
        root = self.root / row["driver_id"] / "home" / "sessions"
        if not root.exists():
            return {"driver_id": row["driver_id"], "session_id": row["session_id"],
                    "entries": [], "has_more": False, "next_before_seq": None}
        script = Path(__file__).resolve().parents[1] / "integrations" / "dsh-driver" / "history.mjs"
        env = dict(os.environ, AITELIER_DSH_ENTRY=str(Path(self.command[0]).resolve()))
        try:
            result = subprocess.run(["node", str(script), str(root), row["session_id"], str(before_seq), str(limit)],
                                    env=env, capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("DSH history unavailable: " + str(exc)) from exc
        if result.returncode:
            raise ValueError("DSH history unavailable: " + result.stderr[-1000:])
        return {"driver_id": row["driver_id"], **json.loads(result.stdout)}

    @staticmethod
    def _named_endpoint(served_by, route):
        """The endpoint this turn NAMED, or None when it could not name one.

        Two different ways a turn fails to name an endpoint, and the store owes
        the same answer to both. It answered but could not attribute the answer
        (None/''), or the gateway handed back the ROUTE argument it was given
        (`worker`) - an alias, not anywhere this session was served from. Both
        land here as None; neither may be turned into a string that reads like
        an endpoint, and `route` must never be stored as if the alias answered.
        """
        if not served_by or served_by == route:
            return None
        return served_by

    @staticmethod
    def _seen_endpoints(raw):
        """The stored JSON list, never a string that would break the reader."""
        try:
            values = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            return []
        return [v for v in values if isinstance(v, str) and v]

    def record_model_call(self, project_id, session_id, *, route, served_by):
        """Keep WHICH endpoints answered this session, durably, per project.

        Called by the completion endpoint for every answered turn, with the
        endpoint the gateway was actually bound to after any failover and the
        route the session asked for.

        `served_by` is the LAST endpoint this session could NAME - deliberately
        not "the endpoint of the last turn". A turn that cannot name one (empty,
        or the route alias) leaves the already-known endpoint in place: one
        unattributable turn says nothing about who answered the earlier ones, so
        writing NULL there would erase the session's only measured identity.

        `served_by_seen` is every endpoint this session has named, first-seen
        order, no duplicates, bounded by SERVED_BY_SEEN_LIMIT - the answer to
        "which model family wrote this" for a session that failed over, which a
        single last-value column gets wrong by construction.

        Neither column ever holds `route`: an endpoint equal to the alias is
        stored as unknown (NULL / absent from the list), here, so no caller can
        get it wrong by passing `served_by=gateway.model`.

        A HELD WRITE LOCK is retried a bounded three times: this call is
        best-effort - the turn is already answered and paid for - but losing it
        to another writer that is mid-transaction costs the owner a turn of
        usage/cost accounting, which is what `iss-0297fe77883241b5` measured
        twice in three hours. Every attempt is a whole transaction from BEGIN
        IMMEDIATE, so a retried attempt cannot double-count `calls`; a lock that
        outlives the window still raises to the caller, which is what keeps the
        `served unrecorded` warning (api/dsh_driver_routers.py) in place.
        """
        retry_while_locked(lambda: self._record_model_call_once(
            project_id, session_id, route=route, served_by=served_by))

    def _record_model_call_once(self, project_id, session_id, *, route, served_by):
        """ONE attempt: open, BEGIN IMMEDIATE, merge, COMMIT. See above."""
        endpoint = self._named_endpoint(served_by, route)
        stamp = now()
        with self.db.get_connection() as conn:
            # The merge below is a read-modify-write, so the write lock is taken
            # BEFORE the read: two turns for one session arriving at once must
            # not each merge onto the same list and lose one endpoint. Explicit
            # BEGIN/COMMIT because a DEFERRED transaction would only take the
            # lock at the INSERT, after both had read.
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT route,served_by,served_by_seen "
                                   "FROM dsh_driver_session_models "
                                   "WHERE project_id=? AND session_id=?",
                                   (project_id, session_id)).fetchone()
                seen = self._seen_endpoints(row["served_by_seen"]) if row else []
                if not seen and row is not None:
                    # A row written before `served_by_seen` existed named one
                    # endpoint at most; keep it as the first sighting instead of
                    # losing it to the backfill this process may have missed.
                    known = self._named_endpoint(row["served_by"], row["route"] or route)
                    if known:
                        seen.append(known)
                if endpoint and endpoint not in seen and len(seen) < SERVED_BY_SEEN_LIMIT:
                    seen.append(endpoint)
                conn.execute(
                    "INSERT INTO dsh_driver_session_models"
                    "(project_id,session_id,route,served_by,served_by_seen,first_seen,last_seen,calls) "
                    "VALUES(?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(project_id,session_id) DO UPDATE SET "
                    "route=excluded.route, "
                    # COALESCE, not excluded: an unnamed turn keeps the endpoint
                    # the session named earlier. This is the whole of c1 - the
                    # value only moves when a turn actually names one.
                    "served_by=COALESCE(excluded.served_by,dsh_driver_session_models.served_by), "
                    "served_by_seen=excluded.served_by_seen, "
                    "last_seen=excluded.last_seen, "
                    "calls=dsh_driver_session_models.calls+1",
                    (project_id, session_id, route, endpoint, json.dumps(seen), stamp, stamp))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass  # BEGIN itself may have been what failed; nothing to undo.
                raise

    def session_models(self, project_id):
        """This project's recorded sessions, oldest first. Never another project's.

        Returns the stored values as the read surface must hand them back:
        `served_by` is the last endpoint the session could NAME (NULL when it has
        named none) and `served_by_seen` is the parsed list of every endpoint it
        named, first-seen order - a caller never has to re-derive either from the
        raw JSON text or fall back to "well, it said worker".
        """
        with self.db.get_connection() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT session_id,route,served_by,served_by_seen,first_seen,last_seen,calls "
                "FROM dsh_driver_session_models WHERE project_id=? "
                "ORDER BY first_seen,session_id", (project_id,))]
        for row in rows:
            row["served_by_seen"] = self._seen_endpoints(row["served_by_seen"])
        return rows

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("project_id", "driver_id", "enabled", "phase", "provider", "model",
                                    "session_id", "last_error", "updated_at")}

    @staticmethod
    def _utc_now_iso() -> str:
        """Current UTC instant in the same shape state_attempts stores leases.

        Both sides are ISO-8601 with an explicit +00:00 offset, so a string
        comparison is a valid time comparison and avoids parsing per row.
        """
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def status(self):
        drivers = [d for d in (self.registry.list() if self.registry else []) if d["status"] != "retired"]
        for row in self.rows():
            for d in drivers:
                if d["driver_id"] == row["driver_id"]:
                    d["presence"] = {"status": "online" if self.running(row["project_id"])
                                     and row["phase"] in {"running", "waiting", "dormant"} else "offline"}
        seen = {d["driver_id"]: d for d in drivers}
        projects = {}
        with self.db.get_connection() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            members = conn.execute("SELECT project_id,driver_id FROM project_drivers WHERE status='member'").fetchall() if self.registry else []
            for m in members:
                if m["driver_id"] in seen:
                    projects.setdefault(m["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0, "unsettled_attempts": 0, "unsettled_expired_attempts": 0})["drivers"].append(seen[m["driver_id"]])
            for r in conn.execute("SELECT project_id,COUNT(*) n FROM state_attempts "
                                  "WHERE execution_kind='skillflow' AND status='running' GROUP BY project_id") if 'state_attempts' in tables else []:
                projects.setdefault(r["project_id"], {"drivers": [], "running_subagents": 0, "running_pipelines": 0, "unsettled_attempts": 0, "unsettled_expired_attempts": 0})["running_pipelines"] = r["n"]
            # "running_subagents" means LIVE workers, and only the native child
            # list tracks liveness: the runtime empties it when a child exits.
            # An external attempt proves a worker was REGISTERED, not that anyone
            # is working - two of three "running" attempts on 2026-11-03 had
            # leases expired 5 and 21 days earlier. Counting attempts as workers
            # therefore reports dead rows as staff.
            #
            # They are still worth seeing: an unsettled attempt is money spent
            # with no result recorded. So they get their own figure, split by
            # whether the lease is still good, and are never added into a running
            # count. Native ids are UUIDs and external_ids are free-form slugs, so
            # the two can never be matched by identity - which is the second
            # reason not to union them: a caller that registers both would be
            # counted twice for one worker.
            unsettled = {}
            for r in conn.execute("SELECT project_id,external_id,lease_expires_at FROM state_attempts "
                                  "WHERE execution_kind='external' AND status='running' "
                                  "AND project_id IS NOT NULL") if 'state_attempts' in tables else []:
                lease = r["lease_expires_at"] or ""
                live = bool(lease) and lease > self._utc_now_iso()
                slot = unsettled.setdefault(r["project_id"], {"live": set(), "expired": set()})
                slot["live" if live else "expired"].add(r["external_id"] or "")
            for pid, slot in unsettled.items():
                projects.setdefault(pid, {"drivers": [], "running_subagents": 0,
                                          "running_pipelines": 0, "unsettled_attempts": 0,
                                          "unsettled_expired_attempts": 0})
        for row in self.rows():
            p = projects.setdefault(row["project_id"], {"drivers": [], "running_subagents": 0,
                                                        "running_pipelines": 0,
                                                        "unsettled_attempts": 0,
                                                        "unsettled_expired_attempts": 0})
            # The dashboard lays the steward out by this, never by the project id.
            p["managed"] = {**self.public(row), "role": role_for_project(row["project_id"])}
            p["running_subagents"] = len(json.loads(row["subagents_json"])) if self.running(row["project_id"]) else 0
        for pid, slot in unsettled.items():
            p = projects[pid]
            p["unsettled_attempts"] = len(slot["live"]) + len(slot["expired"])
            p["unsettled_expired_attempts"] = len(slot["expired"])
        def counts(ds):
            return {"total": len(ds), "online": sum(d.get("presence", {}).get("status") == "online" and d["status"] == "active" for d in ds)}
        for p in projects.values():
            p.update(counts(p.pop("drivers")))
        return {**counts(drivers), "projects": projects}


# ---------------------------------------------------------------- step 24.1a: fallback restart
# How long a turn in progress may hold back a due fallback restart (design-steward 7.8.10a).
FALLBACK_WAIT_FOR_IDLE = timedelta(minutes=5)
FALLBACK_TICK_SECONDS = 10


def _error_code(exc) -> str:
    return type(exc).__name__[:100]


async def fallback_restart(runtime, row, *, then_start, moment=None) -> bool:
    """Execute a due `state_steward_restart_due` marker for this driver row, by whoever
    spawns its process (the DSH host's supervision loop; the backend in non-host mode,
    `then_start=True`). Returns True when it stopped the child.

    Waits while the row's phase is `running` (a turn), at most 5 minutes past the
    earliest due marker. Only a LIVE child of this runtime is restarted. One transaction
    re-reads `enabled`, deletes ALL the driver's markers and - when it deleted any and
    the driver is still enabled - records the restart (`state_steward_restart_done`)
    BEFORE the stop, so a crash in between never loops. The stop preserves `enabled`
    (an owner's stop that lands meanwhile wins); a failure is kept as a code in the
    record, which the gateway's check tells the owner about once."""
    from core.state_steward_connections import _iso, _utcnow, take_due_markers
    project, driver_id = row["project_id"], row["driver_id"]
    moment = moment or _utcnow()

    def due():
        with runtime.db.get_connection() as conn:
            return take_due_markers(conn, driver_id, moment)[0]
    earliest = await asyncio.to_thread(due)
    if earliest is None:
        return False
    if row.get("phase") == "running" and moment < earliest + FALLBACK_WAIT_FOR_IDLE:
        return False
    if not runtime.running(project):
        return False

    def account():
        with runtime.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            enabled = conn.execute("SELECT enabled FROM dsh_drivers WHERE project_id=?", (project,)).fetchone()
            reasons = [{"reason": r[0], "effective_hash": r[1]} for r in conn.execute(
                "SELECT reason, effective_hash FROM state_steward_restart_due WHERE driver_id=? ORDER BY reason",
                (driver_id,))]
            conn.execute("DELETE FROM state_steward_restart_due WHERE driver_id=?", (driver_id,))
            if not reasons or enabled is None or not enabled[0]:
                conn.commit()
                return None
            done = conn.execute("INSERT INTO state_steward_restart_done(driver_id,reasons_json,restarted_at) "
                                "VALUES(?,?,?)", (driver_id, json.dumps(reasons), _iso(_utcnow()))).lastrowid
            conn.commit()
            return done
    done = await asyncio.to_thread(account)
    if done is None:
        return False

    def failed(exc):
        with runtime.db.get_connection() as conn:
            conn.execute("UPDATE state_steward_restart_done SET error=? WHERE id=?", (_error_code(exc), done))
            conn.commit()
    try:
        await runtime.stop(project, preserve_enabled=True)
    except Exception as exc:  # noqa: BLE001 - recorded; the owner is told once
        await asyncio.to_thread(failed, exc)
        return False
    if then_start:
        try:
            await runtime.start(project, actor="dsh-runtime-recovery")
        except Exception as exc:  # noqa: BLE001 - recorded; the owner is told once
            await asyncio.to_thread(failed, exc)
    return True


async def fallback_restart_tick(runtime) -> None:
    """Non-host mode: one pass of the backend's own executor over every enabled row."""
    for row in await asyncio.to_thread(runtime.rows):
        if row["enabled"]:
            try:
                await fallback_restart(runtime, row, then_start=True)
            except Exception:  # noqa: BLE001 - the next tick tries again
                import logging
                logging.getLogger(__name__).exception("fallback restart of %s failed", row["project_id"])


async def fallback_restart_loop(runtime) -> None:
    while True:
        await fallback_restart_tick(runtime)
        await asyncio.sleep(FALLBACK_TICK_SECONDS)
