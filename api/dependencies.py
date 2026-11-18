# File: api/dependencies.py

import copy
import os
import threading
import time
from pathlib import Path
from fastapi import HTTPException, Request
from core import datadir
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from api.auth import CurrentUser

# Composition root: the ONE place that resolves the production data dir and
# hands explicit paths to every component (constructors REQUIRE paths — see
# core/datadir.py and tests/unit/test_datadir_guardrail.py for the rules).
# 默认从环境变量读取存储路径，方便在 Docker / 生产环境中复写
_AITELIER_HOME = datadir.aitelier_home()
_AITELIER_HOME.mkdir(parents=True, exist_ok=True)

DB_PATH = datadir.db_path()
SKILLFLOW_DB_PATH = datadir.skillflow_db_path()
WS_PATH = str(datadir.workspaces_dir())
PROJECTS_PATH = str(datadir.projects_dir())

# 单例模式实例化核心管理器
db_instance = DBManager(DB_PATH)
ws_instance = WorkspaceManager(WS_PATH, projects_base=PROJECTS_PATH)

def get_db_manager() -> DBManager:
    """FastAPI 依赖注入：获取数据库连接池"""
    return db_instance

def get_workspace_manager() -> WorkspaceManager:
    """FastAPI 依赖注入：获取物理工作区管理器"""
    return ws_instance


def _existing_repo_code_path(project_id: str,
                             run_id: str | None = None) -> str | bool | None:
    """Code-path resolver handed to skillflow.

    skillflow's default layout keys the code repo by project_id
    (projects_base/<id>), which cannot point a project at an *existing* repo.
    For `repo_type='existing'` projects AItelier records the linked repo in the
    DB (`repo_path`); returning it here makes skillflow's lifecycle hooks
    (repo_apply commits the fix into the real repo), `from: repository` context,
    and project-level lint all target that repo. Returns None for new/clone
    projects so skillflow keeps its default projects_base/<id> location.

    `run_id` is the isolation question and it is asked FIRST, because the
    project-keyed answer is the shared checkout: a run in its own worktree, a
    review reading an immutable snapshot and a run holding the checkout by lease
    are three different roots for one `repo_path`. A run that predates isolation
    (or one working directly in the checkout) answers "no opinion" and falls
    through to exactly the logic below, unchanged.

    `repo_type='none'` answers **False** — "this run owns no code repository" —
    which is a different statement from None's "no opinion, use your default".
    `setup_workspace` deliberately creates nothing for such a run, but skillflow
    still invented projects_base/<id> for it, and the read surface attached that
    path as a `repo` source on an `is_dir()` check. That made a repo-less run's
    repo access depend on whether the directory happened to exist rather than on
    what the run declares.

    A row can hold BOTH answers, and then `repo_path` wins. `run_launcher`
    overwrites a caller-supplied `repo_type="existing"` with `"none"` when the
    config declares `repo_mode: none`, and keeps the `repo_path` — which is
    exactly what the `against_project` path produces (`api/mcp_router.py`,
    `core/meta_agent.py:_tool_start_config_run`): a run that emits no code of its
    own but reads a real repository it was pointed at. Testing `repo_type` first
    answered False for those and took the repo away. A recorded path is a
    positive statement about where the code IS; `repo_type='none'` only says this
    run produces none.
    """
    if run_id:
        # The RUN is asked first, and its answer is final when it has one. A
        # missing worktree raises out of here rather than falling through to
        # the project-keyed path below — which would hand the run the shared
        # checkout it was isolated from, silently, which is the whole defect.
        from core import run_isolation
        answer = run_isolation.resolve_for_resolver(
            db_instance, run_id, sf=get_skillflow())
        if answer is not None:
            return answer
    try:
        info = db_instance.get_repo_info(project_id)
        path = info.get("repo_path") or None
        if path:
            return path
        if (info.get("repo_type") or "") == "none":
            return False
        return None
    except Exception:
        return None


