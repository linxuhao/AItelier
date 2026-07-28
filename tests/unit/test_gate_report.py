"""The forge gates must leave their verdict somewhere the maker can read.

`feedback: true` on a tool-gate edge writes `_feedback` into a row
`WHERE status = 'pending'`. On a backward loop-back the maker's next instance does
not exist yet, so the write hits nothing and the maker re-runs blind. Measured on
`forge-mcp-server-builder-95991a`: 4 `emit_graph` step instances, not one carrying
`_feedback`, and 3 identical smoke failures. The file is the channel that works.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from aitelier.gate_report import GATE_REPORT_FILE, write_gate_report

_ROOT = Path(__file__).resolve().parents[2]


def _load(tool: str):
    p = _ROOT / "aitelier" / "tools" / tool / "impl.py"
    spec = importlib.util.spec_from_file_location(f"{tool}_impl", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, tool)


class TestWriteGateReport:
    def test_a_failure_lands_in_the_step_dir(self, tmp_path):
        write_gate_report(str(tmp_path / "v_smoke"), "forge_dryrun_smoke", False,
                          "No matching transition from 'pytest_run'")
        f = tmp_path / "v_smoke" / GATE_REPORT_FILE
        assert f.exists()
        text = f.read_text(encoding="utf-8")
        assert "forge_dryrun_smoke" in text
        assert "No matching transition from 'pytest_run'" in text

    def test_a_pass_keeps_the_earlier_findings_visible(self, tmp_path):
        """The whole point of V2.

        Deleting on pass made the maker forget the constraint it had just satisfied:
        `forge-mcp-server-builder-a063e2` emit 2 fixed the registry findings, emit 3
        fixed the smoke and re-introduced the registry ones, because `v_registry`'s
        report had been deleted the moment it passed.
        """
        d = str(tmp_path / "v_lint")
        write_gate_report(d, "forge_lint", False, "unbounded cycle in fix_loop")
        write_gate_report(d, "forge_lint", True, "")
        text = (tmp_path / "v_lint" / GATE_REPORT_FILE).read_text(encoding="utf-8")
        assert "unbounded cycle in fix_loop" in text, "the fixed finding vanished"
        assert "PASSED" in text
        assert "keep it that way" in text

    def test_rounds_are_numbered_in_order(self, tmp_path):
        d = str(tmp_path / "v_registry")
        write_gate_report(d, "forge_registry_check", False, "first defect")
        write_gate_report(d, "forge_registry_check", False, "second defect")
        text = (tmp_path / "v_registry" / GATE_REPORT_FILE).read_text(encoding="utf-8")
        assert text.index("## round 1") < text.index("## round 2")
        assert text.index("first defect") < text.index("second defect")

    def test_a_pass_on_a_clean_dir_still_records_the_round(self, tmp_path):
        write_gate_report(str(tmp_path / "v_lint"), "forge_lint", True, "")
        text = (tmp_path / "v_lint" / GATE_REPORT_FILE).read_text(encoding="utf-8")
        assert "## round 1" in text and "PASSED" in text

    def test_the_log_is_bounded(self, tmp_path):
        """A long convergence must not crowd out the emitter's real context."""
        d = str(tmp_path / "v_smoke")
        for i in range(60):
            write_gate_report(d, "forge_dryrun_smoke", False, f"defect {i} " + "x" * 800)
        text = (tmp_path / "v_smoke" / GATE_REPORT_FILE).read_text(encoding="utf-8")
        assert len(text) <= 16000 + 200
        assert "defect 59" in text, "the newest round must survive trimming"
        assert "earlier rounds dropped" in text

    def test_no_out_dir_is_a_no_op(self, tmp_path):
        write_gate_report("", "forge_lint", False, "boom")   # must not raise
        assert list(tmp_path.iterdir()) == []

    def test_an_unwritable_out_dir_never_breaks_the_gate(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file, not a directory", encoding="utf-8")
        write_gate_report(str(blocker), "forge_lint", False, "boom")   # must not raise

    def test_a_failure_with_no_detail_still_says_something(self, tmp_path):
        write_gate_report(str(tmp_path / "g"), "forge_lint", False, "")
        assert "no detail reported" in (tmp_path / "g" / GATE_REPORT_FILE).read_text()


class TestGatesWriteTheirReport:
    def test_registry_check_logs_both_the_failure_and_the_later_pass(self, tmp_path,
                                                                     monkeypatch):
        forge_registry_check = _load("forge_registry_check")
        import aitelier.tools.forge_registry_check as _pkg  # noqa: F401  (namespace only)

        good = {"name": "g", "description": "x", "begin": "work",
                "end_conditions": {"combinator": "or", "conditions": [
                    {"type": "node_reached", "node": "done", "result": "completed"}]},
                "steps": [
                    {"id": "work", "step_type": "agent", "agent_config": "w",
                     "output": {"mode": "content", "fixed": {"a": {"file": "out.md"}}},
                     "transitions": [{"to": "done"}]},
                    {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
                ]}
        bad = {**good, "steps": [dict(good["steps"][0]), {"id": "done",
               "step_type": "agent", "agent_config": "w", "transitions": [{"to": None}]}]}

        out = tmp_path / "v_registry"
        gp = tmp_path / "g.yaml"

        gp.write_text(yaml.safe_dump(bad), encoding="utf-8")
        res = forge_registry_check(graph_path=str(gp), out_dir=str(out))
        assert res["passed"] is False
        assert (out / GATE_REPORT_FILE).exists()

        first = (out / GATE_REPORT_FILE).read_text(encoding="utf-8")

        gp.write_text(yaml.safe_dump(good), encoding="utf-8")
        res = forge_registry_check(graph_path=str(gp), out_dir=str(out))
        assert res["passed"] is True
        after = (out / GATE_REPORT_FILE).read_text(encoding="utf-8")
        assert "## round 2" in after and "PASSED" in after
        # the round-1 finding is still on the record, so a later emit cannot
        # quietly re-introduce it
        assert "completed-terminal" in first and "completed-terminal" in after

    def test_a_missing_graph_still_reports(self, tmp_path):
        forge_registry_check = _load("forge_registry_check")
        out = tmp_path / "v_registry"
        forge_registry_check(graph_path=str(tmp_path / "nope.yaml"), out_dir=str(out))
        assert "not found" in (out / GATE_REPORT_FILE).read_text()

    def test_smoke_writes_the_reason_it_now_carries(self, tmp_path):
        forge_dryrun_smoke = _load("forge_dryrun_smoke")
        from tests.unit.test_forge_dryrun_smoke import DEAD_END
        gp = tmp_path / "g.yaml"
        gp.write_text(yaml.safe_dump(DEAD_END), encoding="utf-8")
        out = tmp_path / "v_smoke"
        res = forge_dryrun_smoke(graph_path=str(gp), out_dir=str(out))
        if res.get("status") in ("import_error", "boot_error"):
            pytest.skip(f"engine unavailable: {res.get('error')}")
        assert "No matching transition" in (out / GATE_REPORT_FILE).read_text()


def test_the_forge_config_wires_every_gate_to_its_step_dir():
    """A gate that doesn't get `out_dir` writes nothing and the loop stays blind."""
    cfg = yaml.safe_load((_ROOT / "configs" / "pipeline_forge.yaml")
                         .read_text(encoding="utf-8"))
    gates = {s["id"]: s for s in cfg["steps"]
             if s.get("step_type") == "tool" and str(s.get("id")).startswith("v_")}
    assert set(gates) == {"v_lint", "v_registry", "v_smoke"}
    for gid, step in gates.items():
        assert (step.get("tool_params") or {}).get("out_dir") == "$STEP_DIR", gid


def test_the_emitter_reads_every_gate_report():
    cfg = yaml.safe_load((_ROOT / "configs" / "pipeline_forge.yaml")
                         .read_text(encoding="utf-8"))
    emit = next(s for s in cfg["steps"] if s["id"] == "emit_graph")
    read = {(c["source"].get("step"), c["source"].get("file"))
            for c in emit["context"] if isinstance(c.get("source"), dict)}
    for gate in ("v_lint", "v_registry", "v_smoke"):
        assert (gate, GATE_REPORT_FILE) in read, f"emit_graph cannot see {gate}'s verdict"
