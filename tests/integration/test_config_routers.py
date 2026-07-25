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


# ── Generated-pipeline catalog + durable state (/api/pipelines) ─────────────
# Regression cover for the code-review findings on this surface: the read path
# must not provision directories, must gate on the gen_ prefix, must cap in
# BYTES (not characters), and must return the TAIL of an over-cap file.

import pytest

from api.dependencies import get_config_registry, get_skillflow


def _register_gen(name: str):
    """Register a minimal generated pipeline so the catalog has an entry."""
    import yaml
    from core import pipeline_registry as pr
    graph = yaml.safe_dump({
        "name": name, "description": "t", "begin": "work",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "work", "step_type": "agent", "agent_config": "w",
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    })
    pr._register_text(get_skillflow(), get_config_registry(), name, graph)


def test_catalog_lists_generated_pipelines_only(client):
    _register_gen("gen_catalog_probe")
    body = client.get("/api/pipelines").json()["pipelines"]
    names = {p["config_name"] for p in body}
    assert "gen_catalog_probe" in names
    assert not any(n == "dpe_default_v2" for n in names), "built-ins must not be listed"
    # slim projection — the full manifest has its own endpoint
    assert set(body[0]) == {"config_name", "label", "state_files"}


def test_listing_does_not_provision_state_dirs(client):
    """A GET must not mkdir: a side-effecting read littered the state root with
    empty dirs for every pipeline that never wrote state."""
    _register_gen("gen_never_wrote")
    d = get_skillflow()._workspace.state_dir("gen_never_wrote", create=False)
    assert not d.exists()
    client.get("/api/pipelines")
    assert not d.exists(), "listing the catalog created a state dir"
    entry = next(p for p in client.get("/api/pipelines").json()["pipelines"]
                 if p["config_name"] == "gen_never_wrote")
    assert entry["state_files"] == []


def test_state_file_read_and_listing(client):
    _register_gen("gen_state_probe")
    d = get_skillflow()._workspace.state_dir("gen_state_probe")   # creating call
    (d / "positions.json").write_text('{"picks": ["MC.PA"]}', encoding="utf-8")
    entry = next(p for p in client.get("/api/pipelines").json()["pipelines"]
                 if p["config_name"] == "gen_state_probe")
    assert entry["state_files"] == [{"name": "positions.json", "size": 20}]
    r = client.get("/api/pipelines/gen_state_probe/state/file",
                   params={"name": "positions.json"})
    assert r.status_code == 200
    assert r.json()["content"] == '{"picks": ["MC.PA"]}'
    assert r.json()["truncated"] is False


@pytest.mark.parametrize("name", ["../../../etc/passwd", "/etc/passwd", "../secret"])
def test_state_file_rejects_traversal(client, name):
    _register_gen("gen_jail_probe")
    get_skillflow()._workspace.state_dir("gen_jail_probe")
    r = client.get("/api/pipelines/gen_jail_probe/state/file", params={"name": name})
    assert r.status_code in (403, 404), r.text
    assert "passwd" not in r.text and "secret" not in r.text


@pytest.mark.parametrize("config", ["dpe_default_v2", "meta_conversation", "nope"])
def test_state_file_rejects_non_generated_configs(client, config):
    """The read path once accepted ANY registered config, disagreeing with the
    listing and minting state dirs for built-ins."""
    r = client.get(f"/api/pipelines/{config}/state/file", params={"name": "x"})
    assert r.status_code == 404
    d = get_skillflow()._workspace.state_dir(config, create=False)
    assert not d.exists(), f"rejected read still provisioned {d}"


def test_cap_is_bytes_and_returns_the_tail(client):
    """The cap is byte-denominated (a char-sliced cap returned 3x the payload
    for CJK), and an over-cap append-only file yields its NEWEST bytes."""
    from api.config_routers import _STATE_FILE_CAP
    _register_gen("gen_bigstate")
    d = get_skillflow()._workspace.state_dir("gen_bigstate")
    body = "中文记录\n" * 60000            # ~5 bytes/char -> well over the cap
    (d / "memo.md").write_text(body + "\n## NEWEST\n", encoding="utf-8")

    r = client.get("/api/pipelines/gen_bigstate/state/file",
                   params={"name": "memo.md"}).json()
    assert r["truncated"] is True
    assert len(r["content"].encode("utf-8")) <= _STATE_FILE_CAP, "cap must be BYTES"
    assert "NEWEST" in r["content"], "must return the tail (newest entries)"
    assert not r["content"].startswith("�"), "partial codepoint not trimmed"
    assert r["total_bytes"] > _STATE_FILE_CAP
