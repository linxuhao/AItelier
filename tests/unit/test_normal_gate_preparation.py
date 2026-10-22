"""Owned filesystem/runtime controls, with Docker effects replaced at their boundary."""
import hashlib
import importlib
import io
import json
import subprocess
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from skillflow.core import SkillFlow

from cli import server
from core import datadir, deployment_quiescence as dq
from core.db_manager import DBManager
from tools import gate_binding as gb

SOURCE = Path(__file__).resolve().parents[2]
QUOTAS = {"GODOT_RETAIN_MAX_FILES": "4096", "GODOT_RETAIN_MAX_BYTES": "536870912",
          "GODOT_RETAIN_MAX_PATTERNS": "16", "GODOT_RETAIN_MAX_SEARCH_ENTRIES": "4096"}


@pytest.fixture
def bound(tmp_path, monkeypatch, request):
    requested_sdk = getattr(request, "param", "1.5.99")
    fixture = tmp_path / "fixture-source"
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(SOURCE), str(fixture)], check=True)
    manifest = tmp_path / "binding.json"
    binding = {"source": str(SOURCE), "head": gb.git(SOURCE, "rev-parse", "HEAD"),
               "tree": gb.git(SOURCE, "rev-parse", "HEAD^{tree}"), "image": "sha256:" + "a" * 64,
               "sdk": "1.5.99" if requested_sdk == "actual-image" else requested_sdk,
               "files": {name: gb.digest(SOURCE / name) for name in gb.FILES},
               "engine_sha256": gb.digest(SOURCE / gb.FILES[0])}
    manifest.write_text(json.dumps(binding))
    monkeypatch.setenv("GATE_BINDING_FILE", str(manifest))
    # Explicit test metadata boundary, not a claim about the installed SDK.
    version = gb.metadata.version
    if requested_sdk != "actual-image":
        monkeypatch.setattr(gb.metadata, "version",
                            lambda name: binding["sdk"] if name == "skillflow-py" else version(name))
    harness = importlib.import_module("docker.godot.godot_harness")

    def health(url, timeout):
        value = harness.health_snapshot()
        value["source_identity"]["path"] = "/srv/godot_harness.py"
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(gb.urllib.request, "urlopen", health)
    yield manifest, binding, fixture
    shutil.rmtree(fixture)


@pytest.mark.parametrize("bound", ["1.5.85", "1.5.99", "actual-image"], indirect=True)
def test_capture_measures_actual_imports_and_both_coordinates(bound):
    manifest, binding, fixture = bound
    frozen, sha256 = gb.read_binding(manifest)
    captured = gb.capture(frozen, sha256, str(SOURCE), str(fixture))
    assert captured["files"][0] == {"path": str(SOURCE / gb.FILES[0]),
                                    "sha256": binding["files"][gb.FILES[0]]}
    assert captured["files"][1]["path"] == str(fixture / gb.FILES[0])
    assert captured["engine"]["path"] == "/srv/godot_harness.py"
    assert captured["engine"]["sha256"] == binding["engine_sha256"]
    assert captured["sdk"] == binding["sdk"]
    assert captured["binding_sha256"] == sha256


@pytest.mark.parametrize("mutation", ["missing", "changed", "symlink"])
def test_frozen_fixture_refuses_missing_or_changed_file(bound, mutation):
    manifest, binding, fixture = bound
    target = fixture / gb.FILES[1]
    if mutation == "changed":
        target.write_text("changed Source\n")
    else:
        target.unlink()
        if mutation == "symlink":
            target.symlink_to(SOURCE / gb.FILES[1])
    with pytest.raises(ValueError, match="frozen|identity|regular"):
        gb.capture(binding, gb.digest(manifest), str(SOURCE), str(fixture))


