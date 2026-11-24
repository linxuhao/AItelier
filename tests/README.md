# Test tiers and how to run them

Run the suite only in a throwaway container (see `AGENTS.md`), never inside the
production container.

## Markers

`pytest.ini` registers these markers. The default `addopts` deselects
`network`, `docker` and `baked_toolchain` only. The tiers are opt-out: plain
`pytest` selects the `slow`, `meta`, `gate` and `serial` rows, because the
platform's repository gate (`aitelier/tools/run_tests/impl.py`) runs plain
`pytest` and must keep selecting every row it selected before the tiers existed.

| Marker | Default run | What it marks |
|---|---|---|
| `slow` | selected | Rows that take minutes of wall time: the node/vitest suites run from pytest (`tests/integration/test_driver_node_suite_runs.py`, which also runs the director suite, and `test_web_suite_runs.py`), the skillflow absence-gate modules (`tests/skillflow/test_coding_impl_gate_absence.py`, `test_coding_impl_absence_needs_nothing_else_red.py`) and the prose guard's round-9/10 self-proof poles (`test_truncation_is_not_a_formatting_mistake.py`, `test_prose_corruption_catalog.py`). |
| `meta` | selected | Guards over test sources or hand-kept lists rather than product behaviour: `test_no_verbatim_previous_line_dup.py`, `test_state_wait_bounds.py`, `test_driver_suite_lists_its_files.py`, the prose-constant accounting row, the private-ack-table union row and the steward write whitelist row. |
| `gate` | selected | Registered for integration gates that need a real clock or real subprocesses. No row carries it yet. |
| `serial` | selected | Rows that break under pytest-xdist; see "Parallel runs" below. |
| `network`, `docker`, `baked_toolchain` | deselected | See the comments in `pytest.ini`. |

An explicit `-m` on the command line replaces the whole `addopts` expression, so
name every exclusion you still want.

## Commands

```bash
# Everything (what plain `pytest` and the repository gate run, serially)
python -m pytest -q -p no:cacheprovider

# Recommended fast local run: skip the slow tier (this runs no driver or director
# javascript; see below)
python -m pytest -q -p no:cacheprovider -m "not network and not docker and not baked_toolchain and not slow"

# Slow tier only
python -m pytest -q -p no:cacheprovider -m slow

# Meta guards only, or the fast run without them
python -m pytest -q -p no:cacheprovider -m meta
python -m pytest -q -p no:cacheprovider -m "not network and not docker and not baked_toolchain and not slow and not meta"
```

The `serial` rows pass in a plain serial run, so plain `pytest` needs no second
invocation; only parallel runs split them out (see "Parallel runs" below).