def _ruff_check_only(fp: Path) -> dict:
    """Check-only `.py` lint backend, registered OVER skillflow's built-in `ruff`.

    Same rules the built-in selects — E9 (syntax), F63/F7 (real bugs), F82
    (undefined name) — and F821 undefined-name is the one that matters: it is the
    only static check that sees a name used but never imported, the defect that
    zeroes an entire repo (NL2Repo 2026-08-18: `AP` in fastapi-users cost all 556
    tests). Scoped to exactly that; no style rules, because a gate that fails on
    formatting turns into a retry loop, which is why step "3" had to drop its
    schema validation.

    What is dropped is the built-in's FIRST pass: `ruff check --fix` with ruff's
    DEFAULT rule set, which rewrites the file before checking it. Replayed over
    the 12 delivered repos of that sweep, it deleted imports the tests depend on —
    freezegun's `from datetime import date as date_alias` (an import that exists
    precisely so the test can watch it get patched) and a function-local re-import
    in test_class_import.py. Both read as F401-unused to a per-file linter. The
    t_impl lint was a no-op until now, so nothing had ever run that pass on
    delivered code; un-breaking the gate without dropping it would have made a
    checker into an editor.

    Never invents a failure: findings on stdout fail the file, and anything else
    (including a ruff that will not start) leaves the compile() check as the whole
    verdict — the run_tests rule that a missing runner must not masquerade as a
    failure applies here too.
    """
    import subprocess
    import sys

    if fp.suffix.lower() != ".py":
        return {"file": str(fp), "passed": True, "error_message": ""}
    try:
        compile(fp.read_text(encoding="utf-8"), str(fp), "exec")
    except SyntaxError as e:
        return {"file": str(fp), "passed": False, "error_message": f"SyntaxError: {e}"}
    except OSError as e:
        return {"file": str(fp), "passed": False, "error_message": f"Cannot read file: {e}"}

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(fp), "--quiet", "--no-cache",
             "--select", "E9,F63,F7,F82"],
            capture_output=True, text=True, timeout=60)
        findings = (proc.stdout or "").strip()
    except Exception as e:
        return {"file": str(fp), "passed": True,
                "error_message": f"ruff unavailable ({e}); syntax checked only"}
    if findings:
        return {"file": str(fp), "passed": False, "error_message": findings}
    return {"file": str(fp), "passed": True, "error_message": ""}


# ── skillflow integration ────────────────────────────────────────────

_skillflow_instance = None
_tool_loader_instance = None
_config_registry_instance = None
_agent_configs_cache: dict[str, dict] = {}

# Serialises the ONE-AND-ONLY engine boot (see get_skillflow). RLock, not Lock:
# the boot body calls `get_config_registry`, capability definitions and the
# generated-config loaders, any of which could ask for the engine again on this
# same thread, and a plain Lock would deadlock the boot against itself.
_skillflow_boot_lock = threading.RLock()