@pytest.mark.parametrize("bound", ["1.5.85", "1.5.99"], indirect=True)
@pytest.mark.parametrize("stage", [0, 23])
def test_real_stage_exit_and_changed_after_identity_are_retained(bound, tmp_path, monkeypatch, stage):
    manifest, binding, fixture = bound
    game = tmp_path / "game"
    game.mkdir()
    run = game / "run_tests.sh"
    run.write_text(f"#!/bin/sh\nexit {stage}\n")
    run.chmod(0o755)
    monkeypatch.chdir(game)
    report = tmp_path / "gate.json"
    sha256 = gb.digest(manifest)
    assert gb.run_bound(str(manifest), str(report), str(SOURCE), str(fixture), sha256) == stage
    assert json.loads(report.read_text())["identity"] == "unchanged"
    assert gb.receipt_identity(report, stage, manifest, sha256) == "unchanged"
    original = report.read_bytes()
    target = fixture / gb.FILES[1]
    run.write_text(f"#!/bin/sh\nprintf changed > '{target}'\nexit 23\n")
    changed_report = tmp_path / "changed-gate.json"
    assert gb.run_bound(str(manifest), str(changed_report), str(SOURCE), str(fixture), sha256) == 69
    result = json.loads(changed_report.read_text())
    assert result["identity"] == "mismatch" and result["raw_exit"] == 23
    assert report.read_bytes() == original


@pytest.mark.parametrize("bound", ["1.5.85", "1.5.99"], indirect=True)
def test_sdk_mismatch_refuses_before_stage(bound, tmp_path, monkeypatch):
    manifest, binding, fixture = bound
    other = "1.5.99" if binding["sdk"] == "1.5.85" else "1.5.85"
    version = gb.metadata.version
    monkeypatch.setattr(gb.metadata, "version",
                        lambda name: other if name == "skillflow-py" else version(name))
    report = tmp_path / "mismatch.json"
    assert gb.run_bound(manifest, report, str(SOURCE), str(fixture), gb.digest(manifest)) == 69
    receipt = json.loads(report.read_text())
    assert receipt["identity"] == "mismatch" and receipt["raw_exit"] is None
    assert f"expected {binding['sdk']}, observed {other}" in receipt["error"]


@pytest.mark.parametrize("pin", [None, 85, "", "1.5.*", ">=1.5.85", "1.5.99 "])
def test_missing_or_malformed_pin_has_no_implicit_migration(bound, tmp_path, pin):
    manifest, binding, fixture = bound
    if pin is None:
        del binding["sdk"]
    else:
        binding["sdk"] = pin
    manifest.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match="sdk"):
        gb.read_binding(manifest)
    report = tmp_path / "invalid-pin.json"
    assert gb.run_bound(manifest, report, str(SOURCE), str(fixture)) == 69
    receipt = json.loads(report.read_text())
    assert receipt["identity"] == "unknown" and receipt["raw_exit"] is None


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_manifest_requires_regular_non_symlink_bytes(bound, tmp_path, kind):
    manifest, _, _ = bound
    raw = manifest.read_bytes()
    manifest.unlink()
    if kind == "directory":
        manifest.mkdir()
    elif kind == "fifo":
        __import__("os").mkfifo(manifest)
    else:
        target = tmp_path / "target.json"
        target.write_bytes(raw)
        manifest.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        gb.read_binding(manifest)


def test_manifest_path_swap_during_parse_keeps_one_raw_witness(bound, tmp_path, monkeypatch):
    manifest, binding, fixture = bound
    raw = manifest.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({**binding, "sdk": "1.5.85", "image": "sha256:" + "f" * 64}))
    loads, read_regular = gb.json.loads, gb.read_regular
    reads = []

    def counted_read(path):
        if Path(path) == manifest:
            reads.append(path)
        return read_regular(path)

    def swap_during_parse(value, *args, **kwargs):
        monkeypatch.setattr(gb.json, "loads", loads)
        assert value == raw
        replacement.replace(manifest)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(gb, "read_regular", counted_read)
    monkeypatch.setattr(gb.json, "loads", swap_during_parse)
    captured = gb.capture(binding, sha256, str(SOURCE), str(fixture))
    assert len(reads) == 1
    assert captured["sdk"] == "1.5.99"
    assert captured["image"] == binding["image"]
    assert captured["binding_sha256"] == sha256
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() != sha256
    with pytest.raises(gb.IdentityMismatch, match="frozen binding bytes"):
        gb.capture(binding, sha256, str(SOURCE), str(fixture))