The director suite has no row of its own. The driver row's discovery includes
`integrations/dsh-driver/plugin-boundary.test.mjs`, which imports every director test
file the director's npm script names, so the driver row runs every director test
(measured 2026-11-22 at `5638facc0c`: the 463 tests of the director suite run alone all
appear, by name, among the driver run's 773). The
driver row runs node with `--test-concurrency=2`; in a 2-CPU container node's default
is 1, which took about twice as long.

So the recommended fast command runs no driver and no director javascript: the driver
row is `slow` (163-221 s per run in a 2-CPU container at concurrency 2, measured
2026-11-22) and nothing else runs either suite. Only the WeChat channel suite (about
3 s, `tests/integration/test_director_and_channel_node_suites_run.py`) runs in the fast
command. Run the driver and director suites with
`python -m pytest -q -p no:cacheprovider tests/integration/test_driver_node_suite_runs.py`.

The skillflow absence-gate rows spend their time in real `test`-step re-runs (each a
nested pytest plus the `run_tests.sh` gate), not in sleeps: the clock is injected. So
each row drives only as many laps as its assertion needs. The laps row sets the
absence edge's `max_loop` to 5 on the loaded graph (`_wire(absence_max_loop=...)`)
instead of driving the shipped 100. It pins only one shipped value, that
`configs/coding_impl.yaml` still says `max_loop: 100`; the 60 s wait and 10800 s ceiling
are the row's own (product defaults 300 s and 12600 s), and it checks that 100 laps of
the measured lap length fit inside that ceiling. Whether the episode clock tracks elapsed
time is pinned by the pole C rows in `test_coding_impl_gate_absence.py`, not this row. Measured 2026-11-22 in a 2-CPU container, the two
modules went from 12 rows in 367 s to 11 rows in 121 s.

The node suites need `integrations/dsh-driver/node_modules` to resolve. In the
image that is `/opt/aitelier-dsh/node_modules`; a worktree mounted into a test
container needs the (untracked) symlink
`integrations/dsh-driver/node_modules -> /opt/aitelier-dsh/node_modules`. The
SPA job also needs `web/node_modules` linked to the same directory.

## Gate modes (2026-11-24)

`scripts/gate_modes.py` prints the mode plan as JSON and executes nothing by
default; `--run` executes its jobs in order, fail-fast. A refusal is a JSON
plan with `allowed: false`, `offending_path` and `prescribed_mode`.
Ask it for the plan you are about to pay for (add `--run` only when you mean it):

```bash
python scripts/gate_modes.py --mode targeted --changed tests/unit/test_gate_modes_plan.py
```

| Mode | Jobs (in order) | Measured wall seconds |
|---|---|---|
| `targeted` (diff rule below) | `affected-tests`, `whole-tree-guards` | affected-tests: 10.797 s (delivered tree, gate-modes-1124, 2026-11-24); whole-tree-guards: 174.420 s (delivered tree, gate-modes-1124, 2026-11-24) |
| `merge` / `full` | `python-tree`, `driver-node-suite`, `web-vitest-suite` | python-tree: not measured here (the whole ~2.9 h tree; no number invented); driver-node-suite: 253.140 s (delivered tree, gate-modes-1124, 2026-11-24); web-vitest-suite: 89.235 s (delivered tree, gate-modes-1124, 2026-11-24) |

Every job entry is `{name, argv, why}`. The argv are:

```bash
# affected-tests — changed pytest files only; no deselection
python -m pytest -q -p no:cacheprovider tests/unit/test_documented_gate_paths_exist.py tests/unit/test_gate_modes_plan.py
# whole-tree-guards — prose/structure guards read HEAD and every core module
python -m pytest -q -p no:cacheprovider tests/unit/test_truncation_is_not_a_formatting_mistake.py tests/unit/test_prose_corruption_catalog.py
# driver-node-suite — driver + director JS, its own job
python -m pytest -q -p no:cacheprovider tests/integration/test_driver_node_suite_runs.py
# web-vitest-suite — SPA vitest, its own job
python -m pytest -q -p no:cacheprovider tests/integration/test_web_suite_runs.py
# python-tree — exactly plain pytest's selection; pytest.ini is unchanged
python -m pytest tests
```

The container argv used for the measurements, byte for byte as retained next to each
log (`argv_*_job.txt` in the report directory of the run). The mounts differ per job
on purpose: the two jobs that read GitHub objects mount the shared `.git` read-only,
and the guard job mounts the tree read-only. `/home/linxuhao/...` is the measurement
checkout on this host.

```bash
docker run --rm --network none --cpus 2 --memory 2g --init -u 1000:1000 -v /home/linxuhao/.AItelier/director/work/gate-modes-1124:/app -v /home/linxuhao/AItelier/.git:/home/linxuhao/AItelier/.git:ro -w /app aitelier:latest python -m pytest -q -p no:cacheprovider tests/unit/test_documented_gate_paths_exist.py tests/unit/test_gate_modes_plan.py
docker run --rm --network none --cpus 2 --memory 2g --init -u 1000:1000 -v /home/linxuhao/.AItelier/director/work/gate-modes-1124:/app:ro -v /home/linxuhao/AItelier/.git:/home/linxuhao/AItelier/.git:ro -w /app aitelier:latest python -m pytest -q -p no:cacheprovider tests/unit/test_truncation_is_not_a_formatting_mistake.py tests/unit/test_prose_corruption_catalog.py
docker run --rm --network none --cpus 2 --memory 2g --init -u 1000:1000 -v /home/linxuhao/.AItelier/director/work/gate-modes-1124:/app -w /app aitelier:latest python -m pytest -q -p no:cacheprovider tests/integration/test_driver_node_suite_runs.py
docker run --rm --network none --cpus 2 --memory 2g --init -u 1000:1000 -v /home/linxuhao/.AItelier/director/work/gate-modes-1124:/app -w /app aitelier:latest python -m pytest -q -p no:cacheprovider tests/integration/test_web_suite_runs.py
```

Wall seconds are whole-container wall time measured on the delivered
gate-modes-1124 worktree on 2026-11-24, not pytest's internal summary; the only
difference between that measured tree and the final commit is these four table
values. The retained logs live under
`/home/linxuhao/.AItelier/director/reports/gate-modes-1124/logs/`. `python-tree`
was not run here: the whole suite takes hours; no estimate is presented as a
measurement. Note that `python-tree` is `pytest tests` and therefore also selects
the two JS wrapper rows that `driver-node-suite` and `web-vitest-suite` run on
their own: in `merge`/`full` those two suites are paid twice, once nested and
once as their own jobs, which is the point of splitting them out of the serial
Python run rather than a saving in the mode.

Targeted rule: a diff may take the `targeted` path only when every changed path
is under `tests/`, `docs/` or `design/`, is a `.md` file anywhere, or is a
Python file whose executable AST is unchanged (each docstring replaced with
`ast.Pass` on both blobs before comparison, so comments and docstring prose do
not count). Any other path refuses the targeted mode, names the offending path
and prescribes `merge`. `merge` and `full` both keep the whole Python
selection; nothing is deselected, skipped or made conditional.

## Parallel runs (pytest-xdist)

`pyproject.toml` pins `pytest-xdist`, so images built after that change carry it.
Images built before it do not. `pytest-timeout` is deliberately not pinned; see
the end of this section.

```bash
# Everything plain `pytest` selects, in two invocations: xdist for all but the
# serial rows, then the serial rows on their own
python -m pytest -q -p no:cacheprovider -n 4 --dist loadfile --basetemp=/tmp/xb -m "not network and not docker and not baked_toolchain and not serial"
python -m pytest -q -p no:cacheprovider -m serial

# The same without the slow tier
python -m pytest -q -p no:cacheprovider -n 4 --dist loadfile --basetemp=/tmp/xb -m "not network and not docker and not baked_toolchain and not slow and not serial"
```

`--dist loadfile` keeps each file on one worker, so module-level state is never
split.

Pass `--basetemp` with a short path. xdist puts each worker's `tmp_path` under
`popen-gwN/`, and that extra length pushes the unix sockets several rows bind
under `tmp_path` past Linux's 107-byte limit. Measured 2026-11-22: 48 rows failed
for that reason alone ("owned relay path exceeds Linux AF_UNIX's 107-byte
filesystem budget" in the repo-gate rows, "AF_UNIX path too long" in
`test_godot_render_backend.py`), and all of them pass with `--basetemp=/tmp/xb`.
`test_godot_render_backend.py` no longer needs it: its rig now keeps the
runtime root outside the pytest tmp tree, so its socket paths stay short under
any basetemp.
pytest empties the basetemp directory at start, so two concurrent runs on one
host need different paths.

Rows marked `serial` fail under xdist for reasons a path change does not fix.
Run them in a separate, non-parallel invocation, as above:

- `tests/integration/test_loop_stack_watchdog.py::test_real_watchdog_dumps_every_thread_while_one_is_parked_in_a_syscall`:
  execnet's thread shows up in the faulthandler dump but not in
  `threading.active_count()`.
- `tests/unit/test_dsh_todo_package.py::test_the_core_forwards_are_the_package_modules_with_aitelier_hooks`:
  order-dependent. `test_gate_deferral_execution_points.py` reloads
  `core.gate_deferral` in place, and loadfile can schedule that file first on
  the same worker.

`tests/integration/test_loop_stack_watchdog.py` used to leave
`api.main.app.state._test_mode` set to True after its `[main]` app rows, which
made `may_read_private` trust anonymous requests in every later file on the same
worker; when loadfile put `tests/unit/test_private_read_verdict_at_execution.py`
after it, that file's two `[main]` rows failed. The flag is now restored by the
test that sets it.

pytest-timeout, where installed, bounds a hung row with `--timeout <seconds>`.
`core/repository_executor_entry.py` adds `--timeout=120 --timeout-method=thread`
to repository-gate pytest runs that request `pytest_timeout` only when the gate's
own child interpreter, started with the gate's own environment, can import the
plugin. A plugin installed only for the interpreter running the suite (a
directory on `PYTHONPATH`, or a venv) is invisible to that child, so the gate
rows run without the flag rather than failing with
`unrecognized arguments: --timeout=120`. Because pyproject dependencies go into
the product image, pinning pytest-timeout would switch that 120 s gate limit on
for every target repository, and two slow-tier rows take longer than 120 s. It
stays unpinned until the owner accepts that.

## Sharding by measured duration

`tools/shard_by_duration.py split -n N --out-dir DIR` packs the test files into
N shards by measured seconds from `tools/test_durations.json`. Files the
durations file does not name are charged its median. Regenerate the file from
`--durations=0` logs with `tools/shard_by_duration.py measure LOG[:LIST] ...`.
`:LIST` spreads the part of a log's total that `--durations` did not list over
the files of LIST, which only makes sense for a serial log: in an xdist (`-n`) log
the total is wall time, so `measure` ignores LIST there and warns. A
`--durations=0` log lists every row and needs no LIST.
The committed file holds the fast run (everything but `slow`, the recommended
local command above) after the State template change, measured 2026-11-22 under
`-n 4`; files with no row outside `slow` are absent and charged the median.
`tests/unit/test_write_opening_coverage.py` was re-measured alone (serial, 2 CPUs)
after its mutation rows moved in-process and out of the slow tier.
Slow-tier rows are not in it, so dropping the director row and trimming the skillflow
absence rows changed no entry: the non-slow part of
`tests/integration/test_director_and_channel_node_suites_run.py` was always the WeChat
row alone (re-measured 2026-11-22 at 2.8 s, against the recorded 2.7 s).

## State database templates

Building State schema on a fresh SQLite file costs about 2 s under load (each
DDL statement and each last close is an fsync in WAL mode). A fixture that
needs an empty State world copies a template instead:

```python
from tests.support import state_template

@pytest.fixture
def world(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry, tmp_path / "state.sqlite"))
    ...  # the same constructors as before; on an existing schema they are cheap
```

`state_db_copy(recipe, dest)` (tests/conftest.py) builds the recipe's template
once per session (per xdist worker), copies it to `dest` and returns the path.
Every test gets its own file. Pick the recipe in tests/support/state_template.py
whose constructors match the fixture's (`state`, `state_registry`,
`state_registry_dsh`, `dbm`, `dbm_graph`, `dbm_graph_attempts`, `dbm_state`).
There is no universal template: product code branches on whether a table such
as `drivers` exists, so a template with extra tables would change what a test
exercises. Tests whose subject is schema creation, migration or write-lock
behaviour keep building fresh files. The conftest `db_manager` and `client`
fixtures use the `dbm` template.
