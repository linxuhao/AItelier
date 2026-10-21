"""Per-pass compile evidence through the NORMAL path.

Real pieces: the harness HTTP handler, ``compile_project``, ``_run`` (a real
subprocess, process group and timeout kill), the ``godot_compile`` tool speaking
real HTTP to that handler, and the gate reader ``release_disposition``.
The only substitute is the engine binary: ``GODOT_BIN`` points at a tiny shell
script whose output/exit code per pass is set by the test. So these prove the
transport and the verdict policy; they make NO claim about what real Godot
prints (that is the separate native control, run in the official image).
"""
import functools
import json
import os
import stat
import subprocess
import threading
import urllib.request
from contextlib import contextmanager

import pytest

from aitelier.gate_evidence import release_disposition
from aitelier.tools.godot_compile import impl as tool
from docker.godot import godot_harness as gh

COLD = "SCRIPT ERROR: Parse Error: cold importer transient\n"
FAULT = ("SCRIPT ERROR: Parse Error: Identifier \"nope\" not declared\n"
         "          at: GDScript::reload (res://main.gd:2)\n")
PHASES = ["cold_import", "warm_import", "explicit_reparse"]

_ENGINE = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *--script*) p=reparse ;;
  *) n=$(cat "$FAKE_STATE" 2>/dev/null || echo 0); n=$((n+1))
     echo "$n" > "$FAKE_STATE"; p=import$n ;;