@pytest.mark.parametrize("field", ["sdk", "image", "binding_sha256", "rebuilt"])
def test_receipt_requires_original_frozen_pin_hash_and_image(bound, tmp_path, field):
    manifest, binding, fixture = bound
    frozen, sha256 = gb.read_binding(manifest)
    observation = gb.capture(frozen, sha256, str(SOURCE), str(fixture))
    receipt = dict(identity="unchanged", raw_exit=23, before=observation, after=observation)
    report = tmp_path / "receipt.json"
    report.write_text(json.dumps(receipt))
    assert gb.receipt_identity(report, 23, manifest, sha256) == "unchanged"
    assert gb.receipt_identity(report, 23, manifest, None) == "unknown"
    if field == "rebuilt":
        manifest.write_text(json.dumps({**binding, "sdk": "1.5.85", "image": "sha256:" + "f" * 64}))
        observation.update(sdk="1.5.85", image="sha256:" + "f" * 64,
                           binding_sha256=gb.digest(manifest))
    else:
        observation[field] = {"sdk": "1.5.85", "image": "sha256:" + "f" * 64,
                              "binding_sha256": "f" * 64}[field]
    report.write_text(json.dumps(receipt))
    assert gb.receipt_identity(report, 23, manifest, sha256) == "unknown"


def test_capture_compares_callers_binding_instead_of_rebuilding_it(bound):
    manifest, binding, fixture = bound
    with pytest.raises(gb.IdentityMismatch, match="caller's frozen binding"):
        gb.capture({**binding, "sdk": "1.5.85"}, gb.digest(manifest), str(SOURCE), str(fixture))


def test_missing_engine_identity_refuses_before_stage(bound, tmp_path, monkeypatch):
    manifest, _, fixture = bound
    monkeypatch.setattr(gb.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b'{"ok":true}'))
    report = tmp_path / "gate.json"
    assert gb.run_bound(str(manifest), str(report), str(SOURCE), str(fixture)) == 69
    assert json.loads(report.read_text())["raw_exit"] is None


@pytest.mark.parametrize("change_sidecar", [False, True])
@pytest.mark.parametrize("bound", ["1.5.85", "1.5.99"], indirect=True)
@pytest.mark.parametrize("sdk_state", ["compatible", "mismatch", "missing"])
def test_normal_launcher_real_snapshot_exit_and_sidecar_poison(
        bound, tmp_path, monkeypatch, change_sidecar, sdk_state):
    manifest, binding, fixture = bound
    observed_sdk = binding["sdk"] if sdk_state == "compatible" else "1.5.0"
    if sdk_state == "missing":
        del binding["sdk"]
        manifest.write_text(json.dumps(binding))
    bins, game, reports = (tmp_path / name for name in ("bin", "game", "reports"))
    for path in (bins, game, reports):
        path.mkdir()
    subprocess.run(["sh", str(SOURCE / "tools/install_gate_run.sh"), str(bins)], check=True)
    assert gb.digest(bins / "gate_run.sh") == gb.digest(SOURCE / "tools/gate_run.sh")
    # Only this owned script copy redirects the snapshot fixture directory.
    launcher = bins / "gate_run.sh"
    launcher.write_text(launcher.read_text().replace(
        'SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"', f'SNAPROOT="{tmp_path / "snapshots"}"'))
    subprocess.run(["git", "init", "--quiet", str(game)], check=True)
    (game / "run_tests.sh").write_text("#!/bin/sh\nexit 23\n")
    (game / "run_tests.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(game), "add", "run_tests.sh"], check=True)
    subprocess.run(["git", "-C", str(game), "-c", "user.name=fixture", "-c",
                    "user.email=fixture@localhost", "commit", "-qm", "fixture"], check=True)
    head = gb.git(game, "rev-parse", "HEAD")
    calls = tmp_path / "docker-calls.jsonl"
    harness = importlib.import_module("docker.godot.godot_harness")
    health = harness.health_snapshot()
    health["source_identity"]["path"] = "/srv/godot_harness.py"
    health_bytes = json.dumps(health).encode()
    fake = bins / "docker"
    fake.write_text(f'''#!{sys.executable}
import io,json,os,sys
from pathlib import Path
args=sys.argv[1:]
calls=Path({str(calls)!r})
prior=calls.read_text().splitlines() if calls.exists() else []
with calls.open('a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
 print(('d' if {change_sidecar!r} and prior else 'b')*64+' sha256:'+'c'*64+' 123 started running')
else:
 assert args[0]=='run' and '--rm' in args
 assert args[args.index('-w')+1] != {str(game)!r}
 assert {binding['image']!r} in args
 assert {str(SOURCE) + ':/app:ro'!r} in args
 assert {str(SOURCE) + ':/home/linxuhao/AItelier:ro'!r} in args
 assert '/app/tools/gate_binding.py' in args
 assert args[-3:-1]==['run',{str(manifest)!r}]
 # Execute the real helper, imports, Git checks and owned stage. Only image
 # metadata and engine health are controlled boundaries in this unit fixture.
 sys.path.insert(0,{str(SOURCE)!r})
 from tools import gate_binding as observer
 version=observer.metadata.version
 observer.metadata.version=lambda name: {observed_sdk!r} if name=='skillflow-py' else version(name)
 observer.urllib.request.urlopen=lambda *a,**k: io.BytesIO({health_bytes!r})
 os.chdir(args[args.index('-w')+1])
 sys.exit(observer.run_bound({str(manifest)!r},args[-1],{str(SOURCE)!r},{str(fixture)!r},
                            expected_sha256=args[args.index('--binding-sha256')+1]))
''')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bins) + ":" + __import__("os").environ["PATH"])
    result = subprocess.run([str(launcher), str(game), str(reports), "owned"], capture_output=True, text=True)
    retained = change_sidecar or sdk_state == "mismatch"
    assert result.returncode == (69 if retained or sdk_state == "missing" else 23), result.stderr
    if sdk_state == "missing":
        assert not calls.exists()
        assert not (reports / "owned.gate.head").exists()
        assert not (tmp_path / "snapshots").exists()
        return
    assert (reports / "owned.gate.head").read_text().strip() == head
    exit_value = (reports / "owned.gate.exit").read_text().strip()
    assert exit_value.startswith("mismatch-platform-identity;") if retained else exit_value == "23"
    receipt = json.loads((reports / "owned.platform-identity.json").read_text())
    assert receipt["raw_exit"] == (None if sdk_state == "mismatch" else 23)
    assert gb.git(game, "rev-parse", "HEAD") == head
    assert gb.git(game, "status", "--porcelain") == ""
    snapshots = list((tmp_path / "snapshots").iterdir())
    if retained:
        assert len(snapshots) == 1
        assert gb.git(snapshots[0], "rev-parse", "HEAD") == head
        assert f"worktree {snapshots[0]}" in gb.git(game, "worktree", "list", "--porcelain")
    else:
        assert snapshots == []
        assert len(gb.git(game, "worktree", "list", "--porcelain").splitlines()) == 3


