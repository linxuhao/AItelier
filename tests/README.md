# Test tiers and how to run them

Run the suite only in a throwaway container (see `AGENTS.md`), never inside the
production container.

## Markers

`pytest.ini` registers these markers. The default `addopts` deselects
`network`, `docker`, `baked_toolchain` and `slow`.

| Marker | Default run | What it marks |
|---|---|---|
| `slow` | deselected | Rows that take minutes of wall time: the node/vitest suites run from pytest (`tests/integration/test_driver_node_suite_runs.py`, the director row of `test_director_and_channel_node_suites_run.py`, `test_web_suite_runs.py`), the nested-pytest write-opening mutation rows in `tests/unit/test_write_opening_coverage.py`, the skillflow absence-gate modules (`tests/skillflow/test_coding_impl_gate_absence.py`, `test_coding_impl_absence_needs_nothing_else_red.py`) and the prose guard's round-9/10 self-proof poles (`test_truncation_is_not_a_formatting_mistake.py`, `test_prose_corruption_catalog.py`). |
| `meta` | selected | Guards over test sources or hand-kept lists rather than product behaviour: `test_no_verbatim_previous_line_dup.py`, `test_state_wait_bounds.py`, `test_driver_suite_lists_its_files.py`, the prose-constant accounting row, the private-ack-table union row and the steward write whitelist row. |
| `gate` | selected | Registered for integration gates that need a real clock or real subprocesses. No row carries it yet. |
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
run as a gate must run `-m slow` as a second job.

The node suites need `integrations/dsh-driver/node_modules` to resolve. In the
image that is `/opt/aitelier-dsh/node_modules`; a worktree mounted into a test
container needs the (untracked) symlink
`integrations/dsh-driver/node_modules -> /opt/aitelier-dsh/node_modules`.

## Parallel runs (pytest-xdist)

`pyproject.toml` pins `pytest-xdist` and `pytest-timeout`, so images built after
that change carry both. Images built before it have neither.

```bash
python -m pytest -q -p no:cacheprovider -n 4 --dist loadfile -m "not network and not docker and not baked_toolchain and not slow and not serial"
python -m pytest -q -p no:cacheprovider -m serial
```

`--dist loadfile` keeps each file on one worker, so module-level state is never
split. Rows marked `serial` break when other files run at the same time; run
them in a separate, non-parallel invocation, as above. `--timeout <seconds>`
(pytest-timeout) bounds a hung row.

Installing pytest-timeout has a side effect: `core/repository_executor_entry.py`
adds `--timeout=120 --timeout-method=thread` to repository-gate pytest runs that
request `pytest_timeout` whenever the plugin is importable. To try the plugins
in an older image, install them into a directory on `PYTHONPATH`. The gate's
child process builds its own environment and does not see that directory, so
installing pytest-timeout that way makes every gate test fail with
`unrecognized arguments: --timeout=120`. Install only `pytest-xdist execnet`
for such runs.

## Sharding by measured duration

`tools/shard_by_duration.py split -n N --out-dir DIR` packs the test files into
N shards by measured seconds from `tools/test_durations.json`. Files the
durations file does not name are charged its median. Regenerate the file from
`--durations=0` logs with `tools/shard_by_duration.py measure LOG[:LIST] ...`.
The committed file was seeded from the 2026-11-21 full-suite audit shards.