def _load_and_register_agent_configs(sf):
    """Load agent configs from YAML and register them into skillflow.

    Agent roles are keyed by their YAML map key and must be globally unique
    across all agent_configs/*.yaml — skillflow's registry is flat, so a
    collision would silently shadow one config (last-wins). With config
    registration now a glob, that risk grows, so we fail loudly at startup.
    """
    import yaml
    from pathlib import Path
    conf_dir = Path(__file__).resolve().parent.parent / "agent_configs"
    if not conf_dir.exists():
        return
    seen: dict[str, str] = {}          # role name -> file it was first declared in
    collisions: list[str] = []
    for f in sorted(conf_dir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            if name in seen and seen[name] != f.name:
                collisions.append(
                    f"agent role '{name}' declared in both "
                    f"'{seen[name]}' and '{f.name}'"
                )
            seen.setdefault(name, f.name)
            sf.register_agent_config_from_dict(name, cfg)
    if collisions:
        raise RuntimeError(
            "Startup aborted: duplicate agent role names across agent_configs/*.yaml "
            "(roles must be globally unique):\n  " + "\n  ".join(collisions)
        )


def _validate_graph_agent_configs(sf):
    """Verify every agent_config reference in every registered graph
    resolves to a registered agent config.  Raises RuntimeError at
    startup if any are missing.
    """
    missing: list[str] = []
    for graph_name, graph in sf._graphs.items():
        for node in graph.steps:
            if node.agent_config and node.agent_config not in sf.agent_registry:
                missing.append(
                    f"graph='{graph_name}' step='{node.id}' "
                    f"agent_config='{node.agent_config}'"
                )
    if missing:
        raise RuntimeError(
            "Startup aborted: agent_config references in registered "
            "graphs have no matching entry in agent_configs/*.yaml:\n  "
            + "\n  ".join(missing)
        )


def get_skillflow():
    """FastAPI dependency injection: get the skillflow orchestrator singleton.

    Created lazily on first access so that the DB and workspace managers
    are already initialised.

    ONE boot per process, guaranteed by ``_skillflow_boot_lock``. That lock
    became load-bearing the moment the boot moved off the request path
    (api/main.py's lifespan starts `warm_first_dashboard_read`): the prefetch
    thread and the first request that needs the engine would otherwise both see
    `None` and each construct a full AItelierSkillFlow (tool loader scan,
    capability definitions, graph registration — 4.5 s, and its own copy of every
    registry), with one of the two silently thrown away. The check-then-set was
    already racy for two concurrent requests; the lock makes the prefetch safe to
    have.
    """
    global _skillflow_instance
    if _skillflow_instance is not None:
        return _skillflow_instance
    with _skillflow_boot_lock:
        if _skillflow_instance is not None:
            return _skillflow_instance
        return _boot_skillflow()


def _boot_skillflow():
    """Build the singleton. The caller holds ``_skillflow_boot_lock``."""
    global _skillflow_instance
    if _skillflow_instance is None:
        from skillflow import PipelineGraph
        from core.skillflow_host import AItelierSkillFlow
        from core.output_migration import require_output_engine
        require_output_engine()
        from pathlib import Path
        from core.config_registry import ConfigRegistry

        tool_loader = get_tool_loader()
        # NOTE: skillflow's artifact_history is ON by default (>=1.5.11): each
        # promoted step output is git-versioned at the workspace root, so a
        # goal-loop re-run of a step no longer wipes the prior output —
        # recoverable via sf.step_output_versions() for tracing. Pass
        # artifact_history=False here to disable.
        #
        # stale_threshold_seconds: how long a claimed step may stay SILENT
        # before the reaper resets it to pending. Silence, not runtime —
        # skillflow heartbeats updated_at from every trace(), throttled to one
        # write per 25s per step — and skillflow widens the window per-node for
        # TOOL steps only, so every AGENT step gets exactly this flat number.
        # 60 was 2.4 heartbeat intervals: one long reasoning completion with no
        # intervening tool call could outlast it, and a premature reap
        # re-dispatches the step while the original executor is still writing,
        # whose eventual confirm_step then dies on "version mismatch". 180 is
        # ~7 intervals — room for a slow completion — and still well under
        # skillflow's own 300 default, so a truly dead worker is recovered
        # inside three minutes rather than five.
        sf = AItelierSkillFlow(
            SKILLFLOW_DB_PATH, tool_loader=tool_loader, workspace_base=WS_PATH,
            projects_base=PROJECTS_PATH, stale_threshold_seconds=180,
            code_path_resolver=_existing_repo_code_path,
            trace_db_path=WS_PATH)

        # Capability registry: a step's `capability` keyword → framework-
        # provisioned toolset + injected context. LEAST PRIVILEGE — neither the
        # pipeline author nor the agent picks a tool's write folder or its
        # toolset; the framework hands them over, keyed on the step's declared
        # purpose. `stateful` gives a persisting tool a durable, per-config,
        # MOUNTED state_dir (survives across runs AND container recreation) — it
        # replaces a generated tool hardcoding an un-mounted ~/.aitelier path.
        # `tool_creation` grants a tool-building step the authoring/registration
        # toolset.
        # Every definition goes through core.capability_registry, which holds the
        # invariants (a grant's tools must resolve; a name is owned; an offered
        # capability cannot be archived) — the same reason core/model_registry.py
        # is the only writer of the routing tables.
        from core import capability_registry as _caps
        project_root_for_caps = Path(__file__).resolve().parent.parent
        def _must_define(**kw):
            # define() reports rather than raises, so an ignored return value is
            # a capability that quietly does not exist — every step declaring it
            # then runs with nothing, which is precisely the failure the registry
            # was built to make impossible.
            # host=True is the ONE caller allowed to (re)define a host-owned
            # capability: this runs on every boot with the same definitions.
            r = _caps.define(sf, host=True, **kw)
            if not r.get("ok"):
                import logging
                logging.getLogger(__name__).error(
                    "capability %s NOT registered: %s", kw.get("name"),
                    r.get("error"))
            return r

        # The briefing's first line is what every listing shows. Without one the
        # palette row for `stateful` no longer names `state_dir` — the single
        # most important word about it.
        _must_define(name="stateful", owner="host",
                     briefing="Hands this step's tools a durable, per-pipeline, "
                              "MOUNTED `state_dir` kwarg. The tool writes "
                              "RELATIVE to it and never computes its own path: a "
                              "home-relative path escapes the mount and is lost "
                              "on the next container rebuild.",
                     context_provider=lambda cfg: {
                         "state_dir": str(sf._workspace.state_dir(cfg))})
        _must_define(name="tool_creation", owner="host",
                     briefing="Lets this step author and register a new tool. "
                              "Registration is the import check: `register_tool` "
                              "loads the module the way ToolLoader will and "
                              "refuses to publish one that does not import, so "
                              "nothing broken reaches a later step. Write the "
                              "test file alongside the impl — it ships with the "
                              "tool and runs later; this step cannot execute it.",
                     # register_capability alongside register_tool: a tool that
                     # needs framework-chosen state (or that only some loop items
                     # should carry) has nowhere to attach without a capability,
                     # and a maker with no way to declare one writes the grant by
                     # hand onto a role instead.
                     #
                     # NO `pytest`. It was granted here so the step could
                     # self-test, and it cannot: `pytest` resolves its `file`
                     # against `workspace_root`, which the engine fills from
                     # `project_root` — the CODE REPOSITORY — while the file the
                     # step just wrote is in the step's STAGING dir, reported
                     # back as a workspace-relative path. There is no argument
                     # the agent can pass that reaches it.
                     #
                     # Worse, granting it is unsafe on a deployed engine we do
                     # not control. skillflow's `pytest` is a NATIVE tool, so its
                     # guard ships with the PyPI package, not with this repo: the
                     # container currently runs 1.5.46, whose body is
                     # `full = (Path(workspace_root) / file).resolve()` with no
                     # guard at all. `tool_creation` is held by pipeline_forge's
                     # `t_tool_impl`, and pipeline_forge is `repo_mode: none`, so
                     # `_exec_tool` sends `project_root=""` and 1.5.46 copies that
                     # into `workspace_root` — making the root `/app`, the
                     # bind-mounted AItelier checkout. Execution still needs
                     # `full.exists()`, so a hit needs a name collision, but the
                     # ROOT is already wrong. See
                     # tests/unit/test_a_capability_grant_survives_the_deployed_engine.py
                     tools=["write", "run_tests", "register_tool",
                            "register_capability"])
        # game_assets: the tools AND the discipline that keeps their output
        # usable. Both used to ride on every DPE implementer — measured across
        # 22 workspaces, 6,996 tool calls, ZERO uses, 55% of that step's tool
        # schema budget, re-sent every turn. Now they reach only the task cards
        # that declare it.
        _ga = (project_root_for_caps / "configs" / "addons" / "game_harness"
               / "game_assets_briefing.md")
        if not _ga.is_file():
            import logging
            logging.getLogger(__name__).error(
                "game_assets briefing missing at %s — the tools would be granted "
                "with none of the discipline that keeps their output usable", _ga)
        _must_define(name="game_assets", owner="addon:game_harness",
                     tools=["gen_image_asset", "gen_audio_asset"],
                     briefing=_ga.read_text(encoding="utf-8") if _ga.is_file() else "")

        # Custom lint backends are consulted BEFORE built-ins, so this replaces
        # skillflow's `ruff` — the name t_impl's importability gate dispatches to
        # — with a check-only one. See _ruff_check_only for why the built-in's
        # auto-fix pass cannot be allowed to run on delivered code.
        from skillflow.lint_backends import register_backend
        register_backend("ruff", _ruff_check_only)
        # eslint for .js — the architect's linter_manifest.json names it. Built
        # 2026-07-02 and never merged: main shipped only the registry, so JS got
        # the `basic` (non-empty) check while the template promised a linter.
        from aitelier.lint_backends import register_all as _register_lint_backends
        _register_lint_backends()

        # Register agent configs into skillflow so graph validation catches
        # missing agent_config references at startup.
        _load_and_register_agent_configs(sf)

        project_root = Path(__file__).resolve().parent.parent

        # Register every host graph in configs/*.yaml (agent_config refs validated
        # below). No graph name is special-cased — drop a new config in the dir and
        # it is registered automatically. These are the ENGINE-AGNOSTIC bases; addon
        # overlays (configs/addons/) are composed onto them separately, below.
        configs_dir = project_root / "configs"
        if configs_dir.exists():
            for cfg_path in sorted(configs_dir.glob("*.yaml")):
                sf.register_graph(PipelineGraph.from_yaml(cfg_path))

        # skillflow's built-in `skill_converter` is DEPRECATED — `generate_pipeline`
        # now runs the grounded `pipeline_forge` generator (the built-in was
        # ReAct-shaped, ungrounded + lint-only, and hallucinated tools; see
        # design/pipeline_forge.md). skillflow persists registered graphs in its DB,
        # so a prior boot's registration lingers — drop the stale row so it can't
        # clutter the catalog or be run by mistake.
        try:
            # Under the engine's own connection lock. Two reasons, one lock:
            # the connection is shared by every scheduler thread
            # (check_same_thread=False) and its statement cache is keyed by SQL
            # text, so an unlocked statement can be handed the same prepared
            # statement another thread is stepping; and this is a COMMIT —
            # without the lock it can land inside a transaction another thread
            # opened (`with sf._lock, sf._conn:` in core/run_driver.py).
            with sf._lock:
                sf._conn.execute("DELETE FROM skillflow_graphs WHERE name = 'skill_converter'")
                sf._conn.commit()
        except Exception:
            pass

        # Register skillflow's addon_converter graph (still used by generate_addon):
        # turns a capability description + a base into a validated addon overlay,
        # driven by the butler's generate_addon tool. Its agents are registered in
        # Python (like skill_converter), and its acceptance test is the native
        # compose_validate tool (auto-loaded from skillflow's tools dir).
        try:
            import skillflow as _sf_pkg
            from skillflow.plugins.skill_converter import _register_addon_converter_agents
            _register_addon_converter_agents(sf)
            addon_conv_path = (Path(_sf_pkg.__file__).parent / "plugins"
                               / "skill_converter" / "addon_converter.yaml")
            if addon_conv_path.exists():
                sf.register_graph(PipelineGraph.from_yaml(addon_conv_path))
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "addon_converter graph not registered (addon authoring "
                "will be unavailable): %s", e
            )

        # Verify all agent_config references resolve (belt-and-suspenders over
        # skillflow's own _check_agent_configs — gives clearer error messages).
        _validate_graph_agent_configs(sf)

        _skillflow_instance = sf
        # Build the config registry once skillflow knows every graph.
        global _config_registry_instance
        _config_registry_instance = ConfigRegistry.build(sf)

        # Register addon aliases: each addon (configs/addons/) that declares an
        # `alias` is composed onto its `base:` and registered as that name (e.g.
        # game_harness → dpe_game), so the blessed base+addon combo is runnable.
        # Ad-hoc combos use core.addon_registry.register_addon_combo at run time.
        try:
            from core.addon_registry import load_addon_aliases
            load_addon_aliases(sf, _config_registry_instance)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("addon aliases not loaded: %s", e)

        # Register previously-generated capability definitions
        # (~/.AItelier/capabilities/*.json), so a capability the forge authored
        # survives restart exactly like a generated tool or pipeline. Non-fatal.
        try:
            _caps.load_generated(sf)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "generated capabilities not loaded: %s", e)

        # Register previously-generated pipelines (gen_*.yaml in ~/.AItelier/configs)
        # so they survive restart and are runnable by name. Non-fatal.
        try:
            from core.pipeline_registry import load_generated_configs
            load_generated_configs(sf, _config_registry_instance)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "generated pipelines not loaded: %s", e)

        # Register previously-generated addons (~/.AItelier/configs/addons/*.yaml)
        # authored via addon_converter, so they survive restart and their blessed
        # alias combos are runnable by name. Non-fatal.
        try:
            from core.addon_registry import load_generated_addons
            load_generated_addons(sf, _config_registry_instance)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "generated addons not loaded: %s", e)
    return _skillflow_instance