esac
eval "out=\\${OUT_$p-}; err=\\${ERR_$p-}; rc=\\${RC_$p-0}; hang=\\${HANG_$p-0}"
printf '%s' "$out"
printf '%s' "$err" >&2
[ "$hang" = 1 ] && sleep 60
[ "$rc" = kill ] && kill -9 $$
exit "$rc"
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Install the scripted engine; returns a setter and the invocation log."""
    script = tmp_path / "fake-godot"
    script.write_text(_ENGINE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "engine.log"
    monkeypatch.setattr(gh, "GODOT_BIN", str(script))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "engine.state"))
    monkeypatch.setenv("OUT_reparse", "PARSE_ALL_LOADED=1/1\n")

    def script_phases(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return script_phases, calls


@contextmanager
def server():
    httpd = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def project(tmp_path, name="project"):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "project.godot").write_text("config_version=5\n")
    (repo / "main.gd").write_text("extends Node\n")
    return repo


def post(url, repo, headers=None, **identity):
    req = urllib.request.Request(
        url + "/compile", data=json.dumps({"project_dir": str(repo), **identity}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def staged_dirs(calls):
    """Temporary project copies the engine was pointed at."""
    return {line.split("--path ")[1].split()[0] for line in calls}


def by_phase(report):
    return {p["phase"]: p for p in report["compile_diagnostics"]["phases"]}


def test_cold_diagnostic_survives_normal_http_and_temporary_cleanup(tmp_path, engine):
    script, calls = engine
    script(OUT_import1="cold stdout\n", ERR_import1=COLD, RC_import1="1",
           OUT_import2="warm stdout\n")
    repo = project(tmp_path)
    with server() as url:
        report = post(url, repo, project_id="p", run_id="r", operation_id="op")
    assert report["passed"] is True  # a cold importer diagnostic cannot gate
    assert len(calls()) == 3
    assert all(not os.path.exists(d) for d in staged_dirs(calls()))
    evidence = report["compile_diagnostics"]
    assert evidence["project_dir"] == str(repo.resolve())
    assert (evidence["project_id"], evidence["run_id"], evidence["operation_id"]) == ("p", "r", "op")
    assert len(evidence["invocation_id"]) == 32
    phases = evidence["phases"]
    assert [p["phase"] for p in phases] == PHASES
    assert phases[0]["stderr"] == COLD and phases[0]["stdout"] == "cold stdout\n"
    assert phases[1]["stdout"] == "warm stdout\n" and phases[1]["stderr"] == ""
    assert phases[2]["stdout"] == "PARSE_ALL_LOADED=1/1\n"
    assert [p["returncode"] for p in phases] == [1, 0, 0]
    assert all(p["outcome"] == "exited" and p["output_complete"] for p in phases)
    assert release_disposition(report) == "passed"


def test_operation_header_is_the_correlation_when_the_body_has_none(tmp_path, engine):
    with server() as url:
        report = post(url, project(tmp_path), headers={"X-AItelier-Operation": "hdr-op"})
    evidence = report["compile_diagnostics"]
    assert evidence["operation_id"] == "hdr-op"
    assert evidence["project_id"] is None and evidence["run_id"] is None


def test_each_invocation_carries_only_its_own_evidence(tmp_path, engine):
    script, _ = engine
    repo = project(tmp_path)
    script(ERR_import1=COLD, OUT_import1="first call\n")
    with server() as url:
        first = post(url, repo, operation_id="one")
        os.unlink(os.environ["FAKE_STATE"])
        script(ERR_import1="", OUT_import1="second call\n")
        second = post(url, repo, operation_id="two")
    assert first["compile_diagnostics"]["invocation_id"] != second["compile_diagnostics"]["invocation_id"]
    assert by_phase(first)["cold_import"]["stderr"] == COLD
    assert by_phase(second)["cold_import"]["stderr"] == ""
    assert by_phase(second)["cold_import"]["stdout"] == "second call\n"


def test_real_parse_fault_in_explicit_reparse_still_fails(tmp_path, engine):
    script, _ = engine
    script(ERR_reparse=FAULT, OUT_reparse="PARSE_ALL_LOADED=0/1\n")
    with server() as url:
        report = post(url, project(tmp_path), operation_id="op")
    assert report["passed"] is False
    assert [e["kind"] for e in report["errors"]] == ["parse"]
    assert by_phase(report)["explicit_reparse"]["stderr"] == FAULT
    assert release_disposition(report) == "known_failure"


def test_real_parse_fault_in_authoritative_import_still_fails(tmp_path, engine):
    script, _ = engine
    script(ERR_import1=FAULT, ERR_import2=FAULT, RC_import2="1")
    with server() as url:
        report = post(url, project(tmp_path), operation_id="op")
    assert report["passed"] is False
    assert by_phase(report)["warm_import"]["stderr"] == FAULT
    assert by_phase(report)["warm_import"]["returncode"] == 1
    assert report["returncode"] == 1  # unchanged: the warm import's own code


def test_cold_only_fault_and_warm_clean_is_still_a_pass(tmp_path, engine):
    script, _ = engine
    script(ERR_import1=FAULT, RC_import1="1")
    with server() as url:
        report = post(url, project(tmp_path))
    assert report["passed"] is True and report["errors"] == []
    assert by_phase(report)["cold_import"]["stderr"] == FAULT


def test_cold_timeout_keeps_partial_output_and_marks_later_phases_not_run(
        tmp_path, engine, monkeypatch):
    script, calls = engine
    script(OUT_import1="cold partial\n", ERR_import1="cold partial err\n", HANG_import1="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path), operation_id="op")
    assert report["passed"] is False and report["errors"][0]["kind"] == "timeout"
    phases = by_phase(report)
    assert phases["cold_import"]["outcome"] == "timeout"
    assert phases["cold_import"]["stdout"] == "cold partial\n"
    assert phases["cold_import"]["stderr"] == "cold partial err\n"
    assert phases["cold_import"]["output_complete"] is False
    assert phases["cold_import"]["returncode"] is None
    for name in ("warm_import", "explicit_reparse"):
        assert phases[name] == {"phase": name, "outcome": "not_run", "returncode": None,
                                "stdout": None, "stderr": None, "output_complete": False}
    assert len(calls()) == 1
    assert all(not os.path.exists(d) for d in staged_dirs(calls()))


def test_warm_timeout_keeps_the_completed_cold_pass(tmp_path, engine, monkeypatch):
    script, _ = engine
    script(ERR_import1=COLD, RC_import1="1", OUT_import2="warm partial\n", HANG_import2="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path))
    phases = by_phase(report)
    assert report["passed"] is False
    assert (phases["cold_import"]["outcome"], phases["cold_import"]["stderr"]) == ("exited", COLD)
    assert (phases["warm_import"]["outcome"], phases["warm_import"]["stdout"]) == ("timeout", "warm partial\n")
    assert phases["explicit_reparse"]["outcome"] == "not_run"


def test_reparse_timeout_is_explicit_beside_completed_imports(tmp_path, engine, monkeypatch):
    script, _ = engine
    script(OUT_reparse="reparse partial\n", HANG_reparse="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path))
    phases = by_phase(report)
    assert [phases[n]["outcome"] for n in PHASES] == ["exited", "exited", "timeout"]
    assert phases["explicit_reparse"]["stdout"] == "reparse partial\n"
    assert "TIMED OUT" in report["summary"]


def test_engine_killed_by_signal_is_not_reported_as_a_clean_exit(tmp_path, engine):
    script, _ = engine
    script(RC_import1="kill", ERR_import1="crash trace\n")
    with server() as url:
        report = post(url, project(tmp_path))
    cold = by_phase(report)["cold_import"]
    assert (cold["outcome"], cold["returncode"], cold["stderr"]) == ("signaled", -9, "crash trace\n")


def test_no_project_and_boundary_refusals_run_nothing_and_say_so(tmp_path, engine):
    _, calls = engine
    empty = tmp_path / "empty"
    empty.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (tmp_path / "real.godot").write_text("config_version=5\n")
    (linked / "project.godot").symlink_to(tmp_path / "real.godot")
    with server() as url:
        none = post(url, empty)
        boundary = post(url, linked)
    assert none["no_project"] is True and none["passed"] is True
    assert boundary["passed"] is False and boundary["errors"][0]["kind"] == "boundary"
    for report in (none, boundary):
        assert [p["outcome"] for p in report["compile_diagnostics"]["phases"]] == ["not_run"] * 3
        assert all(p["stdout"] is None and p["stderr"] is None
                   for p in report["compile_diagnostics"]["phases"])
    assert calls() == []  # the engine was never started


def test_scope_names_the_unmeasured_entry_time_context_and_runs_no_entry_scripts(
        tmp_path, engine):
    _, calls = engine
    repo = project(tmp_path)
    (repo / "boot.gd").write_text("extends Node\nconst X = preload(\"res://main.gd\")\n")
    (repo / "project.godot").write_text(
        'config_version=5\n[autoload]\nBoot="*res://boot.gd"\n')
    with server() as url:
        report = post(url, repo)
    scope = report["compile_diagnostics"]["scope"]
    assert any("autoload" in item and "not full entry-time validation" in item
               for item in scope["unmeasured"])
    assert [("--script" in line, "--import" in line) for line in calls()] == [
        (False, True), (False, True), (True, False)]


# ── the tool and gate consumer, over real HTTP ──────────────────────────────


# ── incomplete AUTHORITATIVE execution is never a pass ──────────────────────
# The engine here is the scripted shell binary above (NOT real Godot): the
# subprocess, SIGKILL and timeout are real kernel events through the normal
# HTTP handler, compile_project and _run; only the engine's output is scripted.

def assert_incomplete_not_pass(report):
    assert report["passed"] is False
    assert report["execution_complete"] is False
    assert report["incomplete_reasons"]
    assert report["infrastructure_unavailable"] is True
    assert not report["summary"].startswith("GDScript parse OK")
    assert release_disposition(report) == "unresolved"


def test_warm_import_killed_by_sigkill_is_not_a_compile_pass(tmp_path, engine):
    script, _ = engine
    script(RC_import2="kill")
    with server() as url:
        report = post(url, project(tmp_path), operation_id="op")
    warm = by_phase(report)["warm_import"]
    assert (warm["outcome"], warm["returncode"]) == ("signaled", -9)
    assert_incomplete_not_pass(report)
    assert any("warm_import" in r and "signal 9" in r for r in report["incomplete_reasons"])


def test_explicit_reparse_timeout_is_not_a_compile_pass(tmp_path, engine, monkeypatch):
    script, _ = engine
    script(HANG_reparse="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path))
    assert by_phase(report)["explicit_reparse"]["outcome"] == "timeout"
    assert_incomplete_not_pass(report)


def test_warm_import_nonzero_without_parsed_errors_is_not_a_pass(tmp_path, engine):
    script, _ = engine
    script(RC_import2="3", ERR_import2="engine aborted mid-import\n")
    with server() as url:
        report = post(url, project(tmp_path))
    assert by_phase(report)["warm_import"]["returncode"] == 3
    assert_incomplete_not_pass(report)


def test_reparse_without_loaded_marker_is_not_a_pass(tmp_path, engine):
    script, _ = engine
    script(OUT_reparse="")
    with server() as url:
        report = post(url, project(tmp_path))
    assert_incomplete_not_pass(report)
    assert any("PARSE_ALL_LOADED" in r for r in report["incomplete_reasons"])


def test_warm_sigkill_does_not_hide_a_proven_parse_fault(tmp_path, engine):
    script, _ = engine
    script(RC_import2="kill", ERR_import2=FAULT)
    with server() as url:
        report = post(url, project(tmp_path))
    assert report["passed"] is False
    assert [e["kind"] for e in report["errors"]] == ["parse"]
    assert report["execution_complete"] is False
    assert "infrastructure_unavailable" not in report  # a real fault, not unmeasured
    assert release_disposition(report) == "known_failure"


def test_partial_reparse_parse_fault_then_timeout_stays_a_genuine_failure(
        tmp_path, engine, monkeypatch):
    # Real subprocess, real timeout kill: the reparse prints a valid ParseError
    # and then hangs. The proven fault must not be hidden behind "incomplete".
    script, _ = engine
    script(ERR_reparse=FAULT, OUT_reparse="partial\n", HANG_reparse="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path))
    reparse = by_phase(report)["explicit_reparse"]
    assert reparse["outcome"] == "timeout" and reparse["stderr"] == FAULT
    assert report["passed"] is False
    assert [e["kind"] for e in report["errors"]] == ["parse"]
    assert report["execution_complete"] is False
    assert any("TIMED OUT" in r for r in report["incomplete_reasons"])
    assert "infrastructure_unavailable" not in report
    assert release_disposition(report) == "known_failure"


def test_reparse_timeout_without_fault_stays_unmeasured_not_a_code_failure(
        tmp_path, engine, monkeypatch):
    script, _ = engine
    script(HANG_reparse="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        report = post(url, project(tmp_path))
    assert [e["kind"] for e in report["errors"]] == ["incomplete"]
    assert release_disposition(report) == "unresolved"


def test_old_producer_pass_without_phase_record_is_refused_by_the_tool(
        tmp_path, engine, monkeypatch):
    # An older builder answers over real HTTP with passed:true and NO phase
    # record while the engine was really SIGKILLed in the warm import.
    script, _ = engine
    script(RC_import2="kill")
    real = gh.compile_project

    def old_producer(proj, **kw):
        report = real(proj, **kw)
        # What a pre-phase-record builder reported for this run: a clean pass.
        report.pop("compile_diagnostics")
        for key in ("execution_complete", "incomplete_reasons",
                    "infrastructure_unavailable", "evidence_state"):
            report.pop(key, None)
        report.update(passed=True, errors=[], summary="GDScript parse OK (1 of 1).")
        return report
    monkeypatch.setattr(gh, "compile_project", old_producer)
    with server() as url:
        result, out = run_tool(tmp_path, url, monkeypatch, project(tmp_path), run_id="run")
    report = json.loads((out / "compile_report.json").read_text())
    assert result["passed"] is False
    assert report["passed"] is False
    assert report["execution_complete"] is False
    assert report["infrastructure_unavailable"] is True
    assert "no compile phase record" in report["summary"]
    assert release_disposition(report) == "unresolved"
    assert not (out / "compile_diagnostics.json").exists()


def test_tool_still_accepts_the_explicit_no_project_answer_without_phases():
    # Boundary distinction kept: a project-less repo is not an incomplete compile.
    payload = {"passed": True, "errors": [], "file_count": 0, "no_project": True}
    report = dict(payload)
    tool._refuse_incomplete_pass(report)
    assert report == payload


def test_complete_warm_and_reparse_with_cold_only_noise_still_pass(tmp_path, engine):
    script, _ = engine
    script(RC_import1="kill", ERR_import1=COLD)  # cold is diagnostic only
    with server() as url:
        report = post(url, project(tmp_path))
    assert report["passed"] is True
    assert "execution_complete" not in report
    assert by_phase(report)["cold_import"]["outcome"] == "signaled"
    assert release_disposition(report) == "passed"


def test_tool_refuses_a_builder_pass_that_its_own_phase_record_denies(tmp_path, monkeypatch):
    repo = project(tmp_path)
    phases = [{"phase": "cold_import", "outcome": "exited", "returncode": 0,
               "stdout": "", "stderr": "", "output_complete": True},
              {"phase": "warm_import", "outcome": "signaled", "returncode": -9,
               "stdout": "", "stderr": "", "output_complete": True},
              {"phase": "explicit_reparse", "outcome": "exited", "returncode": 0,
               "stdout": "PARSE_ALL_LOADED=1/1\n", "stderr": "", "output_complete": True}]
    payload = {"passed": True, "errors": [], "file_count": 1, "returncode": -9,
               "summary": "GDScript parse OK (1 of 1 scripts explicitly re-parsed).",
               "compile_diagnostics": {"invocation_id": "x", "phases": phases}}

    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())
    out = tmp_path / "out"
    result = tool.godot_compile(project_root=str(repo), out_dir=str(out))
    report = json.loads((out / "compile_report.json").read_text())
    assert result["passed"] is False
    assert_incomplete_not_pass(report)
    playtest = json.loads((out / "playtest_report.json").read_text())
    assert playtest["skipped_because"] == "upstream_failed"
    assert playtest["upstream_state"] == "infrastructure_unavailable"


def test_tool_end_to_end_sigkill_over_real_http_fails_the_compile_gate(
        tmp_path, engine, monkeypatch):
    script, _ = engine
    script(RC_import2="kill")
    with server() as url:
        result, out = run_tool(tmp_path, url, monkeypatch, project(tmp_path), run_id="run")
    report = json.loads((out / "compile_report.json").read_text())
    assert result["passed"] is False
    assert_incomplete_not_pass(report)
    assert report["compile_diagnostics"]["phases"][1]["outcome"] == "signaled"
    raw = json.loads((out / "compile_diagnostics.json").read_text())
    assert raw["phases"][1]["returncode"] == -9


def run_tool(tmp_path, url, monkeypatch, repo, **kwargs):
    monkeypatch.setattr(tool, "_BUILDER_URL", url)
    # The chained play-test is a different gate with its own transport; only the
    # compile hand-off is under test here.
    from aitelier.tools.godot_playtest import impl as playtest

    def fake_playtest(*, out_dir, **_):
        (open(os.path.join(out_dir, "playtest_report.json"), "w")
         .write(json.dumps({"passed": True, "frames": 1, "errors": [], "state": {}})))
        return {"passed": True}
    monkeypatch.setattr(playtest, "godot_playtest", fake_playtest)
    out = tmp_path / "out"
    result = tool.godot_compile(project_root=str(repo), out_dir=str(out), **kwargs)
    return result, out


def test_tool_keeps_raw_phases_beside_a_prompt_sized_report(tmp_path, engine, monkeypatch):
    script, _ = engine
    script(OUT_import1="cold stdout\n", ERR_import1=COLD, RC_import1="1")
    repo = project(tmp_path)
    with server() as url:
        result, out = run_tool(tmp_path, url, monkeypatch, repo,
                               project_id="proj", run_id="run", step_id="5_compile")
    assert result["passed"] is True
    raw = json.loads((out / "compile_diagnostics.json").read_text())
    report = json.loads((out / "compile_report.json").read_text())
    assert raw["phases"][0]["stderr"] == COLD and raw["phases"][0]["stdout"] == "cold stdout\n"
    index = report["compile_diagnostics"]
    assert index["evidence_file"] == "compile_diagnostics.json"
    assert index["invocation_id"] == raw["invocation_id"]
    assert index["request_ids_match"] is True
    assert raw["run_id"] == "run" and raw["project_id"] == "proj"
    assert raw["operation_id"].startswith("run:5_compile:")
    assert all("stdout" not in p and "stderr" not in p for p in index["phases"])
    assert index["phases"][0]["stderr_chars"] == len(COLD)
    assert index["phases"][2]["stderr_chars"] == 0
    assert [p["outcome"] for p in index["phases"]] == ["exited"] * 3
    assert release_disposition(report) == "passed"


def test_tool_without_identity_does_not_claim_an_identity_match(tmp_path, engine, monkeypatch):
    with server() as url:
        _, out = run_tool(tmp_path, url, monkeypatch, project(tmp_path))
    index = json.loads((out / "compile_report.json").read_text())["compile_diagnostics"]
    assert index["request_ids_match"] is None
    assert index["run_id"] is None and index["operation_id"] is None


def test_tool_reports_not_run_phases_as_null_not_empty(tmp_path, engine, monkeypatch):
    script, _ = engine
    script(HANG_import1="1")
    monkeypatch.setattr(gh, "compile_project", functools.partial(gh.compile_project, timeout=1))
    with server() as url:
        result, out = run_tool(tmp_path, url, monkeypatch, project(tmp_path), run_id="run")
    assert result["passed"] is False
    index = json.loads((out / "compile_report.json").read_text())["compile_diagnostics"]
    assert index["phases"][0]["outcome"] == "timeout"
    assert index["phases"][1]["stdout_chars"] is None and index["phases"][1]["stderr_chars"] is None
    raw = json.loads((out / "compile_diagnostics.json").read_text())
    assert raw["phases"][1]["stdout"] is None


def test_tool_flags_evidence_that_echoes_another_invocations_identity(tmp_path, monkeypatch):
    repo = project(tmp_path)
    payload = {"passed": False, "errors": [], "file_count": 1,
               "compile_diagnostics": {"invocation_id": "x", "run_id": "someone-else",
                                       "operation_id": "other", "phases": []}}

    class Response:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Response())
    out = tmp_path / "out"
    tool.godot_compile(project_root=str(repo), out_dir=str(out), run_id="run")
    index = json.loads((out / "compile_report.json").read_text())["compile_diagnostics"]
    assert index["request_ids_match"] is False


def test_stale_diagnostics_file_is_not_left_standing_for_a_run_without_evidence(
        tmp_path, monkeypatch):
    repo = project(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "compile_diagnostics.json").write_text('{"invocation_id": "stale"}')
    monkeypatch.setattr(tool, "_BUILDER_URL", "http://127.0.0.1:9")  # nothing listens
    result = tool.godot_compile(project_root=str(repo), out_dir=str(out))
    report = json.loads((out / "compile_report.json").read_text())
    assert result["passed"] is False and report["infrastructure_unavailable"] is True
    assert "compile_diagnostics" not in report
    assert not (out / "compile_diagnostics.json").exists()
