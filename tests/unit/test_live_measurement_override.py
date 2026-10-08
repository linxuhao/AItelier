"""Fresh owned SDK/API observations; only external process/effect boundaries are fixtures."""
import ast
import fcntl
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import server
from core import deployment_quiescence as dq
from tests.unit.test_host_live_deployment_observation import live, wire_client
from tests.unit.test_normal_gate_preparation import bound, QUOTAS

SOURCE = Path(__file__).resolve().parents[2]


@pytest.fixture
def normal(live, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "operator-home"))
    monkeypatch.delenv("AITELIER_DEPLOY_OVERRIDE_FILE", raising=False)
    monkeypatch.setattr(server.httpx, "Client", wire_client(live))
    monkeypatch.setattr(server, "_require_docker", lambda: None)
    compose_calls = []
    def compose(*args, **kwargs):
        if args == ("ps", "-q", "aitelier"):
            return SimpleNamespace(returncode=0, stdout="a" * 64 + "\n", stderr="")
        compose_calls.append(args)
        assert_fence_held()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server, "_compose", compose)
    original_run = subprocess.run
    def run(command, *args, **kwargs):
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=str(os.getpid()) + "\n", stderr="")
        if command[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["ps", "-axo"]:
            return SimpleNamespace(returncode=0, stdout="999999 1 python3 owned-review-probe.py\n", stderr="")
        return original_run(command, *args, **kwargs)
    monkeypatch.setattr(server.subprocess, "run", run)
    return live, compose_calls


def assert_fence_held():
    with dq.admission_fence_path().open("a+b") as stream:
        with pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)


def override(action, observation, **extra):
    return {"action": action, "actor": "owned-operator", "reason": "bounded Source fixture",
            "ticket": "owned-same-measurement", "inventory_digest": observation["digest"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "acknowledge_unknown": True, **extra}


def observe():
    return dq.measure(runtime_facts=server._live_runtime_observation(server._DEFAULT_URL))


def latest():
    return json.loads(dq.evidence_path().read_text())["latest"]


def test_exact_preimage_and_current_file_refuse_distinct_fresh_observation(normal, tmp_path, monkeypatch):
    previous = observe()
    time.sleep(1.05)  # real seconds advance; observation time is not patched
    current = observe()
    assert previous["observed_at"] != current["observed_at"]
    assert previous["digest"] != current["digest"]
    assert previous["provenance"]["runtime_observed_at"] != current["provenance"]["runtime_observed_at"]
    path = tmp_path / "previous.json"
    path.write_text(json.dumps(override("redeploy", previous)))
    monkeypatch.setenv("AITELIER_DEPLOY_OVERRIDE_FILE", str(path))
    raw = subprocess.check_output(["git", "show", "82ce00652e71d24645f64a01695b1ed258438949:cli/server.py"], cwd=SOURCE).decode()
    node = next(node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)
                and node.name == "_require_deployment_clearance")
    scope = {"os": os, "_DEFAULT_URL": server._DEFAULT_URL,
             "_live_runtime_observation": server._live_runtime_observation}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "exact-source-preimage", "exec"), scope)
    for gate in (scope["_require_deployment_clearance"], server._require_deployment_clearance):
        time.sleep(1.05)
        with pytest.raises(RuntimeError, match="digest does not match this fresh measurement"):
            gate("redeploy")
        event = latest()
        assert event["status"] == "aborted" and event["usable"] is False
        assert event["inventory_digest"] not in (previous["digest"], current["digest"])
        print("fresh-file-refusal", event["inventory_digest"])
    assert normal[1] == []


def test_fresh_decision_same_measurement_fence_and_replay_refusal(normal):
    decisions = []
    def decide(action, snapshot):
        assert_fence_held()
        assert snapshot["provenance"]["domain"] == "live-runtime-and-operator-host"
        assert snapshot["blockers"]["external_active"][0]["ownership"] == "unregistered"
        assert dq._validate_observation(snapshot) is None
        decisions.append(snapshot)
        return override(action, snapshot)
    clearance = server._require_deployment_clearance("redeploy", override_decision=decide)
    assert clearance["event"]["inventory_digest"] == decisions[0]["digest"]
    assert clearance["event"]["audit"]["override_scope"] == "unknown_process_noise"
    assert clearance["event"]["audit"]["affected_ownership"] == decisions[0]["blockers"]["external_active"]
    result = server._finish_deployment(clearance, success=True)
    assert result["event"]["status"] == "completed" and result["event"]["usable"] is True
    cached = override("redeploy", decisions[0])
    fresh = []
    def replay(action, snapshot):
        fresh.append(snapshot)
        return cached
    time.sleep(1.05)
    with pytest.raises(RuntimeError, match="digest does not match"):
        server._require_deployment_clearance("redeploy", override_decision=replay)
    assert fresh[0]["observed_at"] != decisions[0]["observed_at"]
    assert fresh[0]["digest"] != decisions[0]["digest"]
    assert latest()["replayed"] is False
    print("same-fresh-positive", decisions[0]["observed_at"], decisions[0]["digest"],
          "distinct-replay", fresh[0]["observed_at"], fresh[0]["digest"])
    fence = dq.acquire_cutover_fence()
    dq.release_cutover_fence(fence)