def register_pipeline_from_run(run_id: str, name: str) -> dict:
    """Persist + live-register the pipeline a completed generator run made, so it
    can be launched immediately via ``start_config_run``. Dispatches by the run's
    generator: ``pipeline_forge`` (graph + role_table + templates) vs
    ``skill_converter`` (single YAML). Returns ``{config_name, path, action}`` or
    ``{error}``."""
    from core.pipeline_registry import (register_generated_pipeline,
                                        register_forge_pipeline)
    sf = get_skillflow()
    run = sf.get_run(run_id) or {}
    if run.get("graph_name") == "pipeline_forge":
        return register_forge_pipeline(sf, get_config_registry(), run_id, name)
    return register_generated_pipeline(sf, get_config_registry(), run_id, name)


def register_addon_from_run(run_id: str) -> dict:
    """Persist + live-register the addon overlay a completed addon_converter run
    made, so its blessed combo can be launched immediately. Returns
    ``{addon_name, base, action, path, registered_config?}`` or ``{error}``."""
    from core.addon_registry import register_addon_from_run as _bridge
    return _bridge(get_skillflow(), get_config_registry(), run_id)


def get_config_registry():
    """FastAPI dependency injection: the ConfigRegistry singleton.

    Holds a ConfigManifest per registered graph (labels, checkpoints, scheduler
    ownership, …) so the scheduler, API and dashboards can drive and render runs
    of any config generically. Built lazily alongside the skillflow singleton.
    """
    global _config_registry_instance
    if _config_registry_instance is None:
        get_skillflow()  # builds the registry as a side effect
    return _config_registry_instance


