"""Real SkillFlow loader, fencing, journal and native host; only LLM is fake."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader

from core.dpe_pipeline import PipelineEngine
from core.write_scope import WriteScope
from tests.code_output_fixture import init_code_repo
from tests.unit.test_native_direct_code import execute, host, response


def patch(body):
    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


def fixture(tmp_path, monkeypatch, target="code", initial_text="x = 1\n"):
    root = init_code_repo(tmp_path / "code")
    (root / "existing.py").write_text(initial_text)
    (root / "obsolete").write_text("remove me\n")
    git(root, "add", "--", "existing.py", "obsolete")
    git(root, "commit", "-qm", "seed")
    loader = ToolLoader(
        Path(skillflow.__file__).parent / "tools",
        Path(__file__).resolve().parents[2] / "aitelier/tools",
    )
    sf = SkillFlow(
        str(tmp_path / "sf.db"),
        tool_loader=loader,
        workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda pid, run_id=None: root,
    )
    node = StepNode(
        id="implement",
        output_mode="write",
        output_target=target,
        config={"extra_tools": ["apply_patch"]},
        context=[{"from": "repository", "mode": "tool"}],
        transitions=[Transition(to=None)],
    )
    sf.register_graph(PipelineGraph(name="g", begin=node.id, steps=[node]))
    rid = sf.create_run("g", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    return sf, rid, claim, root


def test_native_patch_read_and_deliver_uses_real_code_journal(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    turns = []

    def turn(messages, tools, **kwargs):
        turns.append(messages)
        names = {t["function"]["name"] for t in tools}
        assert "apply_patch" in names and not names.intersection(
            {"create", "edit", "write", "repo_remove_file"}
        )
        if len(turns) == 1:
            return response(
                "apply_patch",
                patch=patch(
                    "*** Update File: existing.py\n@@\n-x = 1\n+x = 2\n*** Add File: new.py\n+new = True\n*** Delete File: obsolete"
                ),
            )
        if len(turns) == 2:
            assert (root / "existing.py").read_text() == "x = 2\n" and not (
                root / "obsolete"
            ).exists()
            assert set(sf._code_output(rid, claim.step_id).load()["paths"]) == {
                "existing.py",
                "new.py",
                "obsolete",
            }
            return response("read", path="existing.py")
        assert "x = 2" in messages[-1]["content"]
        return response("finish_step", summary="patched and read current code")

    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert (
        sf.get_steps(rid)[0]["status"] == "completed"
        and git(root, "status", "--porcelain").strip() == ""
    )
    assert not list((tmp_path / "artifacts").rglob("implement.tmp"))
    receipts = list((tmp_path / "artifacts").rglob("code_changes.json"))
    assert receipts and "obsolete" in receipts[0].read_text()


def scope_host(sf, rid, claim, root, tmp_path):
    e = PipelineEngine.__new__(PipelineEngine)
    e._write_scope = WriteScope("task", ("allowed/",), ("shared.txt",))
    e._scope_violations = []
    e._output_fixed = {}
    e._output_target = "code"
    e._tool_schemas = claim.inputs["_tool_schemas"]
    e._artifact_dir = str(tmp_path / "scope-receipt")
    e._code_path = str(root)
    e._run_id = rid
    e._current_step = claim.step_id
    e._write_scope_step_id = claim.step_id
    e._step_instance_id = claim.token.step_instance_id
    e._claim_epoch = claim.token.claim_epoch
    e._emit = lambda *a, **kw: None
    e._trace = lambda *a, **kw: None
    return e


def test_batch_scope_refusal_precedes_any_mutation_and_persists(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    e = scope_host(sf, rid, claim, root, tmp_path)
    got = e._exec_tool(
        {
            "tool": "apply_patch",
            "params": {
                "patch": patch(
                    "*** Add File: allowed/new.py\n+x\n*** Delete File: obsolete"
                )
            },
        }
    )
    assert got["scope_violation"] and got["requested_path"] == "obsolete"
    assert (
        not (root / "allowed").exists()
        and (root / "obsolete").read_text() == "remove me\n"
    )
    receipt = json.loads(
        (tmp_path / "scope-receipt/write_scope_receipt.json").read_text()
    )
    assert receipt["violations"][0]["tool"] == "apply_patch"
    e._claim_epoch += 1
    e._load_write_scope_receipt()
    assert len(e._scope_violations) == 1


def test_shared_hotspot_and_root_injection_use_host_authority(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    e = scope_host(sf, rid, claim, root, tmp_path)
    got = e._exec_tool(
        {
            "tool": "apply_patch",
            "params": {
                "patch": patch(
                    "*** Add File: allowed/a\n+x\n*** Add File: shared.txt\n+y"
                ),
                "project_root": str(tmp_path),
                "output_dir": str(tmp_path),
                "output_target": "artifact",
            },
        }
    )
    assert got["applied"] and (root / "shared.txt").read_text() == "y\n"
    assert not (tmp_path / "shared.txt").exists()


def test_stale_claim_is_fenced_before_patch_execution(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    got = sf.execute_tool(
        "apply_patch",
        {"patch": patch("*** Add File: forbidden\n+x")},
        run_id=rid,
        step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id,
        claim_epoch=claim.token.claim_epoch + 100,
    )
    assert got["error"] and not (root / "forbidden").exists()


def test_artifact_step_keeps_its_editor_and_cannot_patch_code(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch, target="artifact")
    got = sf.execute_tool(
        "apply_patch",
        {"patch": patch("*** Add File: forbidden\n+x")},
        run_id=rid,
        step_id=claim.step_id,
    )
    assert got["error"] and not (root / "forbidden").exists()
    result = sf.execute_tool(
        "create",
        {"file": "report.md", "content": "artifact report"},
        run_id=rid,
        step_id=claim.step_id,
    )
    assert not result.get("error")
    assert not (root / "report.md").exists()
    assert (
        Path(claim.inputs["_output_dir"]) / "report.md"
    ).read_text() == "artifact report"


def test_delete_only_patch_is_output_and_can_deliver(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    calls = []

    def turn(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            return response("apply_patch", patch=patch("*** Delete File: obsolete"))
        return response("finish_step", summary="removed obsolete file")

    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert (
        not (root / "obsolete").exists()
        and git(root, "status", "--porcelain").strip() == ""
    )


def test_partial_deletion_is_a_real_effect_even_with_an_error():
    got = {"error": "second deletion failed", "deleted": ["first"], "partial": True}
    assert PipelineEngine._effect_name(got) == "deleted: ['first']"


def test_patch_role_rejects_old_mutator_and_missing_patch_argument(
    tmp_path, monkeypatch
):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    e = scope_host(sf, rid, claim, root, tmp_path)
    assert e._exec_tool(
        {"tool": "create", "params": {"file": "allowed/x", "content": "x"}}
    )["error"]
    assert e._exec_tool({"tool": "apply_patch", "params": {}})["error"]
    assert not (root / "allowed").exists()


def json_host(sf, rid, claim, root, run):
    e, ws = host(sf, rid, claim, root, lambda **kw: None)
    e.factory.is_native = lambda _: False
    e.factory.get_agent = lambda _: SimpleNamespace(
        run=run, gateway=SimpleNamespace(litellm_model="fake")
    )
    return e, ws


def test_json_mode_accounts_for_all_paths_and_deletion(tmp_path, monkeypatch):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)

    def run(prompt):
        return json.dumps(
            {
                "actions": [
                    {
                        "tool": "apply_patch",
                        "params": {
                            "patch": patch(
                                "*** Add File: first\n+one\n*** Add File: second\n+two\n*** Delete File: obsolete"
                            )
                        },
                    }
                ]
            }
        )

    e, ws = json_host(sf, rid, claim, root, run)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert not (root / "obsolete").exists() and (root / "first").read_text() == "one\n"
    assert (root / "second").read_text() == "two\n" and git(
        root, "status", "--porcelain"
    ).strip() == ""


def test_json_partial_failure_is_repaired_without_replaying_first_file(
    tmp_path, monkeypatch
):
    from skillflow import write_tools

    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    calls = []
    original = write_tools._write_output_text

    def fail_once(path, *args, **kwargs):
        if path.name == "second" and len(calls) == 1:
            raise OSError("injected second-file failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(write_tools, "_write_output_text", fail_once)

    def run(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            body = "*** Add File: first\n+one\n*** Add File: second\n+two"
        else:
            assert "partial" in prompt and "first" in prompt
            body = "*** Add File: second\n+two"
        return json.dumps(
            {"actions": [{"tool": "apply_patch", "params": {"patch": patch(body)}}]}
        )

    e, ws = json_host(sf, rid, claim, root, run)
    assert execute(e, ws, rid, claim) and len(calls) == 2
    sf.confirm_step(claim.token, StepResult())
    assert (root / "first").read_text() == "one\n" and (
        root / "second"
    ).read_text() == "two\n"


def test_code_files_shortcut_cannot_bypass_patch_frontend(tmp_path, monkeypatch):
    from core.dpe_pipeline import MaxRetriesExceeded

    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    e = scope_host(sf, rid, claim, root, tmp_path)
    with pytest.raises(MaxRetriesExceeded, match="apply_patch"):
        e._write_output_file(
            SimpleNamespace(), "p", claim.step_id, "existing.py", "overwrite"
        )
    assert (root / "existing.py").read_text() == "x = 1\n"


def test_native_patch_invalidates_a_cached_large_read(tmp_path, monkeypatch):
    initial = "x = 1\n" + ("# unchanged padding for the read observation cache\n" * 180)
    sf, rid, claim, root = fixture(tmp_path, monkeypatch, initial_text=initial)
    calls = []

    def turn(messages, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return response("read", path="existing.py")
        if len(calls) == 2:
            assert "x = 1" in messages[-1]["content"]
            return response(
                "apply_patch",
                patch=patch("*** Update File: existing.py\n@@\n-x = 1\n+x = 2"),
            )
        if len(calls) == 3:
            return response("read", path="existing.py")
        assert "x = 2" in messages[-1]["content"]
        assert "x = 1" not in messages[-1]["content"]
        return response(
            "finish_step", summary="read the changed file, not cached content"
        )

    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim) and len(calls) == 4


@pytest.mark.parametrize(
    "target,fixed", [("artifact", {}), ("code", {"report": "report.md"})]
)
def test_host_never_exposes_patch_to_artifact_or_fixed_slot_steps(
    tmp_path, monkeypatch, target, fixed
):
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    e, ws = host(sf, rid, claim, root, lambda **kwargs: None)

    def capture(*args, **kwargs):
        assert "apply_patch" not in e._tool_schemas
        assert "create" in e._tool_schemas
        return True

    e._run_native_step = capture
    assert e.run_step(
        1,
        claim.step_id,
        ws,
        "p",
        agent_config_name="fake",
        run_id=rid,
        tool_schemas=claim.inputs["_tool_schemas"],
        output_target=target,
        output_fixed=fixed,
        config_name="g",
    )


def _cite(sf, rid, claim, path, **kw):
    served = sf.execute_tool(
        "read", {"path": path, **kw}, run_id=rid, step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id,
        claim_epoch=claim.token.claim_epoch)
    assert "error" not in served, served
    return served["citation"]


def test_reference_mode_is_accepted_and_scope_checked_by_the_host(
    tmp_path, monkeypatch
):
    """The host used to accept exactly one argument and derive the authorised
    paths from it. A second addressing mode it let through without deriving
    ITS paths would be a hole in task-card authorisation, not a feature."""
    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    (root / "allowed").mkdir()
    (root / "allowed" / "mine.py").write_text("value = 1\nother = 2\n")
    git(root, "add", "--", "allowed/mine.py")
    git(root, "commit", "-qm", "scoped file")
    e = scope_host(sf, rid, claim, root, tmp_path)

    cite = _cite(sf, rid, claim, "allowed/mine.py")
    assert cite["citable"] is True and cite["start_line"] == 1

    ok = e._exec_tool({"tool": "apply_patch", "params": {"references": [
        {"file": "allowed/mine.py", "sha": cite["sha"], "from_line": 1,
         "from_col": 8, "to_line": 1, "to_col": 9, "new_text": "41"}]}})
    assert ok.get("applied") is True, ok
    assert (root / "allowed" / "mine.py").read_text() == "value = 41\nother = 2\n"

    outside = _cite(sf, rid, claim, "existing.py")
    refused = e._exec_tool({"tool": "apply_patch", "params": {"references": [
        {"file": "existing.py", "sha": outside["sha"], "from_line": 1,
         "from_col": 4, "to_line": 1, "to_col": 5, "new_text": "9"}]}})
    assert refused.get("scope_violation") is True, refused
    assert refused["requested_path"] == "existing.py"
    assert (root / "existing.py").read_text() == "x = 1\n"


def test_a_digest_the_read_never_issued_cannot_edit_an_authorised_file(
    tmp_path, monkeypatch
):
    """Scope authorises the PATH. The citation authorises the RANGE — without
    it, reference mode would be blind coordinate editing inside the scope."""
    import hashlib

    sf, rid, claim, root = fixture(tmp_path, monkeypatch)
    (root / "allowed").mkdir()
    (root / "allowed" / "mine.py").write_text("value = 1\n")
    git(root, "add", "--", "allowed/mine.py")
    git(root, "commit", "-qm", "scoped file")
    e = scope_host(sf, rid, claim, root, tmp_path)

    forged = hashlib.sha256(b"value = 1").hexdigest()[:40]
    got = e._exec_tool({"tool": "apply_patch", "params": {"references": [
        {"file": "allowed/mine.py", "sha": forged, "from_line": 1,
         "from_col": 8, "to_line": 1, "to_col": 9, "new_text": "41"}]}})
    assert got.get("applied") is False
    assert "never issued" in got["error"]
    assert (root / "allowed" / "mine.py").read_text() == "value = 1\n"
