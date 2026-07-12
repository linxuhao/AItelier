# File: api/dependencies.py

import os
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


def _existing_repo_code_path(project_id: str) -> str | None:
    """Code-path resolver handed to skillflow.

    skillflow's default layout keys the code repo by project_id
    (projects_base/<id>), which cannot point a project at an *existing* repo.
    For `repo_type='existing'` projects AItelier records the linked repo in the
    DB (`repo_path`); returning it here makes skillflow's lifecycle hooks
    (repo_apply commits the fix into the real repo), `from: repository` context,
    and project-level lint all target that repo. Returns None for new/clone
    projects so skillflow keeps its default projects_base/<id> location.
    """
    try:
        info = db_instance.get_repo_info(project_id)
        return info.get("repo_path") or None
    except Exception:
        return None


# ── skillflow integration ────────────────────────────────────────────

_skillflow_instance = None
_tool_loader_instance = None
_config_registry_instance = None
_agent_configs_cache: dict[str, dict] = {}


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
    """
    global _skillflow_instance
    if _skillflow_instance is None:
        from skillflow import SkillFlow, PipelineGraph
        from pathlib import Path
        from core.config_registry import ConfigRegistry

        tool_loader = get_tool_loader()
        # NOTE: skillflow's artifact_history is ON by default (>=1.5.11): each
        # promoted step output is git-versioned at the workspace root, so a
        # goal-loop re-run of a step no longer wipes the prior output —
        # recoverable via sf.step_output_versions() for tracing. Pass
        # artifact_history=False here to disable.
        sf = SkillFlow(SKILLFLOW_DB_PATH, tool_loader=tool_loader, workspace_base=WS_PATH,
                     projects_base=PROJECTS_PATH, stale_threshold_seconds=60,
                     code_path_resolver=_existing_repo_code_path,
                     trace_db_path=WS_PATH)

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

        # Register skillflow's skill_converter graph so the butler can generate a
        # new pipeline from a skill description via start_pipeline("skill_converter").
        # It ships inside the skillflow package (its agents are registered in
        # Python, not agent_configs/*.yaml), so it is registered explicitly rather
        # than via the configs/ glob.
        try:
            import skillflow as _sf_pkg
            from skillflow.plugins.skill_converter.converter import _register_converter_agents
            _register_converter_agents(sf)
            conv_path = (Path(_sf_pkg.__file__).parent / "plugins"
                         / "skill_converter" / "skill_converter.yaml")
            if conv_path.exists():
                sf.register_graph(PipelineGraph.from_yaml(conv_path))
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "skill_converter graph not registered (skill→pipeline conversion "
                "will be unavailable): %s", e
            )

        # Register skillflow's addon_converter graph (sibling of skill_converter):
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
    """Persist + live-register the pipeline a completed skill_converter run made,
    so it can be launched immediately via ``start_config_run``. Returns
    ``{config_name, path, action}`` or ``{error}``."""
    from core.pipeline_registry import register_generated_pipeline
    return register_generated_pipeline(
        get_skillflow(), get_config_registry(), run_id, name)


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

    Searches skillflow native tools first, then AItelier custom tools.
    """
    global _tool_loader_instance
    if _tool_loader_instance is None:
        from skillflow.tool_loader import ToolLoader
        from pathlib import Path

        import skillflow
        skillflow_tools = Path(skillflow.__file__).parent / "tools"
        loader = ToolLoader(skillflow_tools)
        # AItelier custom tools
        custom = Path(__file__).resolve().parent.parent / "aitelier" / "tools"
        if custom.exists():
            loader.add_tools_dir(custom)
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


def enrich_project_status(project: dict | None) -> dict | None:
    """Augment a project dict with pipeline status from skillflow (source of truth)."""
    if not project:
        return None
    try:
        sf = get_skillflow()
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
            project["config_name"] = cfg
            try:
                from core.addon_registry import describe_config
                _d = describe_config(cfg)
                project["config_base"] = _d["base"]
                project["config_addons"] = _d["addons"]
            except Exception:
                project["config_base"] = cfg
                project["config_addons"] = []
            try:
                manifest = get_config_registry().get(cfg)
            except Exception:
                manifest = None
            project["config_label"] = manifest.label if manifest else cfg
            has_task_loop = bool(manifest and manifest.has_task_loop)

            # AT-15: preserve the DB's enriched status (e.g. "running:3")
            # over skillflow's raw status ("running"). The scheduler writes
            # the detailed status; only fall back to run["status"] if the DB
            # column hasn't been synced yet.
            run_status = run["status"]
            if run_status == "running" and run.get("current_node"):
                project["status"] = f"running:{run['current_node']}"
            else:
                project["status"] = run_status
            project["current_project_step"] = run["current_node"] or ""
            # completed steps from skillflow, not from cached column
            steps = sf.get_steps(run["id"])
            project["completed_project_steps"] = [
                s["step_id"] for s in steps if s["status"] == "completed"
            ]
            project["has_task_loop"] = has_task_loop
        elif not project.get("status"):
            project["status"] = "planning"
    except Exception:
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