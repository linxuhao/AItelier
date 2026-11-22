"""tools/shard_by_duration.py: a --durations log parses to per-file seconds, and split
packs files into balanced shards with every file in exactly one shard.

Balanced means the greedy longest-first bound: the heaviest shard exceeds the lightest
by at most the single most expensive file. Files the durations do not name are charged
the median of the ones they do name.
"""
from __future__ import annotations

import importlib.util
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "shard_by_duration.py"
COMMITTED = ROOT / "tools" / "test_durations.json"


def _load():
    spec = importlib.util.spec_from_file_location("shard_by_duration_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shard = _load()

SERIAL_LOG = """\
============================= slowest durations ==============================
6.00s call     tests/unit/test_a.py::test_one
2.00s setup    tests/unit/test_a.py::test_one
1.50s call     tests/unit/test_b.py::test_two[x]
0.50s teardown tests/unit/test_b.py::test_two[x]
============ 9 passed in 14.00s ============
"""


def _assert_partition_and_balanced(bins, files, cost, shards):
    assert len(bins) == shards
    assigned = Counter(name for _, members in bins for name in members)
    assert assigned == Counter(files), "every file must land in exactly one shard"
    for total, members in bins:
        assert abs(total - sum(cost[name] for name in members)) < 1e-6
    loads = [total for total, _ in bins]
    assert max(loads) - min(loads) <= max(cost.values()) + 1e-6


def test_measure_sums_rows_per_file_and_spreads_the_serial_residual(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(SERIAL_LOG)
    listing = tmp_path / "files.txt"
    listing.write_text("tests/unit/test_a.py tests/unit/test_b.py tests/unit/test_c.py\n")

    assert shard.measure([str(log)]) == {"tests/unit/test_a.py": 8.0, "tests/unit/test_b.py": 2.0}
    # 14.0 s total - 10.0 s listed = 4.0 s spread over the three files of the listing.
    assert shard.measure([f"{log}:{listing}"]) == {
        "tests/unit/test_a.py": 9.3,
        "tests/unit/test_b.py": 3.3,
        "tests/unit/test_c.py": 1.3,
    }


def test_measure_gives_an_xdist_log_no_residual_share(tmp_path, capsys):
    log = tmp_path / "xdist.log"
    log.write_text("bringing up nodes...\n" + SERIAL_LOG)
    listing = tmp_path / "files.txt"
    listing.write_text("tests/unit/test_c.py\n")

    assert shard.measure([f"{log}:{listing}"]) == {"tests/unit/test_a.py": 8.0, "tests/unit/test_b.py": 2.0}
    assert "xdist log" in capsys.readouterr().err


def test_split_of_parsed_durations_is_a_balanced_partition_with_unknowns_at_the_median(tmp_path):
    log = tmp_path / "serial.log"
    log.write_text(SERIAL_LOG)
    durations = shard.measure([str(log)])  # test_a 8.0, test_b 2.0
    durations.update({"tests/unit/test_d.py": 10.0, "tests/unit/test_e.py": 3.0})
    files = sorted(durations) + ["tests/unit/test_unknown.py", "tests/unit/test_unknown2.py"]

    bins, default = shard.split(files, durations, 3)

    assert default == statistics.median([8.0, 2.0, 10.0, 3.0]) == 5.5
    cost = {name: durations.get(name, default) for name in files}
    _assert_partition_and_balanced(bins, files, cost, 3)
    # Longest first: the 10 s file and the 8 s file never share a shard.
    assert not any({"tests/unit/test_d.py", "tests/unit/test_a.py"} <= set(m) for _, m in bins)


def test_split_packs_longest_first():
    durations = {"a": 5.0, "b": 4.0, "c": 3.0, "d": 3.0, "e": 3.0}
    bins, _ = shard.split(sorted(durations), durations, 2)
    # Longest first: 5 | 4, then each 3 to the lighter shard -> 8 and 10. Shortest
    # first would end at 11 and 7.
    assert sorted(total for total, _ in bins) == [8.0, 10.0]


def test_split_without_any_durations_charges_one_second_each():
    files = [f"tests/unit/test_{i}.py" for i in range(7)]
    bins, default = shard.split(files, {}, 3)
    assert default == 1.0
    _assert_partition_and_balanced(bins, files, dict.fromkeys(files, 1.0), 3)


def test_committed_durations_split_every_test_file_once_and_balanced():
    durations = json.loads(COMMITTED.read_text())
    assert durations and all(
        name.startswith("tests/") and name.endswith(".py") and isinstance(value, (int, float)) and value >= 0
        for name, value in durations.items()
    )
    files = shard._test_files()
    bins, default = shard.split(files, durations, 6)
    cost = {name: durations.get(name, default) for name in files}
    _assert_partition_and_balanced(bins, files, cost, 6)


def test_cli_out_dir_writes_one_file_per_shard_covering_every_path(tmp_path):
    durations = tmp_path / "d.json"
    durations.write_text(json.dumps({"tests/x/test_a.py": 4.0, "tests/x/test_b.py": 1.0}))
    paths = ["tests/x/test_a.py", "tests/x/test_b.py", "tests/x/test_new.py"]
    out = tmp_path / "shards"

    assert shard.main(["split", "-n", "2", "--durations", str(durations), "--out-dir", str(out), *paths]) == 0

    written = [p.read_text().split() for p in sorted(out.glob("shard*.txt"))]
    assert len(written) == 2
    assert Counter(name for members in written for name in members) == Counter(paths)