def get_tool_loader():
    """FastAPI dependency injection: get the ToolLoader singleton.

    Searches skillflow native tools first, then AItelier custom tools. The
    loader is the host subclass, so host routes (`core.tool_routes`) apply to
    every engine dispatch of a routed name and survive cache reloads.
    """
    global _tool_loader_instance
    if _tool_loader_instance is None:
        from core.tool_routes import AItelierToolLoader
        from pathlib import Path

        import skillflow
        skillflow_tools = Path(skillflow.__file__).parent / "tools"
        loader = AItelierToolLoader(skillflow_tools)
        # AItelier custom tools
        custom = Path(__file__).resolve().parent.parent / "aitelier" / "tools"
        if custom.exists():
            loader.add_tools_dir(custom)
        # Generated tools authored by pipeline_forge (persisted, boot-scanned) —
        # mirrors how ~/.AItelier/configs holds generated graphs. register_tool
        # drops <name>/{tool.yaml,impl.py} here; adding the dir makes them resolve
        # in list_tools()/load_fn() on the next boot without a code change.
        gen_tools = datadir.tools_dir()
        gen_tools.mkdir(parents=True, exist_ok=True)
        loader.add_tools_dir(gen_tools)
        _tool_loader_instance = loader
    return _tool_loader_instance


def get_agent_configs() -> dict:
    """Return merged agent configs from agent_configs/ directory."""
    global _agent_configs_cache
    if not _agent_configs_cache:
        import yaml
        from pathlib import Path
        conf_dir = Path(__file__).resolve().parent.parent / "agent_configs"
        if conf_dir.exists():
            for f in conf_dir.glob("*.yaml"):
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _agent_configs_cache.update(data)
    return _agent_configs_cache


