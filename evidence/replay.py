#!/usr/bin/env python3
"""Replay the independent reviewer's five findings against WHICHEVER trees are
importable, asserting PROPERTIES rather than mechanisms.

The reviewer's own probe.py cannot run unmodified against the repair: it reaches
for `_begin_delivery` and `delivery_started_at`, both of which the repair
removed (a stamp taken before validation is not proof that a hook has started).
So each finding is restated as a property with a version-agnostic interception
point, and the same file runs on both trees.

    PYTHONPATH=<aitelier tree>:<skillflow tree>/src python replay.py

F1 fresh publication is never admitted before the whole set lands
F2 replacement publication never exposes a mixed generation
F3 a stop is not reported as complete while an effect is still to come
F4 no NEW tool side effect after a stop that reported completion
F5 fail_step does not revive a claim the cancellation closed
"""
import json, subprocess, sys, tempfile
from pathlib import Path

import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
import core.seed_publication as seeds

TOOLS = Path(skillflow.__file__).parent / "tools"
results: list[tuple[str, str, str]] = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:                                    # noqa: BLE001
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
    else:
        results.append((name, "PASS", ""))


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def build(root):
    repo = root / "projects" / "p1"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "base")
    node = StepNode(id="work", step_type="agent", agent_config="worker",
                    output_mode="write", transitions=[Transition(to=None)],
                    lifecycle={"on_deliver": [
                        {"tool": "repo_apply",
                         "params": {"source_dir": "$STEP_DIR"}}]})
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(TOOLS),
                   workspace_base=str(root / "ws"),
                   projects_base=str(root / "projects"))
    sf.register_agent_config("worker", tools=["read_file"])
    sf.register_graph(PipelineGraph(name="g", begin="work", steps=[node]))
    return sf, repo


def claim(sf):
    rid = sf.create_run("g", {"project_id": "p1"}, project_id="p1")
    sf.start_run(rid); sf.advance_run(rid)
    return rid, sf.claim_next_step(rid).token


def stage(sf, name="DIAGNOSIS.md", body="x\n"):
    d = sf._workspace.get_step_tmp_dir("p1", "g", "work")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def count(repo):
    return int(git(repo, "rev-list", "--count", "HEAD"))


def rows(sf, rid):
    return [dict(r) for r in sf._conn.execute(
        "SELECT step_id, status FROM skillflow_steps WHERE run_id = ?",
        (rid,)).fetchall()]


def stop(sf, rid, reason):
    """Cancel through whichever entry point the tree provides."""
    fn = getattr(sf, "stop_run", None) or sf.fail_run
    return fn(rid, reason) or {}


def completed_stop(report):
    """Did this stop claim to be finished? True on a tree with no `outcome`
    key (the reviewed candidate always claimed completion)."""
    return (report.get("outcome") or "stopped") == "stopped"


# ── F1 / F2: seed generations ────────────────────────────────────────

def _observe_during_publication(d, files):
    seen = []
    real = seeds._atomic_write

    def spy(path, body):
        real(path, body)
        ok, why = seeds.seed_is_published(d, "plan.md")
        seen.append({
            "after": path.name, "gate": ok,
            "plan": (d / "plan.md").read_text() if (d / "plan.md").is_file() else None,
            "extra": (d / "extra.md").read_text() if (d / "extra.md").is_file() else None,
        })
    seeds._atomic_write = spy
    try:
        seeds.publish_seeds(d, files)
    finally:
        seeds._atomic_write = real
    return seen


def f1():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "_seed"
        seen = _observe_during_publication(
            d, {"plan.md": "new-plan", "extra.md": "new-extra"})
        assert seen, "interception never fired"
        bad = [o for o in seen if o["gate"]]
        assert not bad, f"admitted mid-publication: {json.dumps(bad)}"
        assert seeds.seed_is_published(d, "plan.md")[0], "never became published"


def f2():
    """Two acceptable answers, and one unacceptable one.

    A tree may REFUSE to replace published content (immutability — a reader
    resolving several sources by path cannot then be handed two generations), or
    it may replace it without ever exposing a mixture. Exposing a mixture is the
    failure.
    """
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "_seed"
        seeds.publish_seeds(d, {"plan.md": "old-plan", "extra.md": "old-extra"})
        try:
            seen = _observe_during_publication(
                d, {"plan.md": "new-plan", "extra.md": "new-extra"})
        except Exception:                                     # noqa: BLE001
            # Refused. The published set must be untouched.
            assert (d / "plan.md").read_text() == "old-plan", "refused, then mutated"
            assert (d / "extra.md").read_text() == "old-extra", "refused, then mutated"
            ok, why = seeds.seed_is_published(d, "plan.md")
            assert ok, f"refusal left the seed unpublished: {why}"
            return
        mixed = [o for o in seen
                 if o["gate"] and {o["plan"], o["extra"]} == {"new-plan", "old-extra"}]
        assert not mixed, f"mixed generation admitted: {json.dumps(mixed)}"
        assert (d / "plan.md").read_text() == "new-plan"
        assert (d / "extra.md").read_text() == "new-extra"