@pytest.mark.parametrize("case", ["declined", "unknown-unacknowledged", "mutated-copy", "decision-error", "mixed-file"])
def test_explicit_decision_controls_preserve_original_facts(normal, tmp_path, monkeypatch, case):
    def decide(action, snapshot):
        assert_fence_held()
        if case == "declined":
            return None
        if case == "decision-error":
            raise ValueError("owned decision failure")
        if case == "mixed-file":
            pytest.fail("stale file was rescued by callback")
        if case == "mutated-copy":
            snapshot["quiescent"] = True
            snapshot["blockers"] = {}
            snapshot["errors"] = []
        return override(action, snapshot, acknowledge_unknown=False)
    if case == "mixed-file":
        path = tmp_path / "stale.json"
        path.write_text(json.dumps(override("redeploy", observe())))
        monkeypatch.setenv("AITELIER_DEPLOY_OVERRIDE_FILE", str(path))
    with pytest.raises(RuntimeError, match="not quiescent"):
        server._require_deployment_clearance("redeploy", override_decision=decide)
    event = latest()
    assert event["status"] == "aborted" and event["usable"] is False and event["replayed"] is False
    assert event["blockers"]["external_active"] and event["errors"]
    assert normal[1] == []
    fence = dq.acquire_cutover_fence()
    dq.release_cutover_fence(fence)


def test_new_foreign_owner_refuses_same_unknown_ack_without_clearing_owner(normal):
    db = normal[0][1]
    earlier = observe()
    with db.get_connection() as conn:
        conn.execute("INSERT INTO checkout_write_admissions (canonical_checkout,owner,kind) VALUES ('/owned/control','foreign-owner','external')")
        conn.commit()
    seen = []
    def unknown_only(action, snapshot):
        seen.append(snapshot)
        return override(action, snapshot)
    with pytest.raises(RuntimeError, match="cannot bypass authoritative active owners"):
        server._require_deployment_clearance("redeploy", override_decision=unknown_only)
    assert seen[0]["digest"] != earlier["digest"]
    assert seen[0]["blockers"]["checkout_leases"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT owner FROM checkout_write_admissions").fetchone()[0] == "foreign-owner"
    assert normal[1] == []


@pytest.mark.parametrize("change", [None, "cid", "source", "quota"])
def test_normal_godot_current_decision_preserves_effect_identity_and_source_checks(
        normal, bound, tmp_path, monkeypatch, change):
    manifest, binding, _ = bound
    quota = tmp_path / "quota.json"
    quota.write_text(json.dumps({"services": {"godot-builder": {"environment": QUOTAS}}}))
    expected = {"cid": "b" * 64, "pid": 123, "image": "sha256:" + "c" * 64}
    target = "sha256:" + "e" * 64
    after = {**expected, "cid": "d" * 64, "pid": 456, "image": target}
    values = iter([{**expected, "cid": "f" * 64} if change == "cid" else expected, after, after])
    monkeypatch.setattr(server, "_godot_identity", lambda: next(values))
    monkeypatch.setattr(server, "_godot_health", lambda cid: {
        "ok": True, "retention_limits": {"files": 4096, "bytes": 536870912, "patterns": 16, "search_entries": 4096},
        "source_identity": {"path": "/srv/godot_harness.py",
                            "sha256": binding["engine_sha256"], "loaded_sha256": binding["engine_sha256"]}})
    seen = []
    def decide(action, snapshot):
        assert_fence_held()
        seen.append(snapshot)
        if change == "source":
            manifest.write_text(manifest.read_text() + "\n")
        if change == "quota":
            quota.write_text(quota.read_text() + "\n")
        return override(action, snapshot)
    report = tmp_path / "effect.json"
    args = dict(override_file=str(quota), binding_file=str(manifest), expected_cid=expected["cid"],
                expected_pid=123, expected_image=expected["image"], target_image=target,
                report_file=str(report), override_decision=decide)
    if change:
        with pytest.raises(ValueError, match="identity changed|configuration changed"):
            server.recreate_godot_builder(**args)
        assert normal[1] == []
    else:
        server.recreate_godot_builder(**args)
        assert normal[1][0][2:] == ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "godot-builder")
    value = json.loads(report.read_text())
    event = value["journal"]["event"]
    assert event["status"] == ("aborted" if change else "completed")
    assert event["usable"] is (change is None)
    assert event["inventory_digest"] == seen[0]["digest"]
    assert value["expected_before"]["image"] != value["target_image"]
    assert event["audit"]["actor"] == "owned-operator"
    fence = dq.acquire_cutover_fence()
    dq.release_cutover_fence(fence)


def test_cli_acknowledgement_exposes_only_decision_projection(monkeypatch, capsys):
    from cli import app
    answers = iter(["operator", "reason", "ticket", "2100-01-01T00:00:00+00:00"])
    confirms = iter([True, True])
    monkeypatch.setattr(app.typer, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(app.typer, "confirm", lambda *a, **k: next(confirms))
    observation = {"observed_at": "owned-time", "digest": "owned-digest",
                   "provenance": {"runtime_identity": {"pid": 1}}, "blockers": {}, "errors": [],
                   "external_owners": [{"command": "not-in-display"}]}
    result = app._acknowledge_deployment("redeploy", observation)
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"action", "observed_at", "digest", "provenance", "blockers", "errors"}
    assert result["inventory_digest"] == "owned-digest" and result["acknowledge_unknown"] is True
    assert "not-in-display" not in json.dumps(output)