# ── The per-project enrichment memo ──────────────────────────────────
#
# One `GET /api/runs` cache rebuild measured 26874 ms on the live backend
# (2026-11-18, `/health` process.started_at 2026-11-18T13:47:36.354+00:00), and
# while it ran a `GET /api/projects` — a different cache key — took 26.22 s
# against a `GET /health` of 2.8 ms. One interpreter, one GIL: every other read
# the page makes queues behind this loop. Neither the DB nor the registry is the
# cost (`list_projects_with_stats(None)` returns 1545 rows in 0.10 s; the YAML
# registry is 0.00 ms per call warm). The cost is `enrich_project_status` at
# 4.1 ms per row over all 1545 rows, 10.6 ms per row over the 200 most recently
# active: a run lookup, a trace query for the failure reason, a config manifest
# lookup and a `get_steps` call — per row, per pass — and three endpoints rebuild
# the same list from the same rows on a 10 s dashboard poll.
#
# Two snapshots of all 1545 rows 12 s apart differed in ZERO rows by
# (status, updated_at, current_project_step). The scheduler mirrors every run
# change into the project row, so an unchanged row is the common case by a wide
# margin, and there is nothing in it left to recompute.
#
# So the DERIVED fields are memoised on the row's own change fields. Three
# bounds keep that honest:
#
#   the key     project_id / status / updated_at / current_project_step /
#               config_name — the mirror fields the scheduler writes. Any
#               scheduler write to the row changes the key.
#   the TTL     a BACKSTOP for change the mirror row never records — not the
#               freshness mechanism. The key already catches everything the
#               scheduler has recorded, so the only thing the TTL bounds is
#               out-of-band change: a run that advanced between two mirror
#               writes and is being read in that window (one scheduler tick), a
#               writer outside this process, an engine-side fixup.
#   the engine  every value here was read from ONE skillflow object. A different
#               one from `get_skillflow()` (a rebuilt singleton, a test double)
#               is a different database of runs, and its entries are not reused.
#
# What is stored is the derived fields ONLY — never a dict handed to a caller.
# Callers keep mutating the result (`project_routers` sets config_label /
# is_authoring / has_task_loop, `repo_routers` calls this for its side effect),
# so each call applies a fresh copy onto its own dict and no caller can poison
# the next one's answer.
#
# Why 300 s, and why the read cache's invalidation does NOT drop this memo.
#
# `api/_read_cache.clear()` runs after every non-GET request (the middleware at
# api/main.py:442) and each of those writes now costs the read cache one
# background rebuild instead of a cold inline miss. That rebuild walks all 1545
# rows again — so whatever the memo's backstop is, the number of full passes per
# hour is `3600 / backstop` no matter how often somebody clicks. One full pass
# costs 8.9-28.1 s of this process's single core (measured 2026-11-18), so at the
# old 30 s the worst case was 94% of the core spent re-deriving rows that had
# changed nothing (28.1/30), and even the best case 30% (8.9/30). That is the
# complaint: using the page is what made its reads expensive.
#
# 300 s is chosen against what the key already guarantees. Every change the
# scheduler makes to a run is mirrored onto the project row, and any change to
# those five fields is a key miss — so the backstop is paid for change the host
# never recorded, which is rare by construction and needs no tighter bound than
# "the page is told within five minutes even then". At 300 s the fixed cost is
# 3-9.4% of one core (8.9/300 .. 28.1/300) instead of 30-94%, and it is ~11 full
# passes' worth of slack over the ~27 s a cold first pass takes, so a backstop
# can never expire entries it has just written.
#
# So: TTL = backstop for out-of-band change; the key = freshness; the engine
# identity = correctness. A write does not drop this memo, and dropping it on
# every write is exactly the defect — one click would buy back the whole 8.9-28.1
# s pass. (`clear_enrichment_memo()` exists for tests; the middleware must not
# call it, and does not.)
#
# Two seams make it testable without sleeping: `enrich_project_status(...,
# ttl=...)` for one call, and the `ENRICH_MEMO_TTL` module global for the default.
ENRICH_MEMO_TTL = 300.0

# One entry per distinct row state, and rows are user data: a project edited
# 3000 times would otherwise leave one entry per version forever. The read
# cache's pattern — drop everything at the cap — is right here too: losing the
# memo costs one recomputation, and a real eviction policy would be more code
# than the thing it protects.
ENRICH_MEMO_MAX_KEYS = 8192

# key -> (stored_at, engine, derived). `stored_at` is time.monotonic().
_enrichment_memo: dict[tuple, tuple[float, object, dict]] = {}
_enrichment_locks: dict[tuple, threading.Lock] = {}
_enrichment_guard = threading.Lock()