@pytest.fixture
def initialized(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "owned-home"))
    sf = SkillFlow(str(tmp_path / "owned-sf.sqlite"))
    db = DBManager(str(tmp_path / "owned-host.sqlite"))
    monkeypatch.setattr(server, "_live_runtime_observation",
                        lambda url: dq.runtime_observation(skillflow=sf, db=db))
    original = dq.measure
    monkeypatch.setattr(dq, "measure", lambda **kw: original(**kw, external_probe=list))
    monkeypatch.setattr(server, "_require_docker", lambda: None)
    yield sf, db
    sf._conn.close()


@pytest.mark.parametrize("failure", [None, "compose", "health", "identity"])
def test_existing_initialized_fence_authorize_effect_and_journal(
        initialized, bound, tmp_path, monkeypatch, failure):
    manifest, binding, _ = bound
    override = tmp_path / "quota.json"
    override.write_text(json.dumps({"services": {"godot-builder": {"environment": QUOTAS}}}))
    expected = {"cid": "b" * 64, "pid": 123, "image": "sha256:" + "c" * 64}
    target_image = "sha256:" + "e" * 64
    after = {**expected, "cid": "d" * 64, "pid": 456, "image": target_image}
    identities = iter([expected, after, {**after, "pid": 999} if failure == "identity" else after])
    monkeypatch.setattr(server, "_godot_identity", lambda: next(identities))
    calls = []

    def compose(*args, **kwargs):
        # This is the process boundary. Real host guard/fence/journal still run.
        assert Path(datadir.godot_control_dir() / "deployment-admission.lock").exists()
        import fcntl
        with open(datadir.godot_control_dir() / "deployment-admission.lock", "a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        calls.append(args)
        overlay = json.loads(Path(args[1]).read_text())
        assert set(overlay["services"]) == {"godot-builder"}
        assert overlay["services"]["godot-builder"]["image"] == target_image
        assert overlay["services"]["godot-builder"]["environment"] == QUOTAS
        assert set(overlay["services"]["godot-builder"]) == {"image", "environment"}
        return SimpleNamespace(returncode=23 if failure == "compose" else 0)

    monkeypatch.setattr(server, "_compose", compose)
    health = {"ok": True, "retention_limits": {"files": 4096, "bytes": 536870912,
              "patterns": 16, "search_entries": 4096}, "source_identity": {
              "path": "/srv/godot_harness.py", "sha256": binding["engine_sha256"],
              "loaded_sha256": "wrong" if failure == "health" else binding["engine_sha256"]}}
    monkeypatch.setattr(server, "_godot_health", lambda cid: health)
    report = tmp_path / "effect.json"
    args = dict(override_file=str(override), binding_file=str(manifest),
                expected_cid=expected["cid"], expected_pid=expected["pid"],
                expected_image=expected["image"], target_image=target_image, report_file=str(report))
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            server.recreate_godot_builder(**args)
    else:
        server.recreate_godot_builder(**args)
    assert calls[0][2:] == ("up", "-d", "--no-deps", "--force-recreate", "--no-build", "godot-builder")
    latest = json.loads(dq.evidence_path().read_text())["latest"]
    assert latest["status"] == ("aborted" if failure else "completed")
    assert latest["pending"] is False and latest["usable"] is (failure is None)
    assert json.loads(report.read_text())["journal"]["event"] == latest
    fence = dq.acquire_cutover_fence()  # prior operation released its exclusive fence
    dq.release_cutover_fence(fence)


@pytest.mark.parametrize("extra", ["aitelier", "zvec-grep", "volumes", "infinite"])
def test_unsafe_quota_shape_refuses_before_runtime(initialized, bound, tmp_path, monkeypatch, extra):
    manifest, _, _ = bound
    overlay = {"services": {"godot-builder": {"environment": dict(QUOTAS)}}}
    if extra == "infinite":
        overlay["services"]["godot-builder"]["environment"]["GODOT_RETAIN_MAX_FILES"] = "0"
    elif extra == "volumes":
        overlay["services"]["godot-builder"]["volumes"] = ["/:/host"]
    else:
        overlay["services"][extra] = {}
    override = tmp_path / "unsafe.json"
    override.write_text(json.dumps(overlay))
    monkeypatch.setattr(server, "_require_deployment_clearance", lambda *a, **k: pytest.fail("unsafe overlay reached runtime"))
    with pytest.raises(ValueError, match="four owned finite"):
        server.recreate_godot_builder(override_file=str(override), binding_file=str(manifest),
                                     expected_cid="b" * 64, expected_pid=123,
                                     expected_image="sha256:" + "c" * 64, target_image="sha256:" + "e" * 64,
                                     report_file=str(tmp_path / "effect.json"))


def test_initialized_foreign_write_owner_blocks_before_effect(initialized, bound, tmp_path, monkeypatch):
    _, db = initialized
    with db.get_connection() as conn:
        conn.execute("INSERT INTO checkout_write_admissions (canonical_checkout, owner, kind) VALUES ('/foreign/repo', 'foreign-owner', 'external')")
        conn.commit()
    manifest, _, _ = bound
    override = tmp_path / "quota.json"
    override.write_text(json.dumps({"services": {"godot-builder": {"environment": QUOTAS}}}))
    monkeypatch.setattr(server, "_compose", lambda *a, **k: pytest.fail("foreign owner was bypassed"))
    with pytest.raises(RuntimeError, match="not quiescent"):
        server.recreate_godot_builder(override_file=str(override), binding_file=str(manifest),
                                     expected_cid="b" * 64, expected_pid=123,
                                     expected_image="sha256:" + "c" * 64, target_image="sha256:" + "e" * 64,
                                     report_file=str(tmp_path / "effect.json"))
    latest = json.loads(dq.evidence_path().read_text())["latest"]
    assert latest["status"] == "aborted" and not latest["usable"]
    with db.get_connection() as conn:
        assert conn.execute("SELECT owner FROM checkout_write_admissions").fetchone()[0] == "foreign-owner"
