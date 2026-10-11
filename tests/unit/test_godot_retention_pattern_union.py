"""Real filesystem and subprocess controls for one bounded selector union.

CPU SOURCE only: the engine seam runs a Python child, never Godot. Retention,
request dispatch, probe parsing, file descriptors, hashing and cleanup are real.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.unit.test_godot_retention_selectors import _load

PATTERNS = ["monthly-journey-actual-*/*.png", "monthly-journey-actual-*/*.json",
            "cultivation-visual-proof-*/*.png", "cultivation-visual-proof-*/*.json"]


def _sized(gh, monkeypatch):
    # Exact existing loaded server quotas, never enlarged to admit the probe.
    monkeypatch.setattr(gh, "_RETAIN_MAX_SEARCH_ENTRIES", 4096)
    monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 4096)
    monkeypatch.setattr(gh, "_RETAIN_MAX_BYTES", 536870912)
    assert gh._RETAIN_MAX_PATTERNS == 16


def _assert_bytes(result, corr):
    assert result["ok"], result
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert all(manifest[k] == v for k, v in corr.items())
    root = Path(result["retained_dir"])
    for row in manifest["files"] + manifest["raw_logs"]:
        body = (root / row["path"]).read_bytes()
        assert len(body) == row["size"]
        assert hashlib.sha256(body).hexdigest() == row["sha256"]
    return manifest


def test_101_owned_roots_four_patterns_charge_each_directory_once(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    _sized(gh, monkeypatch)
    homes = []
    for i in range(101):
        home = tmp_path / ("home-%d" % i)
        homes.append(home)
        for prefix in ("monthly-journey-actual", "cultivation-visual-proof"):
            directory = home / ("%s-%d" % (prefix, i))
            directory.mkdir(parents=True)
            for j in range(10):
                (directory / ("f%d.%s" % (j, "png" if j % 2 else "json"))).write_bytes(
                    ("owned-%d-%d" % (i, j)).encode())
    # 2 root entries + 20 artifact entries, 101 times = 2222 unique work.
    # The former four separate walks charge (2+10)*4*101 = 4848 and fail.
    scans = []
    real_scandir = gh.os.scandir
    def observed_scandir(fd):
        st = os.fstat(fd)
        scans.append((st.st_dev, st.st_ino))
        return real_scandir(fd)
    monkeypatch.setattr(gh.os, "scandir", observed_scandir)
    raw = [{"label": "pass-%d" % i, "stdout": "complete-%d\n" % i,
            "stderr": "diagnostic-%d\n" % i} for i in range(222)]
    corr = {"project_id": "union-source", "run_id": "run-union",
            "operation_id": "op-union", "owner_id": "owner-source", "generation": 17}
    out = gh._retain_copy([], homes, raw, corr, patterns=PATTERNS, requested=True,
                          pass_labels=[{"script": "entry-%d" % i} for i in range(101)])
    assert out["ok"], out
    assert len(out["files"]) == 2020 and len(out["raw_logs"]) == 444
    assert out["refused"] == out["missing"] == [] and out["limit_hit"] is None
    assert len(scans) == 303 and len(set(scans)) == 303
    monkeypatch.setattr(gh.os, "scandir", real_scandir)
    for home in homes:
        shutil.rmtree(home)
    manifest = _assert_bytes(out, corr)
    assert len({(r["pass"], r["user_path"]) for r in manifest["files"]}) == 2020
    shutil.copytree(out['retained_dir'], tmp_path / 'union-101-owned-evidence', dirs_exist_ok=True)
    (tmp_path / 'union-101-roots.json').write_text(json.dumps(
        {"owned_roots": 101, "patterns": PATTERNS, "unique_entries": 2222,
         "directory_scans": len(scans), "files": len(out["files"]),
         "complete_raw_streams": len(out["raw_logs"]), "manifest": manifest}, indent=2))


def test_true_unique_work_overflow_still_refuses_and_closes_pins(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    _sized(gh, monkeypatch)
    home = tmp_path / "overflow-home"
    directory = home / "reports-1"
    directory.mkdir(parents=True)
    for i in range(4096):
        (directory / ("foreign-%d.txt" % i)).write_text("outside selection")
    before = len(os.listdir('/proc/self/fd'))
    out = gh._retain_copy([], [home], [], {}, patterns=["reports-*/*.json", "reports-*/*.png"], requested=True)
    assert not out["ok"] and out["files"] == []
    assert any("exceeded 4096 directory entries" in r for r in out["refused"])
    assert set(out["missing"]) == {"reports-*/*.json", "reports-*/*.png"}
    assert out["limit_hit"] is None
    assert len(os.listdir('/proc/self/fd')) == before


def test_overlapping_and_mixed_depth_patterns_preserve_each_obligation(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "reports-1/sub").mkdir(parents=True)
    (home / "reports-1/one.json").write_text('{"one":1}')
    (home / "reports-1/sub/deep.json").write_text('{"deep":2}')
    pats = ["reports-*/*.json", "reports-*/one.json", "reports-*/sub/*.json"]
    out = gh._retain_copy([], [home], [], {}, patterns=pats, requested=True)
    assert out["ok"] and len(out["files"]) == 2 and out["missing"] == []
    absent = gh._retain_copy([], [home], [], {}, patterns=pats + ["reports-*/absent.png"], requested=True)
    assert not absent["ok"] and absent["missing"] == ["reports-*/absent.png"]
    assert len(absent["files"]) == 2


_CHILD = r'''
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['HOME'])
user = home / '.local/share/godot/app_userdata/union-control'
for prefix in ('monthly-journey-actual', 'cultivation-visual-proof'):
    directory = user / ('%s-%d' % (prefix, os.getpid()))
    directory.mkdir(parents=True)
    (directory / 'runtime.json').write_text(json.dumps({'pid': os.getpid()}))
    (directory / 'frame.png').write_bytes(b'\x89PNG\r\n\x1a\nowned control')
(user / 'Profiles').mkdir()
(user / 'Profiles/private.json').write_text('private sentinel')
if 'AITELIER_PROBE_OUT' in os.environ:
    frames = int(os.environ['AITELIER_PROBE_FRAMES'])
    pathlib.Path(os.environ['AITELIER_PROBE_OUT']).write_text(json.dumps({
        'frames': frames, 'nodes': {}, 'asserts': [], 'complete': True,
        'timing': {'frames_stepped': frames, 'game_usec': round(frames / 60 * 1000000)}}))
print('complete child stdout:' + 'x' * 6000, flush=True)
print('complete child stderr:' + 'y' * 6000, file=sys.stderr, flush=True)
mode = sys.argv[1]
if mode == 'timeout': time.sleep(10)
if mode == 'fail':
    print('FAIL controlled child', flush=True)
    sys.exit(1)
'''


def _subprocess_engine(gh, monkeypatch, mode='pass'):
    homes, full = [], []
    def run(args, timeout, extra_env=None, render=False):
        env = os.environ.copy()
        env.update(extra_env or {})
        homes.append(Path(env['HOME']))
        actual_mode = mode if '--import' not in args else 'pass'
        cp = subprocess.run([sys.executable, '-c', _CHILD, actual_mode], env=env,
                            capture_output=True, text=True,
                            timeout=0.1 if actual_mode == 'timeout' else timeout)
        full.append(cp)
        if actual_mode == 'unexpected':
            raise RuntimeError('original owned subprocess control error')
        return cp
    monkeypatch.setattr(gh, '_run', run)
    return homes, full


def _project(tmp_path, entries):
    root = tmp_path / 'project'
    (root / 'tests').mkdir(parents=True)
    (root / 'project.godot').write_text('[application]\nconfig/name="union-control"\n')
    for i in range(entries):
        (root / 'tests' / ('entry%d.gd' % i)).write_text('extends SceneTree\n')
    return root


def test_normal_http_script_and_playtest_retain_real_child_outputs(monkeypatch, tmp_path):
    import http.client
    import threading
    gh = _load(tmp_path, monkeypatch)
    _sized(gh, monkeypatch)
    project = _project(tmp_path, 100)
    homes, children = _subprocess_engine(gh, monkeypatch)
    server = gh.ThreadingHTTPServer(('127.0.0.1', 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manifests = []
    try:
        for route, extra, passes in [
            ('/script', {'scripts': []}, 101),
            ('/playtest', {'frames': 2, 'captures': 0,
                          'spec': {'frames': 2, 'scenarios': [
                              {'name': 'scenario-%d' % i, 'timeline': []} for i in range(221)]}}, 222)]:
            start = len(children)
            req = {'project_dir': str(project), 'project_id': 'normal-union',
                   'run_id': 'source-route-run', 'operation_id': 'op-' + route[1:],
                   'retain': {'files': [], 'patterns': PATTERNS}, **extra}
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=60)
            conn.request('POST', route, json.dumps(req), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            result = json.loads(response.read())
            conn.close()
            assert response.status == 200 and result['passed'], result
            out = result['retention']
            assert len(children) - start == passes
            assert len(out['raw_logs']) == passes * 2
            assert len(out['files']) == passes * 4
            assert all('Profiles' not in r['user_path'] for r in out['files'])
            corr = {k: req[k] for k in ('project_id', 'run_id', 'operation_id')}
            manifest = _assert_bytes(out, corr)
            assert manifest['owner_id'] and manifest['generation'] > 0
            for i, cp in enumerate(children[start:]):
                rows = [r for r in manifest['raw_logs'] if r['pass'] == i]
                assert len(rows) == 2
                for row in rows:
                    expected = cp.stdout if row['source'] == 'raw_stdout' else cp.stderr
                    assert (Path(out['retained_dir']) / row['path']).read_text() == expected
                    assert row['size'] > 6000
            assert all(not home.exists() for home in homes)
            manifests.append((Path(out['manifest']), Path(out['manifest']).read_bytes()))
            shutil.copytree(out['retained_dir'], tmp_path / ('normal-union-' + route[1:] + '-evidence'),
                            dirs_exist_ok=True)
            (tmp_path / ('normal-union-' + route[1:] + '.json')).write_text(json.dumps(
                {'route': route, 'child_processes': passes, 'retained_files': len(out['files']),
                 'complete_raw_streams': len(out['raw_logs']), 'manifest': manifest}, indent=2))
        assert manifests[0][0] != manifests[1][0]
        assert all(path.read_bytes() == body for path, body in manifests)
        assert (project / 'project.godot').is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize('mode', ['fail', 'timeout', 'unexpected'])
def test_original_script_failure_retains_union_and_cleans_own_homes(monkeypatch, tmp_path, mode):
    gh = _load(tmp_path, monkeypatch)
    _sized(gh, monkeypatch)
    project = _project(tmp_path, 1)
    homes, children = _subprocess_engine(gh, monkeypatch, mode)
    corr = {'project_id': 'failure-union', 'run_id': mode, 'operation_id': 'op-' + mode}
    if mode == 'unexpected':
        with pytest.raises(RuntimeError, match='^original owned subprocess control error$'):
            gh.run_script(str(project), [], timeout=5, retain_patterns=PATTERNS, corr=corr)
        manifests = list((tmp_path / 'evidence-root/evidence').glob('*/manifest.json'))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert not manifest['ok'] and any('original owned subprocess control error' in s
                                          for s in manifest['refused'])
    else:
        result = gh.run_script(str(project), [], timeout=5, retain_patterns=PATTERNS, corr=corr)
        assert not result['passed'] and result['results'][0]['returncode'] == (124 if mode == 'timeout' else 1)
        manifest = _assert_bytes(result['retention'], corr)
        raw = manifest['raw_logs'][-2:]
        root = Path(result['retention']['retained_dir'])
        assert (root / raw[0]['path']).read_text().startswith('complete child stdout:')
        assert (root / raw[1]['path']).read_text().startswith('complete child stderr:')
        assert raw[0]['timed_out'] is (mode == 'timeout')
    assert len(manifest['files']) == 8 and len(manifest['raw_logs']) == 4
    assert all(not home.exists() for home in homes)
    assert (project / 'project.godot').is_file()
