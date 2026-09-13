"""Real SkillFlow delivery cycle over a synthetic private novel repository.

The model boundary is deterministic, but graph routing, checkpoints, generated
write tools, strict apply_patch, Git worktree isolation and novel tools are real.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import skillflow as _skillflow_pkg
from skillflow import PipelineGraph, SkillFlow
from skillflow.tool_loader import ToolLoader

from aitelier import novel_state as ns
from aitelier.runner import AgentStepRunner
from core import run_isolation
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager


ROOT = Path(__file__).resolve().parents[2]
BRIEF = "本章必须让青禾主动拒绝掌门令，只揭示铜铃发热，不揭开其来历。"
REVISION = "拒绝改成她把掌门令压在茶盏下，并让同行者先误会她要服从。"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _action(tool: str, **params) -> dict:
    return {"tool": tool, "params": params}


def _bible_patch() -> str:
    files = {
        "novel/bible/overview.md": "# 总纲\n\n青禾寻找失踪的姐姐，代价是逐渐失去宗门信任。",
        "novel/bible/compass.md": "终局：姐妹重逢。活跃长线：铜铃来历。",
        "novel/bible/world.yaml": yaml.safe_dump(
            {"magic_system": {"境界": ["听风", "照影", "归真"]}}, allow_unicode=True
        ),
        "novel/bible/pacing.yaml": yaml.safe_dump(
            {"min_chars_per_chapter": 100, "max_chars_per_chapter": 6000},
            allow_unicode=True,
        ),
        "novel/bible/characters.yaml": yaml.safe_dump(
            [
                {"name": "青禾", "role": "protagonist", "is_protagonist": True,
                 "power_level": 10, "tier": 1, "personality": ["审慎"]},
                {"name": "迟舟", "role": "companion", "status": "alive"},
            ], allow_unicode=True,
        ),
        "novel/bible/threads.yaml": yaml.safe_dump(
            [{"name": "铜铃来历", "description": "姐姐留下的铜铃为何发热", "importance": 8,
              "earliest_reveal": {"arc": "寻姐", "node": "n2"}}], allow_unicode=True
        ),
        "novel/bible/arcs.yaml": yaml.safe_dump(
            [{"name": "寻姐", "arc_type": "main", "description": "追踪姐姐",
              "nodes": [{"id": "n1", "beat": "拒绝掌门令"},
                        {"id": "n2", "beat": "找到铜铃线索"}]}], allow_unicode=True
        ),
    }
    lines = ["*** Begin Patch"]
    for name, body in files.items():
        lines.append(f"*** Add File: {name}")
        lines.extend("+" + line for line in body.splitlines())
    lines.append("*** End Patch")
    return "\n".join(lines)


def _prose(chapter: int, *, polished: bool) -> str:
    verb = "压在" if polished else "按在"
    opening = (
        f"# 第{chapter}章：茶盏下的掌门令\n\n"
        f"迟舟看见青禾接过掌门令，以为她终于服从。青禾没有解释，只把令牌{verb}茶盏下。"
    )
    body = "铜铃贴着她腕骨发热，她仍把来历咽回去。门外风声一阵紧过一阵。" * 10
    ending = "\n\n她推开茶盏，明确拒绝掌门令：‘这条路我自己选。’迟舟这才明白自己误会了她。"
    if chapter == 2:
        opening = "# 第2章：风里的回声\n\n青禾记得茶盏下那枚掌门令，也记得迟舟的误会。"
        body = "铜铃仍在发热，却没有交代来历。青禾沿着山道追查姐姐留下的脚印。" * 10
        ending = "\n\n脚印在断桥边消失，她决定天亮前渡河。"
    return opening + body + ending


def _events(chapter: int) -> dict:
    if chapter == 1:
        return {
            "chapter": 1, "title": "茶盏下的掌门令",
            "summary": "青禾以茶盏压住掌门令，拒绝服从；迟舟先误会后理解，铜铃仅仅发热。",
            "events": [], "appearances": [{"name": "青禾"}, {"name": "迟舟"}],
            "locations": ["山门"],
            "thread_updates": [{"name": "铜铃来历", "action": "hint", "detail": "铜铃发热"}],
            "arc_updates": [{"name": "寻姐", "nodes_completed": ["n1"], "notes": "拒绝掌门令"}],
        }
    return {
        "chapter": 2, "title": "风里的回声",
        "summary": "青禾从断桥脚印继续追查姐姐，记得上章的拒令与误会，仍未揭开铜铃来历。",
        "events": [], "appearances": [{"name": "青禾"}], "locations": ["断桥"],
        "thread_updates": [{"name": "铜铃来历", "action": "hint", "detail": "仍然发热"}],
        "arc_updates": [{"name": "寻姐", "nodes_completed": [], "notes": "抵达断桥"}],
    }


def _engine(db_path: Path, ws_base: Path, project_root_by_run: dict) -> SkillFlow:
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(ROOT / "aitelier" / "tools")
    inline = {"restage", "scaffold_bible", "state_probe", "continuity_check", "apply_state"}
    native = loader.is_native
    loader.is_native = lambda name: name in inline or native(name)
    sf = SkillFlow(
        str(db_path), tool_loader=loader, workspace_base=str(ws_base),
        projects_base=str(ws_base.parent / "projects"), stale_threshold_seconds=1,
        code_path_resolver=lambda _pid, run_id=None: project_root_by_run.get(run_id),
    )
    for config in ("novel_init", "novel_chapter"):
        roles = yaml.safe_load((ROOT / "agent_configs" / f"{config}.yaml").read_text())
        for name, value in roles.items():
            try:
                sf.register_agent_config_from_dict(name, value)
            except Exception:
                pass
        sf.register_graph(PipelineGraph.from_yaml(ROOT / "configs" / f"{config}.yaml"))
    return sf


def _seed(sf: SkillFlow, project_id: str, config: str, filename: str, text: str) -> None:
    directory = sf._workspace.get_config_path(project_id, config)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(text, encoding="utf-8")


def _response(step_id: str, schemas: dict, *, chapter: int, revised_outline: bool) -> str:
    if step_id == "brainstorm":
        return json.dumps({"thoughts": "synthetic proposal", "actions": [
            _action("write_proposal", content="# 方案\n\n青禾拒绝被宗门安排，并寻找姐姐。")
        ]}, ensure_ascii=False)
    if step_id == "design":
        assert "apply_patch" in schemas
        return json.dumps({"thoughts": "synthetic bible", "actions": [
            _action("apply_patch", patch=_bible_patch())
        ]}, ensure_ascii=False)
    if step_id.endswith("_review") or step_id == "design_review":
        verdict = {"passed": True, "feedback": "事实、因果和账本对应", "suggestions": []}
        return json.dumps({"thoughts": "independent red review", "actions": [
            _action("write_verdict", content=json.dumps(verdict, ensure_ascii=False))
        ]}, ensure_ascii=False)
    if step_id == "outline":
        detail = REVISION if revised_outline else BRIEF
        return json.dumps({"thoughts": "outline", "actions": [
            _action("write_outline", content=f"# 第{chapter}章纲\n\n{detail}\n铜铃只发热，不揭底。")
        ]}, ensure_ascii=False)
    if step_id == "draft":
        return json.dumps({"thoughts": "draft", "actions": [
            _action("write_draft", content=_prose(chapter, polished=False))
        ]}, ensure_ascii=False)
    if step_id == "humanize":
        return json.dumps({"thoughts": "language-only polish", "actions": [
            _action("write_final", content=_prose(chapter, polished=True))
        ]}, ensure_ascii=False)
    if step_id == "finalize":
        return json.dumps({"thoughts": "extract events", "actions": [
            _action("write_events", content=json.dumps(_events(chapter), ensure_ascii=False))
        ]}, ensure_ascii=False)
    raise AssertionError(f"unexpected agent step {step_id}")


async def _drive(sf, db, host_ws, run_id, monkeypatch, *, chapter=0,
                 reject_checkpoint=None, stop_at_checkpoint=None, observations=None):
    import api.dependencies as deps
    import core.agents as agents

    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    current = {"response": "{}"}
    calls = []

    def get_agent(_self, role):
        agent = MagicMock()
        agent.gateway.litellm_model = f"fixture/{role}"
        agent.run.side_effect = lambda *a, **k: current["response"]
        calls.append(role)
        return agent

    monkeypatch.setattr(agents.AgentFactory, "get_agent", get_agent)
    monkeypatch.setattr(agents.AgentFactory, "is_native", lambda _self, _name: False)
    monkeypatch.setattr(agents.AgentFactory, "get_max_retries", lambda _self, _name: 2)
    monkeypatch.setattr(agents.AgentFactory, "get_max_tool_turns", lambda _self, _name: 4)
    runner = AgentStepRunner(db, host_ws)
    rejected = False
    executed = []
    for _ in range(140):
        node = sf.advance_run(run_id)
        if node is None:
            row = sf.get_run(run_id)
            if row["status"] == "paused":
                checkpoint = {
                    "design_review": "design_gate",
                    "outline_review": "outline_gate",
                    "finalize_review": "final_gate",
                }.get(executed[-1], executed[-1])
                if stop_at_checkpoint == checkpoint:
                    return "paused", executed, calls
                if reject_checkpoint == checkpoint and not rejected:
                    redirect = {
                        "design_gate": "design",
                        "outline_gate": "outline",
                        "final_gate": "draft",
                    }.get(checkpoint, checkpoint)
                    sf.reject_checkpoint(run_id, checkpoint, REVISION, redirect_to=redirect)
                    rejected = True
                else:
                    sf.approve_checkpoint(run_id)
                continue
            if row["status"] == "running":
                continue
            return row["status"], executed, calls
        claim = sf.claim_next_step(run_id)
        if claim is None:
            continue
        resolved = claim.inputs.get("_resolved_context", {})
        if observations is not None:
            observations.append({
                "step": claim.step_id,
                "role": claim.inputs.get("_agent_config", {}).get("name"),
                "model": claim.inputs.get("_agent_config", {}).get("model"),
                "context": json.dumps(resolved, ensure_ascii=False),
            })
        revised = claim.step_id == "outline" and executed.count("outline") > 0
        current["response"] = _response(
            claim.step_id, claim.inputs.get("_tool_schemas", {}),
            chapter=chapter, revised_outline=revised,
        )
        executed.append(claim.step_id)
        result = await runner.execute(claim)
        sf.confirm_step(claim.token, result)
    return "timeout", executed, calls


def _make_run(sf, db, source: Path, project_id: str, config: str) -> tuple[str, dict]:
    db.ensure_project(project_id, name=project_id, repo_type="existing", repo_path=str(source),
                      config_name=config)
    run_id = sf.create_run(config, {"project_id": project_id}, project_id=project_id)
    rec = run_isolation.ensure_for_run(
        db, run_id=run_id, project_id=project_id, config_name=config, repo_mode="code"
    )
    sf._workspace._code_path_resolver = (
        lambda _pid, run_id=None: run_isolation.resolve_for_resolver(db, run_id)
    )
    sf.start_run(run_id)
    return run_id, rec


def _integrate(source: Path, rec: dict) -> tuple[str, str, str]:
    base = rec["base_sha"]
    candidate = _git(Path(rec["worktree_path"]), "rev-parse", "HEAD")
    assert _git(source, "rev-parse", "HEAD") == base
    _git(source, "merge", "--ff-only", candidate)
    integrated = _git(source, "rev-parse", "HEAD")
    assert integrated == candidate
    return base, candidate, integrated


@pytest.mark.asyncio
async def test_two_chapter_delivery_cycle_in_real_linked_worktrees(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    source = tmp_path / "synthetic-private-novel"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "config", "user.name", "Synthetic Fixture")
    (source / "README.md").write_text("synthetic fixture only\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "synthetic novel base")

    db = DBManager(str(tmp_path / "aitelier.db"))
    ws_base = tmp_path / "workspace"
    host_ws = WorkspaceManager(base_path=str(ws_base))
    roots = {}
    sf = _engine(tmp_path / "skillflow.db", ws_base, roots)

    # Init: the first checkpoint is genuinely revised. Design then executes in
    # the run-owned linked worktree and scaffold creates a readable genesis.
    _seed(sf, "init", "novel_init", "novel_request.md", "青禾寻找姐姐。")
    init_run, init_rec = _make_run(sf, db, source, "init", "novel_init")
    roots[init_run] = init_rec["worktree_path"]
    init_obs = []
    status, init_steps, init_calls = await _drive(
        sf, db, host_ws, init_run, monkeypatch,
        reject_checkpoint="brainstorm", observations=init_obs,
    )
    assert status == "completed"
    assert init_steps.count("brainstorm") == 2
    assert REVISION in next(o["context"] for o in init_obs
                            if o["step"] == "brainstorm" and REVISION in o["context"])
    linked = Path(init_rec["worktree_path"])
    assert (linked / ".git").is_file()
    assert _git(linked, "show", "novel-genesis:novel/bible/overview.md")
    genesis_target = _git(linked, "rev-parse", "novel-genesis^{commit}")
    assert genesis_target == _git(linked, "rev-parse", "HEAD")
    init_ids = _integrate(source, init_rec)
    run_isolation.release(db, init_run, "integrated-by-fixture")

    identities = {"init": init_ids}
    all_observations = list(init_obs)
    for chapter in (1, 2):
        pid = f"chapter-{chapter}"
        brief = BRIEF if chapter == 1 else "承接上一章拒令与迟舟误会，追查断桥脚印。"
        _seed(sf, pid, "novel_chapter", "director_brief.md", brief)
        run_id, rec = _make_run(sf, db, source, pid, "novel_chapter")
        roots[run_id] = rec["worktree_path"]
        observations = []
        if chapter == 1:
            status, steps, calls = await _drive(
                sf, db, host_ws, run_id, monkeypatch, chapter=chapter,
                reject_checkpoint="outline_gate", observations=observations,
            )
        else:
            # Stop at the final checkpoint, reconstruct the engine from the
            # same durable DB, and resume with the exact retained worktree.
            status, steps, calls = await _drive(
                sf, db, host_ws, run_id, monkeypatch, chapter=chapter,
                stop_at_checkpoint="final_gate", observations=observations,
            )
            assert status == "paused"
            rec_again = run_isolation.ensure_for_run(
                db, run_id=run_id, project_id=pid,
                config_name="novel_chapter", repo_mode="code",
            )
            assert rec_again["worktree_path"] == rec["worktree_path"]
            sf = _engine(tmp_path / "skillflow.db", ws_base, roots)
            sf._workspace._code_path_resolver = (
                lambda _pid, run_id=None: run_isolation.resolve_for_resolver(db, run_id)
            )
            sf.approve_checkpoint(run_id)
            resumed, later, more_calls = await _drive(
                sf, db, host_ws, run_id, monkeypatch, chapter=chapter,
                observations=observations,
            )
            status, steps, calls = resumed, steps + later, calls + more_calls
        assert status == "completed", steps
        if chapter == 1:
            assert steps.count("outline") == 2
            revised = [o for o in observations if o["step"] == "outline"][-1]
            assert BRIEF in revised["context"] and REVISION in revised["context"]
            revised_review = [
                o for o in observations if o["step"] == "outline_review"
            ][-1]
            assert REVISION in revised_review["context"]
            assert "茶盏下" in revised_review["context"]
        chapter_root = Path(rec["worktree_path"])
        chapter_dir = ns.chapter_dir(chapter_root, chapter)
        prose = (chapter_dir / "prose.md").read_text(encoding="utf-8")
        events = yaml.safe_load((chapter_dir / "events.yaml").read_text(encoding="utf-8"))
        summary = (chapter_dir / "summary.md").read_text(encoding="utf-8")
        assert events["chapter"] == chapter
        assert "青禾" in summary
        assert "铜铃" in prose and "来历" in prose
        assert "编辑" not in prose and "修改意见" not in prose
        if chapter == 1:
            assert "茶盏下" in prose and "误会" in prose and "拒" in prose
        else:
            assert "茶盏" in prose and "误会" in prose and "断桥" in prose
            outline_context = next(
                o["context"] for o in observations if o["step"] == "outline"
            )
            assert "第1章" in outline_context and "茶盏下的掌门令" in outline_context
        draft_review_context = next(
            o["context"] for o in observations if o["step"] == "draft_review"
        )
        humanize_review_context = next(
            o["context"] for o in observations if o["step"] == "humanize_review"
        )
        finalize_review_context = next(
            o["context"] for o in observations if o["step"] == "finalize_review"
        )
        assert f"第{chapter}章" in draft_review_context
        assert f"第{chapter}章" in humanize_review_context
        assert "summary" in finalize_review_context
        assert f'\\"chapter\\": {chapter}' in finalize_review_context
        identities[pid] = _integrate(source, rec)
        run_isolation.release(db, run_id, "integrated-by-fixture")
        all_observations.extend(observations)

    # Exact identity chain: every run starts from the previously integrated
    # candidate and only its own candidate is fast-forwarded into the source.
    assert identities["chapter-1"][0] == identities["init"][2]
    assert identities["chapter-2"][0] == identities["chapter-1"][2]
    assert _git(source, "rev-parse", "HEAD") == identities["chapter-2"][2]
    assert _git(source, "status", "--porcelain") == ""

    # Route identity and call counts come from actual graph claims. Creative
    # and Red roles remain separated through the portable aliases.
    route_by_role = {o["role"]: o["model"] for o in all_observations}
    assert route_by_role["novel_writer"] == "novel"
    assert route_by_role["novel_chapter_reviewer"] == "novel_alt"
    assert route_by_role["novel_humanizer"] == "novel_alt"
    assert route_by_role["novel_humanize_reviewer"] == "flash"
    assert sum(o["step"] == "draft" for o in all_observations) == 2
    assert sum(o["step"] == "humanize_review" for o in all_observations) == 2
    routes = json.loads((ROOT / "model_routes.example.json").read_text(encoding="utf-8"))
    assert {"novel", "novel_alt", "flash"} <= set(routes)

    index = ns.load_yaml(ns.state_dir(source) / "index.yaml")
    assert index["chapters_written"] == 2 and index["next_chapter"] == 3
    assert set(index["by_character"]["青禾"]) == {1, 2}
    assert _git(source, "show", "novel-genesis:novel/bible/overview.md")
    assert _sha(ns.chapter_dir(source, 1) / "prose.md") != _sha(ns.chapter_dir(source, 2) / "prose.md")
    print("NOVEL_CYCLE_IDENTITIES=" + json.dumps(identities, sort_keys=True))
