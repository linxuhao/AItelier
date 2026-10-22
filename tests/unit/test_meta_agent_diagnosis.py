"""The butler's diagnosis surface: trace, tool registry, config source.

A failed run's *reason* lives in its trace — gate tools record their error in the
trace payload and leave an empty output dir behind, so no file-reading tool can
surface it. These cover the tools that close that gap, plus the registry/config
readers that let the agent check what already exists before building more.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest
import yaml

from core.meta_agent import MetaAgent
from core.trace_reader import trace_list


@pytest.fixture
def agent(tmp_path):
    return MetaAgent(MagicMock(), MagicMock(), owner_email="test@local")


# ── a stand-in trace DB, same shape skillflow writes ─────────────────────

TRACE_ROWS = [
    # (step_id, category, event, payload)
    ("survey", "step", "claimed", {"agent": "forge_surveyor"}),
    ("v_lint", "tool_result", "forge_lint", {"source": "tool_step", "error": "", "passed": True}),
    ("v_registry", "tool_call", "forge_registry_check",
     {"source": "tool_step", "params": {"graph_path": "/x/pipeline.yaml", "role_table": "/x/rt.yaml"}}),
    ("v_registry", "tool_result", "forge_registry_check",
     {"source": "tool_step", "passed": False,
      "error": "Registry check failed — step 'spec': agent_config 'spec_maker' not defined in role table"}),
    ("emit_review", "response", "agent_response", {"text": "The graph is structurally sound."}),
]


@pytest.fixture
def traced_run(tmp_path, agent, monkeypatch):
    """Wire a fake skillflow whose trace_query hits a real sqlite DB."""
    db = tmp_path / "trace.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE skillflow_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_id TEXT,
        step_instance_id INTEGER, seq INTEGER NOT NULL, category TEXT NOT NULL,
        event TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for i, (step, cat, ev, payload) in enumerate(TRACE_ROWS, start=1):
        conn.execute("INSERT INTO skillflow_trace (run_id, step_id, seq, category, event, "
                     "payload_json) VALUES (?,?,?,?,?,?)",
                     ("run-1", step, i, cat, ev, json.dumps(payload)))
    conn.commit()
    conn.row_factory = sqlite3.Row

    run_row = {"id": "run-1", "project_id": "forge-demo-1", "status": "failed",
               "error": "Cycle limit exceeded", "graph_name": "pipeline_forge"}
    sf = MagicMock()
    sf.get_run.side_effect = lambda r: run_row if r == "run-1" else None
    sf.list_runs.side_effect = lambda project_id=None, **k: (
        [run_row] if project_id == "forge-demo-1" else [])
    sf.trace_query.side_effect = lambda rid, sql, params=(): conn.execute(sql, params).fetchall()

    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    return sf


class TestTrace:
    def test_lists_newest_first_by_default(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1"})
        assert res["count"] == len(TRACE_ROWS)
        assert [e["seq"] for e in res["entries"]] == sorted(
            [e["seq"] for e in res["entries"]], reverse=True)
        assert res["run_status"] == "failed"

    def test_accepts_a_project_id(self, agent, traced_run):
        """The butler mostly holds project ids — both must work."""
        res = agent._tool_trace_list({"run": "forge-demo-1"})
        assert res["run_id"] == "run-1"
        assert res["count"] == len(TRACE_ROWS)

    def test_unknown_run_says_what_it_tried(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "nope"})
        assert "No run found" in res["error"]

    def test_errors_only_surfaces_the_real_reason(self, agent, traced_run):
        """The whole point: 'Cycle limit exceeded' → the gate error behind it."""
        res = agent._tool_trace_list({"run": "run-1", "errors_only": True})
        assert res["count"] == 1
        entry = res["entries"][0]
        assert entry["step"] == "v_registry"
        assert "not defined in role table" in entry["summary"]

    def test_errors_only_skips_a_passing_gate(self, agent, traced_run):
        """v_lint records error:"" on success — that must not read as a failure."""
        res = agent._tool_trace_list({"run": "run-1", "errors_only": True})
        assert all(e["step"] != "v_lint" for e in res["entries"])

    def test_filters_by_step(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1", "step": "v_registry"})
        assert res["count"] == 2
        assert {e["step"] for e in res["entries"]} == {"v_registry"}

    def test_filters_by_category(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1", "category": "tool_result"})
        assert {e["category"] for e in res["entries"]} == {"tool_result"}

    def test_summaries_are_capped(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1"})
        assert all(len(e["summary"]) <= 221 for e in res["entries"])

    def test_search_finds_the_step_that_failed(self, agent, traced_run):
        res = agent._tool_trace_search({"run": "run-1", "query": "role table"})
        assert res["count"] == 1
        assert res["entries"][0]["step"] == "v_registry"

    def test_search_requires_a_query(self, agent, traced_run):
        assert "error" in agent._tool_trace_search({"run": "run-1"})

    def test_read_returns_full_payloads(self, agent, traced_run):
        res = agent._tool_trace_read({"run": "run-1", "seq": 3, "seq_end": 4})
        assert res["count"] == 2
        payload = res["entries"][-1]["payload"]
        assert payload["passed"] is False
        assert "spec_maker" in payload["error"]        # untruncated

    def test_read_caps_the_range(self, agent, traced_run):
        res = agent._tool_trace_read({"run": "run-1", "seq": 1, "seq_end": 9999})
        assert res["count"] <= 20

    def test_read_needs_an_integer_seq(self, agent, traced_run):
        assert "error" in agent._tool_trace_read({"run": "run-1", "seq": "abc"})


# ── errors_only must not lose real failures behind look-alike rows ───────

@pytest.fixture
def trace_db(monkeypatch):
    """Factory: a skillflow stand-in over a real sqlite skillflow_trace table.

    Rows are (run_id, step, category, event, payload); a dict/list payload is stored
    as JSON, a str verbatim, seq counting from 1 per run. `runs` declares runs that
    may have no rows. The result has .add(...) to append a row, .after_query (called
    with the query count after every query) and .newest_first (project -> run ids).
    """
    conns = []

    def build(rows, runs=("run-1",)):
        conn = sqlite3.connect(":memory:")
        conns.append(conn)
        conn.execute("""CREATE TABLE skillflow_trace (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_id TEXT,
            step_instance_id INTEGER, seq INTEGER NOT NULL, category TEXT NOT NULL,
            event TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        conn.row_factory = sqlite3.Row
        seqs: dict[str, int] = {}

        def add(run, step, cat, event, payload):
            seqs[run] = seqs.get(run, 0) + 1
            conn.execute("INSERT INTO skillflow_trace (run_id, step_id, seq, category, "
                         "event, payload_json) VALUES (?,?,?,?,?,?)",
                         (run, step, seqs[run], cat, event,
                          payload if isinstance(payload, str) else json.dumps(payload)))
            conn.commit()

        for row in rows:
            add(*row)
        known = {r: {"id": r, "project_id": f"proj-{r}", "status": "failed",
                     "error": None, "graph_name": "pipeline_forge"}
                 for r in {*runs, *seqs}}
        sf = MagicMock()
        sf.conn, sf.add, sf.after_query, sf.newest_first = conn, add, None, {}

        def list_runs(project_id=None, **k):
            if project_id in sf.newest_first:
                return [known[r] for r in sf.newest_first[project_id]]
            return [r for r in known.values() if r["project_id"] == project_id]

        def query(rid, sql, params=()):
            got = conn.execute(sql, params).fetchall()
            if sf.after_query:
                sf.after_query(sf.trace_query.call_count)
            return got

        sf.get_run.side_effect = known.get
        sf.list_runs.side_effect = list_runs
        sf.trace_query.side_effect = query
        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        return sf
    yield build
    for conn in conns:
        conn.close()


# Payloads that are NOT failures but look like them to a text search ("error",
# "false", "failed").
NOISE = [
    {"source": "tool_step", "error": "", "passed": True},        # a passing gate
    {"error": "  \n\t "},                                        # blank after strip
    {"error": "\u00a0\u2003 "},                                  # unicode blank
    {"passed": 0, "note": "a false-positive hint"},              # 0 is not False
    {"passed": "false"},                                         # text, not boolean
    {"status": "Failed", "detail": "case differs"},
    {"text": "the earlier attempt failed; this one is fine"},
    {"error": None, "text": "no error"},
    {"error": ["not", "a", "string"]},
    {"error": 0, "passed": 1},
    {"error": {"code": 7}},
    {"status": ["failed"]},
    {"prompt": "Handle the error path and return false when empty. " * 20},
    "error: not JSON at all, false, failed",                     # stored verbatim
]


def _failure(n: int) -> tuple[dict, str]:
    """A payload that IS a failure, and the summary it must be listed with."""
    kind = n % 5
    if kind == 0:
        text = f"Validation failed: item {n} is malformed"
        return {"error": text}, text
    if kind == 1:
        return {"source": "tool_step", "passed": False}, "passed=false"
    if kind == 2:                                   # no "error"/"false" text at all
        return {"status": "failed", "detail": f"step {n} crashed"}, f"step {n} crashed"
    if kind == 3:
        return {"error": f"  padded failure {n}\n"}, f"padded failure {n}"
    return {"passed": True, "error": f"warning {n}"}, f"warning {n}"


def _scenario(layout: str, n_failures: int, n_noise: int) -> list[tuple]:
    """(payload, expected summary or None) in seq order, noise placed per layout."""
    noise = [(NOISE[i % len(NOISE)], None) for i in range(n_noise)]
    failures = [_failure(i) for i in range(n_failures)]
    if layout == "noise_first":
        return noise + failures
    if layout == "noise_last":
        return failures + noise
    gap = n_noise // max(n_failures, 1)             # noise before each failure
    items = []
    for i, failure in enumerate(failures):
        items += noise[i * gap:(i + 1) * gap] + [failure]
    return items + noise[n_failures * gap:]


class TestErrorsOnlyAcrossNoise:
    """errors_only applies its rule before `limit`, not after a fixed SQL window."""

    @pytest.mark.parametrize("limit", [2, 12])
    @pytest.mark.parametrize("order", ["asc", "desc"])
    @pytest.mark.parametrize("layout", ["noise_first", "noise_last", "interleaved"])
    @pytest.mark.parametrize("n_noise", [0, 13, 250])
    @pytest.mark.parametrize("n_failures", [0, 2, 30])
    def test_lists_the_first_limit_failures(self, trace_db, n_failures, n_noise,
                                            layout, order, limit):
        items = _scenario(layout, n_failures, n_noise)
        sf = trace_db([("run-1", "s", "step", "evt", p) for p, _ in items])
        want = [(i, s) for i, (_, s) in enumerate(items, start=1) if s is not None]
        if order == "desc":
            want.reverse()
        res = trace_list(sf, "run-1", errors_only=True, limit=limit, order=order)
        assert [(e["seq"], e["summary"]) for e in res["entries"]] == want[:limit]
        assert res["count"] == len(want[:limit])

    @pytest.fixture
    def stepped(self, trace_db):
        """A step that failed, was retried past look-alike rows and failed again;
        another step and another run fail in the same table."""
        rows = [("run-1", "2", "step", "validation_failed",
                 {"error": "Validation failed:\nitems[0].id must be stable; "
                           "items[1].id must be stable"})]
        rows += [("run-1", "2", ("tool_call", "response")[i % 2], "turn",
                  NOISE[i % len(NOISE)]) for i in range(300)]
        late = "Checkpoint output 'inventory' is unavailable: No such file"
        rows += [("run-1", "1", "step", "validation_failed", {"error": "other step"}),
                 ("run-2", "2", "step", "validation_failed", {"error": "other run"}),
                 ("run-1", "2", "step", "validation_failed", {"error": late}),
                 ("run-1", "2", "step", "claimed",
                  {"attempt_feedback": True, "validation_error": late, "instance_n": 2}),
                 ("run-1", "2", "step", "resumed_from_trace",
                  {"step_id": "2", "turns": 8, "written_files": ["step2_design.md"]})]
        return trace_db(rows)

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_a_late_failure_is_not_hidden_by_the_rows_before_it(
            self, agent, stepped, order):
        res = agent._tool_trace_list({"run": "run-1", "step": "2", "errors_only": True,
                                      "order": order, "limit": 12})
        want = [1, 303] if order == "asc" else [303, 1]
        assert [e["seq"] for e in res["entries"]] == want
        by_seq = {e["seq"]: e for e in res["entries"]}
        assert {e["event"] for e in by_seq.values()} == {"validation_failed"}
        assert by_seq[1]["summary"].startswith(
            "Validation failed: items[0].id must be stable")
        assert by_seq[303]["summary"] == (
            "Checkpoint output 'inventory' is unavailable: No such file")

    @pytest.fixture
    def grid(self, trace_db):
        """Every run x step x category block: 120 look-alikes, then 3 failures."""
        rows = []
        for run in ("run-1", "run-2"):
            for step in ("a", "b"):
                for cat in ("step", "tool_result"):
                    rows += [(run, step, cat, "evt", NOISE[i % len(NOISE)])
                             for i in range(120)]
                    rows += [(run, step, cat, "evt",
                              {"error": f"fail {run} {step} {cat} {k}"})
                             for k in range(3)]
        return trace_db(rows)

    @pytest.mark.parametrize("ref", ["run-1", "proj-run-1"])
    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_filters_isolate_run_step_and_category(self, grid, ref, order):
        def block(step, cat):
            return [f"fail run-1 {step} {cat} {k}" for k in range(3)]

        def check(blocks, limit, **filters):
            want = [t for b in blocks for t in b]
            if order == "desc":
                want.reverse()
            res = trace_list(grid, ref, errors_only=True, limit=limit, order=order,
                             **filters)
            assert [e["summary"] for e in res["entries"]] == want[:limit]
            assert ("resolved" in res) == (ref != "run-1")

        check([block("b", "tool_result")], 2, step="b", category="tool_result")
        check([block("a", "step"), block("a", "tool_result")], 5, step="a")
        check([block("a", "step"), block("b", "step")], 12, category="step")
        check([block("a", "step"), block("a", "tool_result"),
               block("b", "step"), block("b", "tool_result")], 12)

    def test_stops_reading_once_it_has_enough(self, trace_db):
        rows = [("run-1", "s", "step", "evt", _failure(i)[0]) for i in range(2)]
        rows += [("run-1", "s", "step", "evt", NOISE[i % len(NOISE)])
                 for i in range(400)]
        sf = trace_db(rows)
        res = trace_list(sf, "run-1", errors_only=True, limit=2, order="asc")
        assert [e["seq"] for e in res["entries"]] == [1, 2]
        assert sf.trace_query.call_count <= 2       # at most a boundary probe + one page

    def test_every_query_is_bounded_and_none_means_none(self, trace_db):
        sf = trace_db([("run-1", "s", "step", "evt", NOISE[i % len(NOISE)])
                       for i in range(450)])
        res = trace_list(sf, "run-1", errors_only=True, limit=5)
        assert res["entries"] == [] and res["count"] == 0
        assert max(c.args[2][-1] for c in sf.trace_query.call_args_list) <= 200

    @pytest.mark.parametrize("asked, got", [(500, 200), (0, 50), (-3, 1)])
    def test_the_requested_limit_still_caps_the_result(self, trace_db, asked, got):
        sf = trace_db([("run-1", "s", "step", "evt", {"error": f"failure {i}"})
                       for i in range(250)])
        res = trace_list(sf, "run-1", errors_only=True, limit=asked, order="asc")
        assert [e["seq"] for e in res["entries"]] == list(range(1, got + 1))

    def test_a_status_failed_payload_is_listed(self, trace_db):
        sf = trace_db([
            ("run-1", "s", "step", "evt", {"status": "failed", "detail": "step 4 crashed"}),
            ("run-1", "s", "step", "evt", {"status": "passed", "detail": "all fine"})])
        res = trace_list(sf, "run-1", errors_only=True)
        assert [e["summary"] for e in res["entries"]] == ["step 4 crashed"]

    def test_a_non_object_payload_is_neither_a_failure_nor_fatal(self, trace_db):
        rows = [("run-1", "s", "step", "evt", p)
                for p in ('["error", false]', '"failed error false"')]
        rows.append(("run-1", "s", "step", "evt", {"error": "real failure"}))
        res = trace_list(trace_db(rows), "run-1", errors_only=True)
        assert [e["summary"] for e in res["entries"]] == ["real failure"]

    def test_listing_leaves_the_raw_trace_untouched(self, agent, trace_db):
        items = _scenario("interleaved", 6, 40)
        sf = trace_db([("run-1", "s", "step", "evt", p) for p, _ in items])
        dump = "SELECT * FROM skillflow_trace ORDER BY id"
        before = [tuple(r) for r in sf.conn.execute(dump)]
        res = agent._tool_trace_list({"run": "run-1", "errors_only": True, "limit": 200})
        assert [tuple(r) for r in sf.conn.execute(dump)] == before
        assert res["count"] == 6
        padded = next(e for e in res["entries"] if e["summary"].startswith("padded"))
        raw = agent._tool_trace_read({"run": "run-1", "seq": padded["seq"]})
        assert raw["entries"][0]["payload"] == items[padded["seq"] - 1][0]
        assert raw["entries"][0]["payload"]["error"] == "  padded failure 3\n"

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_json_spelling_does_not_change_the_classification(self, trace_db, order):
        spelled = [   # (plain payload, same payload with JSON \u escapes, summary)
            ({"error": "boom"}, r'{"\u0065rror": "boom"}', "boom"),
            ({"passed": False}, r'{"p\u0061ssed": false}', "passed=false"),
            ({"status": "failed", "detail": "x crashed"},
             r'{"status": "fail\u0065d", "detail": "x crashed"}', "x crashed"),
            ({"error": "\u00a0\u2003"}, r'{"\u0065rror": "\u00a0\u2003"}', None),
            ({"passed": 0, "note": "false"},
             r'{"p\u0061ssed": 0, "note": "fals\u0065"}', None),
            ({"passed": "false"}, r'{"p\u0061ssed": "fals\u0065"}', None),
        ]
        rows = []
        for run, col in (("run-1", 0), ("run-2", 1)):
            for item in spelled:
                rows += [(run, "s", "step", "evt", NOISE[i % len(NOISE)])
                         for i in range(120)]
                rows.append((run, "s", "step", "evt", item[col]))
        want = [item[2] for item in spelled if item[2]]
        if order == "desc":
            want.reverse()
        sf = trace_db(rows, runs=("run-1", "run-2"))
        for run in ("run-1", "run-2"):
            res = trace_list(sf, run, errors_only=True, order=order)
            assert [e["summary"] for e in res["entries"]] == want

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_rows_appended_during_the_scan_are_not_chased(self, trace_db, order):
        rows = [("run-1", "s", "step", "evt", NOISE[i % len(NOISE)]) for i in range(230)]
        rows.append(("run-1", "s", "step", "evt", {"error": "failure before the request"}))
        sf = trace_db(rows)

        def append(n):
            if n <= 10:                                  # finite: ten rounds of appends
                sf.add("run-1", "s", "step", "evt", {"error": f"appended during scan {n}"})
                for i in range(120):
                    sf.add("run-1", "s", "step", "evt", NOISE[i % len(NOISE)])

        sf.after_query = append
        res = trace_list(sf, "run-1", errors_only=True, limit=50, order=order)
        assert [e["summary"] for e in res["entries"]] == ["failure before the request"]
        assert sf.trace_query.call_count <= 6

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_a_project_alias_is_pinned_for_the_whole_scan(self, trace_db, order):
        rows = [("run-1", "s", "step", "evt", NOISE[i % len(NOISE)]) for i in range(250)]
        rows.append(("run-1", "s", "step", "evt", {"error": "run-1 failure"}))
        rows.append(("run-2", "s", "step", "evt", {"error": "run-2 failure"}))
        sf = trace_db(rows, runs=("run-1", "run-2"))
        sf.newest_first["proj-x"] = ["run-1", "run-2"]

        def switch(n):
            if n == 1:                                   # a newer run appears mid-scan
                sf.newest_first["proj-x"] = ["run-2", "run-1"]

        sf.after_query = switch
        res = trace_list(sf, "proj-x", errors_only=True, order=order)
        assert [e["summary"] for e in res["entries"]] == ["run-1 failure"]
        assert res["run_id"] == "run-1"
        assert res["resolved"]["run_id"] == "run-1"
        assert [o["run_id"] for o in res["resolved"]["other_runs"]] == ["run-2"]
        assert sf.trace_query.call_count > 1


class TestToolRegistry:
    @pytest.fixture
    def loader(self, monkeypatch, tmp_path):
        schemas = {
            "web_search": {"description": "Search the web via SearXNG",
                           "parameters": {"query": {}}},
            "md_link_check": {"description": "Check markdown links resolve",
                              "parameters": {"path": {}}, "x-generated-by": "gen_mdlink"},
        }
        ld = MagicMock()
        ld.list_tools.return_value = list(schemas)
        ld.load_schema.side_effect = lambda n: schemas[n]
        ld._find_tool_dir.side_effect = lambda n: tmp_path
        sf = MagicMock()
        sf._tool_loader = ld
        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        return ld

    def test_list_reports_params_and_provenance(self, agent, loader):
        res = agent._tool_tool_list({})
        by_name = {t["name"]: t for t in res["tools"]}
        assert by_name["web_search"]["params"] == ["query"]
        assert by_name["md_link_check"]["generated_by"] == "gen_mdlink"

    def test_list_filters_by_name(self, agent, loader):
        res = agent._tool_tool_list({"filter": "link"})
        assert [t["name"] for t in res["tools"]] == ["md_link_check"]

    def test_search_matches_on_purpose_not_just_name(self, agent, loader):
        """'does something already do this?' must be answerable by description."""
        res = agent._tool_tool_search({"query": "check that markdown links resolve"})
        assert res["tools"][0]["name"] == "md_link_check"

    def test_search_requires_a_query(self, agent, loader):
        assert "error" in agent._tool_tool_search({})

    def test_read_rejects_an_unregistered_tool(self, agent, loader):
        res = agent._tool_tool_read({"name": "not_a_tool"})
        assert "not registered" in res["error"]

    def test_read_rejects_an_arbitrary_file(self, agent, loader):
        res = agent._tool_tool_read({"name": "web_search", "file": "../../etc/passwd"})
        assert "error" in res


class TestConfigSource:
    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text(
            yaml.safe_dump({"name": "gen_demo", "begin": "a",
                            "steps": [{"id": "a", "step_type": "gate"}]}), encoding="utf-8")
        (d / "gen_demo.roles.json").write_text(
            json.dumps({"gen_demo__maker": {"system_prompt": "be good"}}), encoding="utf-8")
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        return d

    def test_reads_a_generated_graph(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo"})
        assert "begin: a" in res["content"]

    def test_reads_the_role_table(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo", "file": "roles"})
        assert "be good" in res["content"]

    def test_reads_a_builtin_config(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "dpe_default"})
        assert "steps:" in res["content"]

    def test_rejects_a_path_in_the_name(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "../../etc/passwd"})
        assert "bare pipeline name" in res["error"]

    def test_missing_file_names_the_roles_alternative(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo",
                                       "file": "templates/nope.md"})
        assert "roles.json" in res["error"]

    def test_search_finds_which_config_uses_a_tool(self, agent, gen_dir):
        res = agent._tool_config_search({"query": "forge_registry_check"})
        assert any(h["config"] == "pipeline_forge" for h in res["hits"])

    def test_search_requires_a_query(self, agent, gen_dir):
        assert "error" in agent._tool_config_search({})


