"""/health reports the identity of the gate export it would import from.

The repository gate's subprocess imports a godot harness baked into the image.
/health must say which bytes that is, computed at request time, so a stale
image is visible from outside. An absent export is declared, never a 500.
"""
import hashlib

import api.main as main


def test_digest_is_sha256_of_the_real_bytes(client, tmp_path, monkeypatch):
    export = tmp_path / "godot_harness.py"
    payload = b"print('harness v1')\n"
    export.write_bytes(payload)
    monkeypatch.setattr(main, "GATE_EXPORT_HARNESS", export)

    gate = client.get("/health").json()["gate_harness"]

    assert gate["sha256"] == hashlib.sha256(payload).hexdigest()
    assert gate["path"] == str(export)
    assert gate["reason"] is None


def test_rewriting_the_same_path_changes_the_digest(client, tmp_path, monkeypatch):
    export = tmp_path / "godot_harness.py"
    export.write_bytes(b"harness v1\n")
    monkeypatch.setattr(main, "GATE_EXPORT_HARNESS", export)
    first = client.get("/health").json()["gate_harness"]["sha256"]

    rewritten = b"harness v2, five commits later\n"
    export.write_bytes(rewritten)
    second = client.get("/health").json()["gate_harness"]["sha256"]

    assert first != second
    assert second == hashlib.sha256(rewritten).hexdigest()


def test_missing_export_answers_200_with_null_digest_and_names_the_path(
        client, tmp_path, monkeypatch):
    missing = tmp_path / "absent" / "godot_harness.py"
    monkeypatch.setattr(main, "GATE_EXPORT_HARNESS", missing)

    response = client.get("/health")

    assert response.status_code == 200
    gate = response.json()["gate_harness"]
    assert gate["sha256"] is None
    assert str(missing) in gate["reason"]


def test_status_and_engine_keep_their_values(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["engine"] == "DPE SOTA v3.0"
