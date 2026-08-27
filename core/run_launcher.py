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


def missing_cross_config_inputs(sf, config_name: str, project_id: str) -> list[dict]:
    """Required inputs *config_name* imports from ANOTHER config's run and that
    do not exist yet under *project_id*.

    A ``{config: X, step: Y, output: Z, required: true}`` context source is a
    PRECONDITION, not an input this run can produce: the artifact is written by a
    run of X, and no step of this graph will ever create it. dpe_default_v2's
    step "1" imports ``meta_conversation/finalize/step1_goals.json`` that way, so
    a dpe_* run started without its meta conversation is dead at its first claim.
    Same-config sources are excluded — those are the run's own seed/step output.

    Existence is decided by skillflow's OWN resolver (``ContextResolver`` and the
    ``RequiredContextMissing`` it raises for an empty required source), never by
    re-deriving the path math here: a private copy that drifted would start
    refusing launches skillflow would have served, and admitting ones it would
    not. Each entry is ``{config, step, output, reader}``.
    """
    from skillflow.context import ContextResolver
    from skillflow.exceptions import RequiredContextMissing

    graph = sf._get_resolver(config_name).graph
    resolver = ContextResolver(sf._workspace.get_project_path(project_id))
    missing: list[dict] = []
    seen: set[tuple] = set()
    for node in graph.steps:
        for spec in (node.context or []):
            source = spec.get("source", spec)
            producer = source.get("config") or ""
            if not (spec.get("required") or source.get("required")):
                continue
            if not producer or producer == config_name:
                continue
            key = (producer, source.get("step", ""), source.get("output", ""))
            if key in seen:
                continue
            seen.add(key)
            try:
                resolver.resolve([spec], current_config=config_name)
            except RequiredContextMissing:
                missing.append({"config": producer, "step": source.get("step", ""),
                                "output": source.get("output", ""), "reader": node.id})
    return missing


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

    # Repo-ness is DECLARED by the config (manifest.repo_mode), not inferred from
    # what the config registers. A config that emits an artifact instead of code
    # (authoring converters, most generated pipelines) declares repo_mode="none"
    # and gets a repo-less workspace — no repo_path, no throwaway
    # projects/<id>/.git — so it never surfaces as a "fake repo" on the
    # group-by-repo dashboard.
    eff_repo_type = "none" if manifest.repo_mode == "none" else repo_type

    if not db.get_project(project_id):
        # Compute default repo_path for new/clone, same as project_routers.py.
        rpath = repo_path
        if eff_repo_type in ("new", "clone") and not rpath:
            from core.datadir import projects_dir
            rpath = str(projects_dir() / project_id)
        db.ensure_project(project_id, name=name, owner_email=owner_email,
                          repo_type=eff_repo_type, repo_path=rpath,
                          repo_url=repo_url, config_name=config_name)
    elif manifest.scheduler_owned:
        # The project row already exists — point config_name at THIS config.
        # It was only ever set at creation, so a project created by one config
        # and then run under another kept the first name forever. That is not
        # cosmetic: get_next_active_project ends with
        # `AND config_name IN (<scheduler-owned configs>)`, so a project whose
        # row still said `meta_conversation` was invisible to the poller and sat
        # at its first step with the tick log reporting `idle` — no error
        # anywhere, because nothing was wrong except that nobody could see it.
        #
        # Live: jinyong-ux, 2026-08-22. Triggered by doing it in the RIGHT order
        # (meta_conversation first, then dpe_game); the earlier project only
        # worked because the two runs happened to be started backwards.
        #
        # Guarded on scheduler_owned so a butler-driven config
        # (meta_conversation, the converters) can never steal the name from the
        # build config the poller has to drive.
        db.update_project(project_id, config_name=config_name)
    if priority:
        db.update_project(project_id, priority=priority)

    # DPE (and its addon combos, e.g. dpe_game) keep the proven brief→step-1
    # seeding ritual — keyed on the DPE brief contract (seed_file), not a single
    # config name, so composed game pipelines seed the same way as the base.
    seed_inputs = seed_inputs or {}
    if manifest.seed_file == "project_brief.md" and isinstance(seed_inputs.get("brief"), dict):
        ws.setup_workspace(project_id, repo_type=seed_inputs.get("repo_type", repo_type),
                           repo_path=repo_path, repo_url=repo_url)
        from core.project_submit import seed_and_trigger
        result = seed_and_trigger(db, ws, project_id, seed_inputs["brief"])
        result.setdefault("config_name", config_name)
        result["scheduler_owned"] = manifest.scheduler_owned
        return result

    sf = get_skillflow()

    # A required cross-config input is a precondition this run cannot satisfy for
    # itself, so a launch that lacks one has already failed — say so here instead
    # of handing back a run id that will never claim a step. Checked before the
    # workspace is set up, so a refused launch leaves no half-built repo behind.
    #
    # Live 2026-08-23, jinyong-usable: POST /api/runs started dpe_game and
    # answered 201 {"status":"started"}. Thirty seconds later the run was failed,
    # and the whole trace was one scheduler tick line — `claim_terminal …
    # Required context source resolved to no content: finalize` — because no
    # meta_conversation run had ever produced step1_goals.json for that project.
    # The caller had every reason to believe a build was under way. The sibling
    # entry path (core/project_submit.py:seed_and_trigger) has refused exactly
    # this since a brief-less DPE run spun on the same message for 47 minutes;
    # the generic launcher never grew the guard, so it kept reporting success for
    # runs that had no first move.
    missing = missing_cross_config_inputs(sf, config_name, project_id)
    if missing:
        needs = "; ".join(
            f"{m['output']} (produced by config '{m['config']}'"
            + (f" step '{m['step']}'" if m["step"] else "")
            + f", read by step '{m['reader']}')"
            for m in missing)
        remedy = (
            "Run the meta conversation for this project first — start the build "
            "through the butler, which drives meta_conversation to finalize and "
            "then launches the pipeline."
            if any(m["config"] == "meta_conversation" for m in missing)
            else "Run the producing config for this project first, then start "
                 "this one.")
        return {"status": "error", "message":
                f"Cannot start '{config_name}' for project '{project_id}': it "
                f"requires input that only another config produces, and that "
                f"input does not exist: {needs}. {remedy}"}

    ws.setup_workspace(project_id, repo_type=eff_repo_type,
                       repo_path=repo_path, repo_url=repo_url)

    # Write seeds into the config's seed dir (read by the first step's
    # {from: config} context spec).
    #
    # A config that declares no `seed_file` has nowhere to put seed_text, and
    # the old code simply skipped the write — the caller got
    # {"status": "started"} and the text vanished. Live: meta_conversation
    # declares no seed_file (only dpe_default does), so
    # `start_config_run("meta_conversation", seed_text=<the whole brief>)`
    # reported success and launched a conversation that had never been told
    # what it was about. The caller cannot tell that from the reply, and the
    # run only looks wrong much later, in what the agent asks.
    #
    # Refusing is the honest answer: the caller asked for something this config
    # cannot do. Same shape as the missing-cross-config-input guard above —
    # fail at launch, not silently at runtime.
    files: dict[str, str] = {}
    if seed_text is not None:
        if not manifest.seed_file:
            return {"status": "error", "message":
                    f"Config '{config_name}' declares no `seed_file`, so there "
                    f"is nowhere to write seed_text ({len(seed_text)} chars) — "
                    f"it would be silently discarded. Pass the text through "
                    f"`seed_inputs={{'<filename>': ...}}` naming a file the "
                    f"first step actually reads, or start a config that "
                    f"declares a seed_file (dpe_default reads "
                    f"'project_brief.md')."}
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