class TestConfigEdit:
    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text("name: gen_demo\nbegin: a\n", encoding="utf-8")
        import core.pipeline_registry as pr
        import api.dependencies as deps
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        monkeypatch.setattr(pr, "reload_generated_pipeline",
                            lambda sf, reg, name: {"config_name": name})
        monkeypatch.setattr(deps, "get_skillflow", lambda: MagicMock())
        monkeypatch.setattr(deps, "get_config_registry", lambda: MagicMock())
        return d

    def test_edits_and_reloads(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "begin: a", "new_str": "begin: b"})
        assert res["edited"] and res["reloaded"]
        assert "begin: b" in (gen_dir / "gen_demo.yaml").read_text()

    def test_refuses_an_ambiguous_match(self, agent, gen_dir):
        (gen_dir / "gen_demo.yaml").write_text("x: 1\nx: 1\n", encoding="utf-8")
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "x: 1", "new_str": "x: 2"})
        assert "appears 2 times" in res["error"]

    def test_refuses_a_missing_match(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "nope", "new_str": "y"})
        assert "not found" in res["error"]

    def test_refuses_to_edit_a_builtin_config(self, agent, gen_dir):
        """Repo configs are code — a chat session does not rewrite them in place."""
        res = agent._tool_config_edit({"config_name": "dpe_default",
                                       "old_str": "steps:", "new_str": "steps:"})
        assert "built-in config" in res["error"]

    def test_a_broken_edit_warns_that_the_registry_is_stale(self, agent, gen_dir, monkeypatch):
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "reload_generated_pipeline",
                            lambda sf, reg, name: {"error": "invalid graph"})
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "begin: a", "new_str": "begin: !!"})
        assert res["reload_error"] == "invalid graph"
        assert "previous version" in res["warning"]


class TestConfigEditMessages:
    """A refusal has to say the true reason — the agent acts on what it is told."""

    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text("name: gen_demo\n", encoding="utf-8")
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        return d

    def test_a_template_of_a_generated_pipeline_is_not_called_built_in(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "file": "templates/forge_explain.md",
                                       "old_str": "x", "new_str": "y"})
        assert "built-in config" not in res["error"]
        assert "roles.json" in res["error"]        # points at the real edit surface

    def test_a_real_builtin_still_says_so(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "dpe_default",
                                       "old_str": "steps:", "new_str": "steps:"})
        assert "built-in config" in res["error"]
