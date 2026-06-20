# tests/integration/test_config_routers.py
# Phase 1: configs are queryable data via /api/configs.

from starlette.testclient import TestClient


def _configs_by_name(client: TestClient) -> dict:
    resp = client.get("/api/configs")
    assert resp.status_code == 200, resp.text
    return {c["config_name"]: c for c in resp.json()["configs"]}


def test_list_configs_includes_dpe_and_meta(client):
    configs = _configs_by_name(client)
    assert "dpe_default_v2" in configs
    assert "meta_conversation" in configs


def test_dpe_manifest_flags_and_labels(client):
    dpe = _configs_by_name(client)["dpe_default_v2"]
    assert dpe["has_task_loop"] is True
    assert dpe["scheduler_owned"] is True
    assert dpe["seed_file"] == "project_brief.md"
    # data-driven labels derived from the x-aitelier block
    assert dpe["labels"]["1"] == "Researcher"
    assert dpe["labels"]["git_sync_pre"] == "Sync Repo"
    # checkpoints derived from the graph, default kind = file-review
    assert set(dpe["checkpoints"]) >= {"1", "2", "3"}
    assert dpe["checkpoints"]["1"]["kind"] == "file-review"


def test_meta_conversation_is_butler_driven_with_conversational_checkpoint(client):
    meta = _configs_by_name(client)["meta_conversation"]
    assert meta["scheduler_owned"] is False
    assert meta["checkpoints"]["gather"]["kind"] == "conversational"


def test_single_manifest_and_404(client):
    ok = client.get("/api/configs/dpe_default_v2/manifest")
    assert ok.status_code == 200, ok.text
    assert ok.json()["config_name"] == "dpe_default_v2"

    missing = client.get("/api/configs/does_not_exist/manifest")
    assert missing.status_code == 404