def clear_enrichment_memo() -> None:
    """Drop every memoised enrichment. For tests, and for a writer that knows.

    Deliberately NOT wired to `api/main.py`'s invalidation middleware. That
    middleware clears the READ cache after every mutating request, and if it
    dropped this memo too, every click would re-pay one full 1545-row pass
    (8.9-28.1 s of this process's single core) — the defect the read cache's
    stale-while-rebuilding path exists to remove. A write that actually changes a
    row changes that row's key (`_enrichment_memo_key`), so it misses on its own;
    nothing else needs to be dropped. See the block above.
    """
    with _enrichment_guard:
        _enrichment_memo.clear()
        _enrichment_locks.clear()


def _enrichment_memo_key(project: dict) -> tuple:
    """The row's OWN change fields — the scheduler's mirror of a run's state.

    Deliberately not the whole row: `title`, `owner_email` and friends change
    without changing anything skillflow knows, and a key that moved for them
    would miss for no reason.
    """
    return (project.get("project_id"), project.get("status"),
            project.get("updated_at"), project.get("current_project_step"),
            project.get("config_name"))


def _enrichment_lookup(key: tuple, sf, ttl: float) -> dict | None:
    with _enrichment_guard:
        hit = _enrichment_memo.get(key)
    if hit is None:
        return None
    stored_at, engine, derived = hit
    if engine is not sf or time.monotonic() - stored_at >= ttl:
        return None
    return derived


def _enrichment_lock_for(key: tuple) -> threading.Lock:
    """The per-key lock, so N threads arriving on one cold key compute it once.

    These endpoints are called from AnyIO worker threads; without this, a cold
    project list under three concurrent requests runs the same 1545 enrichments
    three times over, which is the stampede `api/_read_cache` exists to prevent
    one layer up.
    """
    with _enrichment_guard:
        lock = _enrichment_locks.get(key)
        if lock is None:
            if len(_enrichment_locks) >= ENRICH_MEMO_MAX_KEYS:
                _enrichment_memo.clear()
                _enrichment_locks.clear()
            lock = _enrichment_locks[key] = threading.Lock()
        return lock


def _enrichment_store(key: tuple, sf, derived: dict) -> None:
    with _enrichment_guard:
        if len(_enrichment_memo) >= ENRICH_MEMO_MAX_KEYS:
            _enrichment_memo.clear()
            _enrichment_locks.clear()
        _enrichment_memo[key] = (time.monotonic(), sf, derived)


def _copy_derived(derived: dict) -> dict:
    """A copy of the memoised fields that one caller may keep and mutate.

    The containers (`completed_project_steps`, `config_addons`) are copied too: a
    caller appending to them must not edit the next caller's answer. Everything
    else in there is a str/bool, which cannot be mutated.
    """
    return {k: (copy.deepcopy(v) if isinstance(v, (list, dict, set)) else v)
            for k, v in derived.items()}


def _derive_project_enrichment(project: dict, sf) -> tuple[dict, bool]:
    """The expensive part: read one project's pipeline state out of skillflow.

    Returns ``(derived_fields, complete)`` and NEVER the caller's dict, so the
    result can be memoised and re-applied to a fresh dict per call. `complete` is
    False when skillflow raised part-way: the fields collected up to that point
    are still applied (that is what the old in-place code left behind, and a
    transient failure must not blank a row that was already enriched), but the
    partial answer is not memoised — the next pass gets to try again.
    """
    derived: dict = {}
    complete = True
    try:
        # get_run_by_project excludes completed runs, so a finished project would
        # otherwise keep its last cached status (e.g. "running:5") forever. Use the
        # most recent run of ANY status as the source of truth.
        run = sf.get_run_by_project(project["project_id"])
        if not run:
            all_runs = sf.list_runs(project["project_id"])  # newest first
            run = all_runs[0] if all_runs else None
        if run:
            # Surface the run's config identity so clients can tell which config a
            # run belongs to and render its labels/checkpoints generically.
            cfg = run.get("graph_name") or project.get("config_name") or "dpe_default_v2"
            derived["config_name"] = cfg
            try:
                from core.addon_registry import describe_config
                _d = describe_config(cfg)
                derived["config_base"] = _d["base"]
                derived["config_addons"] = _d["addons"]
            except Exception:
                derived["config_base"] = cfg
                derived["config_addons"] = []
            try:
                manifest = get_config_registry().get(cfg)
            except Exception:
                manifest = None
            derived["config_label"] = manifest.label if manifest else cfg
            has_task_loop = bool(manifest and manifest.has_task_loop)

            # AT-15: preserve the DB's enriched status (e.g. "running:3")
            # over skillflow's raw status ("running"). The scheduler writes
            # the detailed status; only fall back to run["status"] if the DB
            # column hasn't been synced yet.
            #
            # The enrichment is "<raw>:<detail>", so the DB value is authoritative
            # exactly when its prefix still agrees with skillflow. That covers
            # "failed:<why>" too — which this used to throw away, leaving the user
            # with a bare "failed" even though the scheduler had already written
            # "failed:Cycle limit exceeded — v_smoke: ...". A DB status whose prefix
            # DISAGREES is stale (e.g. a reactivated run), so skillflow wins.
            # The reason, ENRICHED — never the raw column alone. skillflow's
            # `error_reason` is often a framework artifact that names the edge and
            # not the cause: a novel chapter died with "Cycle limit exceeded" while
            # the tool that actually rejected it had said
            # "continuity_check 未通过: 字数超限 5662 字（上限 4500）". The scheduler
            # already resolves that (`_failure_reason` → `_last_trace_error`), so
            # serving the raw column here would hide a tool result the host had
            # already dug out — reintroducing the defect one layer further along.
            # Reconciliation below can also discard the DB's enriched status when
            # its prefix disagrees, so this must not depend on which producer won.
            try:
                from core.scheduler import _failure_reason
                derived["error_reason"] = _failure_reason(run) if (
                    run.get("status") == "failed") else (run.get("error_reason") or "")
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "could not resolve the enriched failure reason", exc_info=True)
                derived["error_reason"] = run.get("error_reason") or ""
            run_status = run["status"]
            db_status = project.get("status") or ""
            if run_status == "running" and run.get("current_node"):
                derived["status"] = f"running:{run['current_node']}"
            elif db_status.split(":", 1)[0] == run_status and db_status != run_status:
                pass                      # keep the enriched value
            else:
                derived["status"] = run_status
            derived["current_project_step"] = run["current_node"] or ""
            # completed steps from skillflow, not from cached column
            steps = sf.get_steps(run["id"])
            derived["completed_project_steps"] = [
                s["step_id"] for s in steps if s["status"] == "completed"
            ]
            derived["has_task_loop"] = has_task_loop
        elif not project.get("status"):
            derived["status"] = "planning"
    except Exception:
        complete = False
    return derived, complete


