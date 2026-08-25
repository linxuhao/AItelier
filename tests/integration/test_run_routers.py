# Phase 4: generic /api/runs surface + run-id-keyed checkpoint delegation.


def test_list_all_runs_attaches_config_label(client):
    """GET /api/runs lists runs of any config with config label + has_task_loop."""
    client.post("/api/projects", json={"project_id": "run_list_proj", "name": "RunList"})

    resp = client.get("/api/runs")
    assert resp.status_code == 200, resp.text
    runs = {r["project_id"]: r for r in resp.json()["runs"]}
    assert "run_list_proj" in runs
    row = runs["run_list_proj"]
    assert row["config_name"] == "dpe_default_v2"
    assert row["config_label"] == "DPE Pipeline"
    assert row["has_task_loop"] is True


def test_list_runs_filter_by_config(client):
    """The config_name filter narrows the list."""
    client.post("/api/projects", json={"project_id": "filt_proj", "name": "Filt"})
    # No meta_conversation runs exist → empty, but DPE filter returns our run.
    assert client.get("/api/runs?config_name=meta_conversation").json()["runs"] == []
    dpe = client.get("/api/runs?config_name=dpe_default_v2").json()["runs"]
    assert any(r["project_id"] == "filt_proj" for r in dpe)


def test_unknown_run_detail_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_run_checkpoint_delegation_unknown_run_404(client):
    """Run-id checkpoint routes resolve run_id→project_id; unknown run → 404
    (same outcome as the project-keyed route for a missing run)."""
    assert client.get("/api/runs/does-not-exist/checkpoint").status_code == 404
    assert client.post("/api/runs/does-not-exist/checkpoint/approve",
                       json={"checkpoint": "1"}).status_code == 404


def test_start_run_unknown_config_404(client):
    """POST /api/runs rejects an unregistered config."""
    resp = client.post("/api/runs", json={"config_name": "no_such_config"})
    assert resp.status_code == 404


def test_run_detail_includes_cache_stats(client):
    """GET /api/runs/{run_id} includes cache_stats at run and step level."""
    client.post("/api/projects", json={"project_id": "cache_test_proj", "name": "CacheTest"})
    # dpe_default_v2 step "1" reads meta_conversation/finalize/step1_goals.json
    # as a REQUIRED cross-config input, so run_launcher refuses to start one for
    # a project that never had a meta conversation. Seed that artifact — the
    # same thing `finalize` writes — rather than dropping the start: this test
    # needs a real skillflow run to query, and POST /api/projects alone does not
    # create one.
    from api.dependencies import get_skillflow
    finalize = (get_skillflow()._workspace.get_project_path("cache_test_proj")
                / "meta_conversation" / "finalize")
    finalize.mkdir(parents=True, exist_ok=True)
    (finalize / "step1_goals.json").write_text(
        '{"goals": ["ship it"]}', encoding="utf-8")

    start_resp = client.post("/api/runs", json={
        "config_name": "dpe_default_v2",
        "project_id": "cache_test_proj",
    })
    assert start_resp.status_code == 201, start_resp.text
    resp = client.get("/api/runs/cache_test_proj")
    assert resp.status_code == 200
    data = resp.json()
    # Run-level cache_stats must be present
    assert "cache_stats" in data
    assert isinstance(data["cache_stats"], dict)
    assert "cache_hit_tokens" in data["cache_stats"]
    assert "cache_miss_tokens" in data["cache_stats"]
    assert "hit_ratio" in data["cache_stats"]
    assert "total_tokens" in data["cache_stats"]
    # With no usage traces, hit_ratio should be None, total_tokens 0
    assert data["cache_stats"]["hit_ratio"] is None
    assert data["cache_stats"]["cache_hit_tokens"] == 0
    assert data["cache_stats"]["total_tokens"] == 0
    # Per-step map must be present
    assert "cache_stats_by_step" in data
    assert isinstance(data["cache_stats_by_step"], dict)


def test_list_all_runs_includes_cache_stats(client):
    """GET /api/runs includes cache_stats on each run."""
    client.post("/api/projects", json={"project_id": "cache_list_proj", "name": "CacheList"})
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert "runs" in data
    our_run = next((r for r in data["runs"] if r["project_id"] == "cache_list_proj"), None)
    assert our_run is not None, "Created project must appear in run list"
    assert "cache_stats" in our_run
    if our_run["cache_stats"] is not None:
        assert "cache_hit_tokens" in our_run["cache_stats"]
        assert "cache_miss_tokens" in our_run["cache_stats"]
        assert "hit_ratio" in our_run["cache_stats"]
        assert "total_tokens" in our_run["cache_stats"]


# ── loop_item is additive: an old run and an old skillflow must both render ──

def _start_dpe(client, project_id):
    """Start a real dpe_default_v2 run (seeding the cross-config input it
    requires), so there are step rows to project."""
    client.post("/api/projects", json={"project_id": project_id, "name": project_id})
    from api.dependencies import get_skillflow
    finalize = (get_skillflow()._workspace.get_project_path(project_id)
                / "meta_conversation" / "finalize")
    finalize.mkdir(parents=True, exist_ok=True)
    (finalize / "step1_goals.json").write_text('{"goals": ["x"]}', encoding="utf-8")
    r = client.post("/api/runs", json={"config_name": "dpe_default_v2",
                                       "project_id": project_id})
    assert r.status_code == 201, r.text


def test_run_detail_reports_loop_item_as_none_when_nothing_ran(client):
    """A run whose loop never executed carries no items, and every step says so
    explicitly rather than omitting the key — a client that reads it must not
    have to distinguish "absent" from "not in a loop"."""
    _start_dpe(client, "loopitem_fresh")
    steps = client.get("/api/runs/loopitem_fresh").json()["steps"]
    assert steps, "expected seeded step rows"
    assert all("loop_item" in s for s in steps)
    assert all(s["loop_item"] is None for s in steps)


def test_run_detail_survives_a_skillflow_without_the_column(client, monkeypatch):
    """PURELY additive: the run page must still render against a skillflow that
    predates `loop_item`.

    This is not hypothetical. The container installs skillflow from PyPI, and a
    dev-loop wheel override is reverted by any `docker compose up -d` that
    recreates it — so the app can find itself one release behind at any time. A
    `s["loop_item"]` here would raise KeyError inside the projection and 500 the
    whole run detail, taking every historical run's page down with it.
    """
    from api.dependencies import get_skillflow
    _start_dpe(client, "loopitem_oldsf")
    sf = get_skillflow()
    real = sf.get_steps

    def without_the_column(run_id, **kw):
        return [{k: v for k, v in s.items() if k != "loop_item"}
                for s in real(run_id, **kw)]

    monkeypatch.setattr(sf, "get_steps", without_the_column)
    resp = client.get("/api/runs/loopitem_oldsf")
    assert resp.status_code == 200, resp.text
    steps = resp.json()["steps"]
    assert steps and all(s["loop_item"] is None for s in steps)