# ── F3: a completed stop must not precede an effect ──────────────────

def f3():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t); sf, repo = build(root); rid, tok = claim(sf); stage(sf)
        before = count(repo)
        box = {}
        # Version-agnostic interception: cancel between the authorisation of the
        # delivery and its first lifecycle hook. On the reviewed candidate that
        # authorisation is `_begin_delivery`; on the repair it is `_admit_op`.
        if hasattr(sf, "_admit_op"):
            real = sf._admit_op
            def wrap(*a, **kw):
                op = real(*a, **kw)
                box["report"] = stop(sf, rid, "cancel before first hook")
                box["status_at_stop"] = sf.get_run(rid)["status"]
                return op
            sf._admit_op = wrap
        else:
            real = sf._begin_delivery
            def wrap(token):
                real(token)
                box["report"] = stop(sf, rid, "cancel before first hook")
                box["status_at_stop"] = sf.get_run(rid)["status"]
            sf._begin_delivery = wrap
        try:
            sf.confirm_step(tok, StepResult(outputs={}))
        except Exception:
            pass
        committed = count(repo) - before
        rep = box.get("report", {})
        assert box.get("report") is not None, "the cancel never ran"
        if committed:
            assert not completed_stop(rep), (
                f"stop reported completion, then {committed} commit(s) landed: {rep}")
            assert box["status_at_stop"] not in ("failed", "completed"), (
                f"run was marked {box['status_at_stop']} while an effect was "
                f"still to come")
        assert sf.get_run(rid)["status"] == "failed", \
            "the cancellation never reached a terminal state"


# ── F4: no new tool effect after a completed stop ────────────────────

def f4():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t); sf, _ = build(root); rid, tok = claim(sf)
        box = {}
        real = sf._execute_tool_impl

        def cancel_then_run(*a, **kw):
            box["report"] = stop(sf, rid, "cancel after the tool was admitted")
            return real(*a, **kw)
        sf._execute_tool_impl = cancel_then_run
        out = sf.execute_tool("create", {"file": "after.md", "content": "after-stop"},
                              run_id=rid, step_id="work",
                              step_instance_id=tok.step_instance_id,
                              claim_epoch=tok.claim_epoch)
        staged = sf._workspace.get_step_tmp_dir("p1", "g", "work") / "after.md"
        rep = box.get("report", {})
        if staged.exists():
            assert not completed_stop(rep), (
                f"stop reported completion ({rep}), then a NEW file was staged: "
                f"{staged}")
        assert "error" in out or staged.exists(), "tool neither ran nor was refused"


# ── F5: fail_step must not revive a closed claim ─────────────────────

def f5():
    with tempfile.TemporaryDirectory() as t:
        root = Path(t); sf, _ = build(root); rid, tok = claim(sf)
        box = {}
        real = sf._release_step_tools

        def release_then_cancel(*a, **kw):
            real(*a, **kw)
            if "report" not in box:
                box["report"] = stop(sf, rid, "cancel after fail_step began")
        sf._release_step_tools = release_then_cancel
        sf.fail_step(tok, "error", retryable=True)
        assert box.get("report") is not None, "the cancel never ran"
        after = rows(sf, rid)
        assert all(r["status"] != "pending" for r in after), (
            f"fail_step revived a claim the cancellation closed: {after} "
            f"(stop said {box['report']})")


print(f"aitelier under test:  {Path(seeds.__file__).resolve().parents[1]}")
print(f"skillflow under test: {Path(skillflow.__file__).resolve().parents[1]}")
check("F1 fresh publication never admitted early", f1)
check("F2 replacement never shows a mixed generation", f2)
check("F3 completed stop never precedes an effect", f3)
check("F4 no new tool effect after a completed stop", f4)
check("F5 fail_step does not revive a closed claim", f5)
w = max(len(n) for n, _, _ in results)
for n, v, detail in results:
    print(f"{n.ljust(w)}  {v}" + (f"  {detail}" if detail else ""))
sys.exit(0 if all(v == "PASS" for _, v, _ in results) else 1)
