"""run_launcher — one entrypoint to start a run of ANY registered config.

Generalizes the proven DPE submit / skill_converter launch rituals into a single
``start_config_run`` so a new skillflow config becomes runnable (and visible in
the dashboards) with no bespoke wiring:

  * ensures the host run row (tagged with config_name) and workspace,
  * writes the seed input into the config's seed dir (read by the first step's
    ``{from: config}`` context), or delegates to the DPE brief→step-1 pre-hook,
  * creates + starts the skillflow run,
  * wakes the polling scheduler for scheduler-owned configs (DPE etc.); butler-
    driven configs (meta_conversation, skill_converter) are left for the butler
    to drive.

The DPE config keeps its exact, proven seeding path (``project_submit.seed_and_trigger``)
so nothing about the demo-critical build changes.
"""

import json
import re
import uuid


def slugify(text: str, *, sep: str = "-", maxlen: int = 40,
            fallback: str = "project") -> str:
    """Lowercase, collapse every run of non-alphanumerics to *sep*, strip, cap.

    Single source of truth for the project's name→slug logic (callers pick the
    separator/cap: ``-`` for run ids/pids, ``_`` for config/graph names)."""
    s = re.sub(r"[^a-z0-9]+", sep, (text or "").lower()).strip(sep)[:maxlen]
    return s or fallback


def generate_run_id(config_name: str) -> str:
    """A filesystem-safe project_id (run key) for a fresh run of a config."""
    slug = "".join(c if c.isalnum() else "-" for c in config_name.lower()).strip("-")
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def start_config_run(db, ws, config_name: str, project_id: str, *,
                     seed_text: str | None = None,
                     seed_inputs: dict | None = None,
                     name: str | None = None,
                     owner_email: str = "cli@local",
                     priority: int = 0,
                     repo_type: str = "new",
                     repo_url: str | None = None,
                     repo_path: str | None = None) -> dict:
    """Start a run of ``config_name`` keyed by ``project_id``.

    ``seed_text`` is written to the config's ``manifest.seed_file``; ``seed_inputs``
    is an optional ``{filename: content}`` map of extra seed files. For DPE, pass
    ``seed_inputs={"brief": <brief dict>}`` to take the proven brief→step-1 path.
    Returns ``{status, project_id, run_id, config_name, scheduler_owned}``.
    """
    from api.dependencies import get_skillflow, get_config_registry
    from core.scheduler import wake_scheduler

    manifest = get_config_registry().get(config_name)
    if manifest is None:
        return {"status": "error", "message": f"Unknown config '{config_name}'"}

    if not db.get_project(project_id):
        # Compute default repo_path for new/clone, same as project_routers.py
        rpath = repo_path
        if repo_type in ("new", "clone") and not rpath:
            from core.datadir import projects_dir
            rpath = str(projects_dir() / project_id)
        db.ensure_project(project_id, name=name, owner_email=owner_email,
                          repo_type=repo_type, repo_path=rpath,
                          repo_url=repo_url, config_name=config_name)
    if priority:
        db.update_project(project_id, priority=priority)

    # DPE keeps its proven brief→step-1 seeding ritual.
    seed_inputs = seed_inputs or {}
    if config_name == "dpe_default_v2" and isinstance(seed_inputs.get("brief"), dict):
        ws.setup_workspace(project_id, repo_type=seed_inputs.get("repo_type", repo_type),
                           repo_path=repo_path, repo_url=repo_url)
        from core.project_submit import seed_and_trigger
        result = seed_and_trigger(db, ws, project_id, seed_inputs["brief"])
        result.setdefault("config_name", config_name)
        result["scheduler_owned"] = manifest.scheduler_owned
        return result

    ws.setup_workspace(project_id, repo_type=repo_type,
                       repo_path=repo_path, repo_url=repo_url)
    sf = get_skillflow()

    # Write seeds into the config's seed dir (read by the first step's
    # {from: config} context spec).
    files: dict[str, str] = {}
    if seed_text is not None and manifest.seed_file:
        files[manifest.seed_file] = seed_text
    for fname, content in seed_inputs.items():
        files[fname] = content if isinstance(content, str) else json.dumps(content)
    if files:
        seed_dir = sf._workspace.get_config_path(project_id, config_name) / "_seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in files.items():
            (seed_dir / fname).write_text(content, encoding="utf-8")

    run_id = sf.get_or_create_run(config_name, project_id, {"project_id": project_id})
    run = sf.get_run(run_id)
    if run and run["status"] == "pending":
        sf.start_run(run_id)

    if manifest.scheduler_owned:
        wake_scheduler(owner_email if owner_email != "cli@local" else None)

    return {"status": "started", "project_id": project_id, "run_id": run_id,
            "config_name": config_name, "scheduler_owned": manifest.scheduler_owned}


def start_addon_run(db, ws, base: str, addons: list[str], project_id: str, **kwargs) -> dict:
    """run(base, [addons]) — compose a base with a list of addons and start it.

    Composes base + addons into a runnable config (an emergent name, or the
    single blessed alias if it is exactly one aliased addon), registers it, then
    delegates to start_config_run. Ad-hoc combos are registered on demand;
    already-registered ones (e.g. a boot alias) are reused idempotently.
    """
    from api.dependencies import get_skillflow, get_config_registry
    from core.addon_registry import register_addon_combo

    try:
        config_name = register_addon_combo(get_skillflow(), get_config_registry(), base, addons)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    return start_config_run(db, ws, config_name, project_id, **kwargs)