def _memoised_enrichment(project: dict, sf, ttl: float) -> dict:
    """The derived fields for `project`, from the memo when it is still valid."""
    key = _enrichment_memo_key(project)
    derived = _enrichment_lookup(key, sf, ttl)
    if derived is not None:
        return derived
    with _enrichment_lock_for(key):
        # Re-check under the lock: whoever held it may have just filled the
        # entry, and that is the whole point — one computation, N readers.
        derived = _enrichment_lookup(key, sf, ttl)
        if derived is not None:
            return derived
        derived, complete = _derive_project_enrichment(project, sf)
        if complete:
            _enrichment_store(key, sf, derived)
        return derived


def enrich_project_status(project: dict | None, ttl: float | None = None) -> dict | None:
    """Augment a project dict with pipeline status from skillflow (source of truth).

    In place, on the caller's own dict, exactly as before — `repo_routers` relies
    on the side effect and the other callers on the return value. What changed is
    that the fields come from a memo keyed on the row's own change fields
    (see the block above): a second pass over unchanged rows does no skillflow
    work at all, instead of one run lookup, one trace query, one manifest lookup
    and one `get_steps` call per row.

    `ttl` overrides `ENRICH_MEMO_TTL` for this call — the seam a test uses to
    cross the backstop without sleeping for it (`ttl=0.0` crosses it for one
    call). It is a BACKSTOP for out-of-band change, not the freshness
    mechanism; see the block above.
    """
    if not project:
        return None
    try:
        sf = get_skillflow()
        project.update(_copy_derived(_memoised_enrichment(
            project, sf, ENRICH_MEMO_TTL if ttl is None else ttl)))
    except Exception:
        # This runs on every project list; it must never break the page. Same
        # contract as before the memo: whatever could be resolved is applied and
        # anything else is left as the DB returned it.
        pass
    return project


# ── Ownership helpers (auth-optional: no-op when user=None) ──


def owner_filter(user: CurrentUser | None, request: Request) -> str | None:
    """
    Return the owner_email filter for DB queries.
    - user=None (CLI): return None (see all rows, defaults to 'cli@local')
    - user set, normal mode: return user.email
    - user set, demo mode: return None (see all rows)
    """
    if user is None:
        return None
    mode = getattr(request.app.state, "mode", "normal")
    return user.email if mode == "normal" else None


def check_write_owner(user: CurrentUser | None, resource: dict):
    """
    Raise 404 if user is authenticated and does not own the resource.
    No-op when user=None (CLI mode — all resources accessible).
    """
    if user is not None and resource.get("owner_email") != user.email:
        raise HTTPException(status_code=404, detail="Not found")


def check_read_owner(user: CurrentUser | None, request: Request, resource: dict):
    """
    Raise 404 if user is authenticated, in normal mode, and does not own the resource.
    No-op when user=None (CLI) or demo mode.
    """
    if user is None:
        return
    mode = getattr(request.app.state, "mode", "normal")
    if mode == "normal" and resource.get("owner_email") != user.email:
        raise HTTPException(status_code=404, detail="Not found")