"""A seeded config is not schedulable until its seed is published.

Live, 2026-09-05, wuxia-m1-original-map. `POST /api/projects` registered a
reserved git worktree as an existing-repo project with
`config_name="coding_impl"` at 09:08:04. `ensure_project` inserts with the
schema default `status='planning'`, and the poller selects on
`(status='planning', config_name IN <scheduler-owned>)` — so at 09:08:05 it
created and started a run and claimed `implement`. There was no `plan.md`. The
spawned implementer had no scope, no goals and no restrictions; it found a prior
round's `t_impl/fix_terminal_victory_win/DIAGNOSIS.md` in the repository, decided
that was its plan, and edited it.

Three guards already existed and all three are shaped for the DPE brief:
`meta_state='drafting'` is never set by that endpoint; `planning_guard` wants a
`dpe_run_state.brief` row that existing-repo registration never writes; and
`missing_cross_config_inputs` skips same-config sources by an explicit decision
that is right for a step output and wrong for a seed.

What these pin is not merely "the file exists". Two failures survive an
existence check and the tests below are ordered by them:

  * a seed set is not published until ALL of it is written — a config with
    several seed inputs must never be visible after the first one lands, and no
    file may be visible half-written;
  * `start_config_run` inserts the project row before it writes the seed, so a
    tick landing inside that window reproduces the incident from the SUPPORTED
    launch path, with no operator error at all.
"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader

from core import scheduler
from core.run_launcher import start_config_run
from core.seed_publication import (MARKER, adopt_legacy_seed, publish_seeds,
                                   published_generation, reads_own_seed,
                                   seed_dir, seed_is_published)
import core.seed_publication as seedmod

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


# ── publication is atomic, and it is the SET that publishes ──────────

def _gate_after_every_write(d: Path, files: dict, monkeypatch) -> list[dict]:
    """Call the REAL gate after each real file rename during a real publication.

    This is the independent reviewer's interception, kept as the regression
    test: intercept `_atomic_write` immediately after it returns, ask the gate,
    and let publication continue. Every observation must be a complete
    generation or nothing — never a mixture.
    """
    seen: list[dict] = []
    real = seedmod._atomic_write

    def spy(path, body):
        real(path, body)
        ok, why = seed_is_published(d, "plan.md")
        seen.append({
            "after": path.name, "gate": ok, "why": why,
            "generation": published_generation(d),
            "plan": (d / "plan.md").read_text() if (d / "plan.md").is_file() else None,
            "extra": (d / "extra.md").read_text() if (d / "extra.md").is_file() else None,
        })
    monkeypatch.setattr(seedmod, "_atomic_write", spy)
    publish_seeds(d, files)
    return seen


def test_a_fresh_publication_is_invisible_until_the_whole_set_lands(
        tmp_path, monkeypatch):
    """The reviewer's `fresh` counterexample: gate=true after the FIRST rename.

    Pre-repair this observed `gate=true, marker=false, plan=new-plan,
    extra=null` — the "legacy" fallback answering over a publication in
    progress, because nothing could tell the two apart.
    """
    d = tmp_path / "_seed"
    seen = _gate_after_every_write(
        d, {"plan.md": "new-plan", "extra.md": "new-extra"}, monkeypatch)

    assert seen, "the interception never fired"
    for obs in seen:
        assert obs["gate"] is False, f"admitted mid-publication: {obs}"
    ok, why = seed_is_published(d, "plan.md")
    assert ok, why
    assert (d / "plan.md").read_text() == "new-plan"
    assert (d / "extra.md").read_text() == "new-extra"


def test_a_replacement_publication_never_shows_a_mixed_generation(
        tmp_path, monkeypatch):
    """The reviewer's `replacement` counterexample: `gate=true, marker=true,
    plan=new-plan, extra=old-extra` — the previous marker still vouching for a
    set that was being overwritten underneath it."""
    d = tmp_path / "_seed"
    first = publish_seeds(d, {"plan.md": "old-plan", "extra.md": "old-extra"})

    seen = _gate_after_every_write(
        d, {"plan.md": "new-plan", "extra.md": "new-extra"}, monkeypatch)

    assert seen
    for obs in seen:
        # Every observation during the swap is the OLD generation, whole.
        assert obs["generation"] == first, f"mixed generation observed: {obs}"
        assert (obs["plan"], obs["extra"]) == ("old-plan", "old-extra"), obs
    assert published_generation(d) != first
    assert (d / "plan.md").read_text() == "new-plan"
    assert (d / "extra.md").read_text() == "new-extra"


def test_the_marker_names_the_whole_set(tmp_path):
    d = tmp_path / "_seed"
    publish_seeds(d, {"plan.md": "p", "extra.json": "{}"})
    names = json.loads((d / MARKER).read_text())["files"]
    assert names == ["extra.json", "plan.md"]
    for n in names:
        assert (d / n).is_file()


def test_a_seed_directory_with_no_marker_is_not_published(tmp_path):
    """No fallback. A lone non-empty seed file is indistinguishable from a
    publication in progress, which is precisely how the first repair admitted
    half a set."""
    d = tmp_path / "_seed"
    d.mkdir(parents=True)
    (d / "plan.md").write_text("the plan", encoding="utf-8")
    ok, why = seed_is_published(d, "plan.md")
    assert not ok and "adopt_legacy_seed" in why


def test_a_legacy_workspace_is_migrated_explicitly(tmp_path):
    """The supported route for a pre-generation workspace: deliberate, named,
    and never taken by the read path."""
    d = tmp_path / "_seed"
    d.mkdir(parents=True)
    (d / "plan.md").write_text("the plan", encoding="utf-8")
    assert seed_is_published(d, "plan.md")[0] is False

    gen = adopt_legacy_seed(d, "plan.md")

    assert gen and published_generation(d) == gen
    assert seed_is_published(d, "plan.md")[0] is True
    assert (d / "plan.md").read_text() == "the plan"
    assert adopt_legacy_seed(d, "plan.md") is None      # idempotent


def test_a_published_set_missing_a_named_file_is_refused(tmp_path):
    d = tmp_path / "_seed"
    publish_seeds(d, {"plan.md": "p", "extra.json": "{}"})
    (d / "extra.json").unlink()
    ok, why = seed_is_published(d, "plan.md")
    assert not ok and "extra.json" in why


def test_a_crash_mid_swap_leaves_nothing_published_not_a_mixture(tmp_path,
                                                                 monkeypatch):
    """If the process dies between the two renames, `_seed` is absent. That is
    "not published" — the safe answer — and the next publication clears it."""
    d = tmp_path / "_seed"
    publish_seeds(d, {"plan.md": "old", "extra.md": "old"})
    real_rename = seedmod.os.rename
    calls = []

    def die_after_first(src, dst):
        real_rename(src, dst)
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError("process died mid-swap")
    monkeypatch.setattr(seedmod.os, "rename", die_after_first)
    with pytest.raises(OSError):
        publish_seeds(d, {"plan.md": "new", "extra.md": "new"})
    monkeypatch.undo()

    ok, _ = seed_is_published(d, "plan.md")
    assert ok is False, "a half-swapped directory must not read as published"
    publish_seeds(d, {"plan.md": "new", "extra.md": "new"})
    assert seed_is_published(d, "plan.md")[0] is True
    assert (d / "plan.md").read_text() == "new"


def test_no_file_is_ever_visible_half_written(tmp_path):
    """Content arrives by rename, so a concurrent reader never sees a prefix."""
    d = tmp_path / "_seed"
    big = "x" * 2_000_000
    torn: list[int] = []
    stop = threading.Event()

    def watcher():
        f = d / "plan.md"
        while not stop.is_set():
            try:
                n = len(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if n != len(big):
                torn.append(n)

    d.mkdir(parents=True)
    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    try:
        publish_seeds(d, {"plan.md": big})
    finally:
        stop.set()
        t.join(5)
    assert torn == [], f"a reader saw a partial seed: {sorted(set(torn))[:5]}"


def test_an_empty_seed_is_not_a_seed(tmp_path):
    d = tmp_path / "_seed"
    publish_seeds(d, {"plan.md": "   \n"})
    ok, why = seed_is_published(d, "plan.md")
    assert not ok and "empty" in why


def test_an_unwritten_seed_says_so(tmp_path):
    ok, why = seed_is_published(tmp_path / "_seed", "plan.md")
    assert not ok and "plan.md" in why


# ── the gate is narrow, and derived from the graph ───────────────────

def _graph(name: str) -> PipelineGraph:
    return PipelineGraph.from_yaml(CONFIGS / f"{name}.yaml")


def test_coding_impl_is_gated_by_its_own_plan():
    g = _graph("coding_impl")
    assert reads_own_seed(g, "coding_impl", "plan.md")


def test_dpe_is_not_gated_and_this_is_the_regression_that_matters():
    """dpe_default_v2 DECLARES seed_file: project_brief.md and never reads it.

    Its brief reaches step 1 as a cross-config import from meta_conversation,
    which the existing guards already cover. A blanket "the declared seed_file
    must exist" gate would have stalled every DPE build in the system.
    """
    g = _graph("dpe_default")
    assert g.name == "dpe_default_v2"
    assert not reads_own_seed(g, "dpe_default_v2", "project_brief.md")


def test_a_same_config_step_output_is_not_mistaken_for_a_seed():
    """coding_impl's implement step also reads `{step: "test"}` — the previous
    iteration's test report, which the RUN produces for itself. A gate that
    counted that as a seed would wait forever for a file only the run can write.
    """
    g = _graph("coding_impl")
    assert not reads_own_seed(g, "coding_impl", "test_report.json")
    # and the shape directly: a source carrying `step` is never a seed
    node = next(s for s in g.steps if s.id == "implement")
    assert any((sp.get("source", sp) or {}).get("step") == "test"
               for sp in node.context)


def test_every_shipped_config_that_feeds_itself_names_its_seed():
    """Contract sweep: whatever the gate is asked about, it can answer.

    A config whose graph reads `{config: <self>, output: …}` is fed by its
    launcher, so it must declare WHICH of those files is the seed — otherwise
    the gate has nothing to key on and the config silently falls back to
    ungated, which is the hole this whole change closes. (Extra launcher-written
    inputs alongside the seed are fine: pipeline_forge reads its
    `skill_description.md` seed AND an optional `baseline_graph.yaml`.)
    """
    checked = []
    for path in sorted(CONFIGS.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        seed = ((raw.get("x-aitelier") or {}).get("seed_file")) or ""
        g = PipelineGraph.from_yaml(path)
        own = {(src or {}).get("output")
               for node in g.steps for spec in (node.context or [])
               for src in [spec.get("source", spec)]
               if isinstance(src, dict) and not src.get("step")
               and src.get("config") == g.name and src.get("output")}
        if not own:
            continue
        checked.append(g.name)
        assert seed, f"{path.name}: feeds itself {sorted(own)} but declares no seed_file"
        assert seed in own, (
            f"{path.name}: seed_file is {seed!r} but the graph reads only "
            f"{sorted(own)} — the gate would never fire")
        assert reads_own_seed(g, g.name, seed)
    assert "coding_impl" in checked and len(checked) >= 6, checked


# ── the poller: an unseeded project is not started ───────────────────

def _manifest(name, seed_file, owned=True):
    return SimpleNamespace(config_name=name, seed_file=seed_file,
                           scheduler_owned=owned, repo_mode="code",
                           registers_generated_pipeline=False,
                           registers_generated_addon=False)


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A REAL SkillFlow and a REAL workspace — the interleaving under test is
    between two real writers, and a mock cannot race."""
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(),
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"))
    sf.register_agent_config("offload_implementer", tools=["read_file"])
    sf.register_graph(_graph("coding_impl"))

    registry = MagicMock()
    registry.get.side_effect = lambda n: _manifest(n, "plan.md")

    db = MagicMock()
    db.get_project.return_value = {"project_id": "p1", "config_name": "coding_impl",
                                   "meta_state": None, "brief": ""}
    monkeypatch.setattr(scheduler, "db", db)
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: registry)
    logged: list = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda pid, outcome, **kw: logged.append((outcome, kw)))
    return SimpleNamespace(sf=sf, db=db, registry=registry, logged=logged,
                           tmp=tmp_path)


def test_the_incident_registration_alone_starts_nothing(live):
    """The exact shape of 09:08:04 -> 09:08:05."""
    assert scheduler._get_or_create_skillflow_run("p1") is None
    assert live.sf.list_runs() == []
    outcome, kw = live.logged[-1]
    assert outcome == "awaiting_seed"
    assert kw["seed"] == "plan.md" and "plan.md" in kw["reason"]


def test_a_published_seed_releases_it(live):
    publish_seeds(seed_dir(live.sf, "p1", "coding_impl"), {"plan.md": "# plan\n"})
    run_id = scheduler._get_or_create_skillflow_run("p1")
    assert run_id
    assert live.sf.get_run(run_id)["status"] == "running"


def test_an_empty_published_seed_does_not(live):
    publish_seeds(seed_dir(live.sf, "p1", "coding_impl"), {"plan.md": "\n"})
    assert scheduler._get_or_create_skillflow_run("p1") is None
    assert live.logged[-1][0] == "awaiting_seed"


def test_an_already_running_run_is_returned_regardless(live):
    """The gate guards CREATION only. A run that is already going must keep
    going, or a launcher that died after start_run would leave it undriveable.
    """
    rid = live.sf.get_or_create_run("coding_impl", "p1", {"project_id": "p1"})
    live.sf.start_run(rid)
    assert scheduler._get_or_create_skillflow_run("p1") == rid


def test_a_gateless_config_is_untouched(live, monkeypatch):
    """A config that does not read its own seed (dpe_default_v2) must not be
    gated even though it declares one."""
    dpe = _graph("dpe_default")
    for node in dpe.steps:
        if getattr(node, "agent_config", None):
            live.sf.register_agent_config(node.agent_config, tools=["read_file"])
    live.sf.register_graph(dpe)
    live.registry.get.side_effect = lambda n: _manifest(n, "project_brief.md")
    live.db.get_project.return_value = {"project_id": "p1", "meta_state": None,
                                        "config_name": "dpe_default_v2",
                                        "brief": "b"}
    monkeypatch.setattr("core.run_launcher.missing_cross_config_inputs",
                        lambda *a, **k: [])
    run_id = scheduler._get_or_create_skillflow_run("p1")
    assert run_id, [x for x in live.logged]


# ── the launcher window ──────────────────────────────────────────────

def test_a_tick_inside_start_config_run_creates_no_second_run(live, monkeypatch):
    """The window the incident did NOT come through, and would have next time.

    `start_config_run` inserts the project row (`db.ensure_project`) and only
    then builds the workspace and writes the seed. A poller tick landing in
    between is not hypothetical — the interval is 5 s and `setup_workspace`
    clones/inits a repository. Fire a REAL tick from inside `setup_workspace` to
    place it exactly there, deterministically.
    """
    ticks: list = []

    def ws_setup(project_id, **kw):
        ticks.append(scheduler._get_or_create_skillflow_run(project_id))

    ws = MagicMock()
    ws.setup_workspace.side_effect = ws_setup
    live.db.get_project.return_value = None       # not registered yet
    monkeypatch.setattr(scheduler, "wake_scheduler", lambda *a, **k: None)

    def after_ensure(pid, **kw):
        live.db.get_project.return_value = {
            "project_id": pid, "config_name": "coding_impl",
            "meta_state": None, "brief": ""}
        return {}
    live.db.ensure_project.side_effect = after_ensure

    result = start_config_run(live.db, ws, "coding_impl", "p1",
                              seed_text="# approved plan\n",
                              repo_type="existing",
                              repo_path=str(live.tmp / "projects" / "p1"))

    assert ticks == [None], "the poller started a run before the seed existed"
    assert result["status"] == "started"
    runs = live.sf.list_runs()
    assert len(runs) == 1 and runs[0]["id"] == result["run_id"]
    assert live.sf.get_run(result["run_id"])["status"] == "running"
    ok, _ = seed_is_published(seed_dir(live.sf, "p1", "coding_impl"), "plan.md")
    assert ok


def test_a_tick_after_publication_but_before_start_still_yields_one_run(
        live, monkeypatch):
    """The other half of the window: once the seed is published the poller MAY
    adopt the project, and it must converge on the launcher's run rather than
    compete with it — including `start_run`, which raises on a row that is no
    longer pending and used to take the whole launch down with it.
    """
    seen: list = []
    real_publish = start_config_run.__globals__["publish_seeds"]

    def publish_then_tick(directory, files):
        out = real_publish(directory, files)
        seen.append(scheduler._get_or_create_skillflow_run("p1"))   # poller wins
        return out

    monkeypatch.setitem(start_config_run.__globals__, "publish_seeds",
                        publish_then_tick)
    monkeypatch.setattr(scheduler, "wake_scheduler", lambda *a, **k: None)
    live.db.get_project.return_value = {"project_id": "p1",
                                        "config_name": "coding_impl",
                                        "meta_state": None, "brief": ""}

    result = start_config_run(live.db, MagicMock(), "coding_impl", "p1",
                              seed_text="# approved plan\n",
                              repo_type="existing",
                              repo_path=str(live.tmp / "projects" / "p1"))

    assert result["status"] == "started", result
    assert seen and seen[0] == result["run_id"], \
        "the launcher and the poller ended up on different runs"
    assert len(live.sf.list_runs()) == 1


# ── the runtime backstop ─────────────────────────────────────────────

def test_a_plan_less_run_cannot_claim_its_implement_step(live):
    """`required: true` on coding_impl's plan. Reached only if something creates
    a run with no seed anyway — and then it is a named terminal failure at the
    first claim, not an implementer with no scope."""
    from skillflow.exceptions import RequiredContextMissing
    rid = live.sf.get_or_create_run("coding_impl", "p1", {"project_id": "p1"})
    live.sf.start_run(rid)
    live.sf.advance_run(rid)
    with pytest.raises(RequiredContextMissing):
        live.sf.claim_next_step(rid)


def test_a_seeded_run_claims_and_the_agent_gets_the_plan(live):
    """The compatible healthy launch the backstop must not break."""
    publish_seeds(seed_dir(live.sf, "p1", "coding_impl"),
                  {"plan.md": "# approved plan\nrename the map\n"})
    rid = live.sf.get_or_create_run("coding_impl", "p1", {"project_id": "p1"})
    live.sf.start_run(rid)
    live.sf.advance_run(rid)
    claimed = live.sf.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == "implement"
    assert "rename the map" in json.dumps(claimed.inputs)
