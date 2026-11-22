# Test tiers and how to run them

Run the suite only in a throwaway container (see `AGENTS.md`), never inside the
production container.

## Markers

`pytest.ini` registers these markers. The default `addopts` deselects
`network`, `docker`, `baked_toolchain` and `slow`.

| Marker | Default run | What it marks |
|---|---|---|
| `slow` | deselected | Rows that take minutes of wall time: the node/vitest suites run from pytest (`tests/integration/test_driver_node_suite_runs.py`, the director row of `test_director_and_channel_node_suites_run.py`, `test_web_suite_runs.py`), the skillflow absence-gate modules (`tests/skillflow/test_coding_impl_gate_absence.py`, `test_coding_impl_absence_needs_nothing_else_red.py`) and the prose guard's round-9/10 self-proof poles (`test_truncation_is_not_a_formatting_mistake.py`, `test_prose_corruption_catalog.py`). |
| `meta` | selected | Guards over test sources or hand-kept lists rather than product behaviour: `test_no_verbatim_previous_line_dup.py`, `test_state_wait_bounds.py`, `test_driver_suite_lists_its_files.py`, the prose-constant accounting row, the private-ack-table union row and the steward write whitelist row. |
| `gate` | selected | Registered for integration gates that need a real clock or real subprocesses. No row carries it yet. |
| `serial` | selected | Rows that break under pytest-xdist; see "Parallel runs" below. |
| `network`, `docker`, `baked_toolchain` | deselected | See the comments in `pytest.ini`. |

An explicit `-m` on the command line replaces the whole `addopts` expression, so
name every exclusion you still want.

## Commands

```bash
# Default tier (what plain `pytest` runs)
python -m pytest -q -p no:cacheprovider

# Slow tier only
python -m pytest -q -p no:cacheprovider -m slow

# Everything the default used to select before the slow tier existed
python -m pytest -q -p no:cacheprovider -m "not network and not docker and not baked_toolchain"

# Meta guards only, or the default tier without them
python -m pytest -q -p no:cacheprovider -m meta
python -m pytest -q -p no:cacheprovider -m "not network and not docker and not baked_toolchain and not slow and not meta"
```

The slow tier is not part of the default run, so whoever relies on the default
run as a gate must run `-m slow` as a second job. The platform's repository gate
(`aitelier/tools/run_tests/impl.py`) runs plain `pytest` and has no such job
yet: until one exists, a gate run of this repository does not run the driver,
director and web JS suites, the skillflow
absence gate or the prose self-proof poles.

The node suites need `integrations/dsh-driver/node_modules` to resolve. In the
image that is `/opt/aitelier-dsh/node_modules`; a worktree mounted into a test
container needs the (untracked) symlink
`integrations/dsh-driver/node_modules -> /opt/aitelier-dsh/node_modules`.

## Parallel runs (pytest-xdist)

`pyproject.toml` pins `pytest-xdist`, so images built after that change carry it.
Images built before it do not. `pytest-timeout` is deliberately not pinned; see
the end of this section.

```bash
python -m pytest -q -p no:cacheprovider -n 4 --dist loadfile --basetemp=/tmp/xb -m "not network and not docker and not baked_toolchain and not slow and not serial"
python -m pytest -q -p no:cacheprovider -m serial
```

`--dist loadfile` keeps each file on one worker, so module-level state is never
split.

Pass `--basetemp` with a short path. xdist puts each worker's `tmp_path` under
`popen-gwN/`, and that extra length pushes the unix sockets several rows bind
under `tmp_path` past Linux's 107-byte limit. Measured 2026-11-22: 48 rows failed
for that reason alone ("owned relay path exceeds Linux AF_UNIX's 107-byte
filesystem budget" in the repo-gate rows, "AF_UNIX path too long" in
`test_godot_render_backend.py`), and all of them pass with `--basetemp=/tmp/xb`.
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
Installing it has a side effect: `core/repository_executor_entry.py`
adds `--timeout=120 --timeout-method=thread` to repository-gate pytest runs that
request `pytest_timeout` whenever the plugin is importable. To try the plugins
in an older image, install them into a directory on `PYTHONPATH`. The gate's
child process builds its own environment and does not see that directory, so
installing pytest-timeout that way makes every gate test fail with
`unrecognized arguments: --timeout=120`. Install only `pytest-xdist execnet`
for such runs. Because pyproject dependencies go into the product image, pinning
pytest-timeout would switch that 120 s gate limit on for every target repository,
and two slow-tier rows take longer than 120 s. It stays unpinned until the owner
accepts that.

## Sharding by measured duration

`tools/shard_by_duration.py split -n N --out-dir DIR` packs the test files into
N shards by measured seconds from `tools/test_durations.json`. Files the
durations file does not name are charged its median. Regenerate the file from
`--durations=0` logs with `tools/shard_by_duration.py measure LOG[:LIST] ...`.
`:LIST` spreads the part of a log's total that `--durations` did not list over
the files of LIST, which only makes sense for a serial log: in an xdist (`-n`) log
the total is wall time, so `measure` ignores LIST there and warns. A
`--durations=0` log lists every row and needs no LIST.
The committed file holds the default tier (plain `pytest`, without `slow`)
after the State template change, measured 2026-11-22 under `-n 4`; files with no
default-tier row are absent and charged the median.
`tests/unit/test_write_opening_coverage.py` was re-measured alone (serial, 2 CPUs)
after its mutation rows moved in-process and into the default tier.

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
