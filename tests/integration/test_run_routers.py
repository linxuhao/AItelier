# tests/integration/test_run_routers.py
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
