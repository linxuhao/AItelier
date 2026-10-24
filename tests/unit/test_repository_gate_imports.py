"""Real entry -> Bash -> Python import and request-budget regressions.

Run in the newly built reviewed disposable CPU image. No dependency installation,
engine call or fake harness is used. For the failing baseline, set
REPOSITORY_EXECUTOR_ENTRY_BASELINE to the retained old entry file INSIDE that
same image; the useful child regression must then fail with missing docker.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
EXPORT = Path("/opt/aitelier-gate-imports")


def _gate(tmp_path, body, *, report=True, extra=None):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src/project_marker.py").write_text("VALUE = 'project-source-visible'\n")
    (repo / "tools/prepare.py").write_text(textwrap.dedent(body))
    script = repo / "run_tests.sh"
    script.write_text("#!/bin/sh\nexec python3 tools/prepare.py\n")
    ticket = tmp_path / "ticket"
    ticket.mkdir()
    payload = {"repo": str(repo), "args": ["bash", str(script)], "timeout": 20}
    if report:
        payload["report_dir"] = str(ticket)
    payload.update(extra or {})
    entry = os.environ.get("REPOSITORY_EXECUTOR_ENTRY_BASELINE") or str(
        ROOT / "core/repository_executor_entry.py")
    # These parent values must not enter the real authored subprocess.
    parent = {**os.environ, "PYTHONPATH": "/forbidden-parent-import-root",
              "AITELIER_ADMIN_TOKEN": "parent-token-canary"}
    done = subprocess.run([sys.executable, entry, json.dumps(payload)], cwd=tmp_path,
                          env=parent, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, (done.stdout, done.stderr)
    return json.loads(done.stdout)


def test_real_gate_imports_the_three_trusted_helpers_and_prepares_useful_budgets(tmp_path):
    result = _gate(tmp_path, """
        import hashlib, json, os
        from pathlib import Path
        import project_marker
        from docker.godot import godot_harness as harness
        from docker.godot.godot_harness import (
            _MAX_SPEC_FRAMES, _authored_execution_plan, _valid_process_timeout)
        assert _MAX_SPEC_FRAMES == 3000
        assert _valid_process_timeout(240) and not _valid_process_timeout(True)
        assert not _valid_process_timeout(float('nan'))
        assert project_marker.VALUE == 'project-source-visible'
        assert 'AITELIER_ADMIN_TOKEN' not in os.environ
        assert '/forbidden-parent-import-root' not in os.environ['PYTHONPATH']
        assert '/app' not in os.environ['PYTHONPATH'].split(os.pathsep)
        # The actual game gate's preparation rule: extending one scenario's
        # wall ceiling must keep every default scenario at the original120s.
        prepared = {'scenarios': [
            {'name': 'extended', 'execution_budget':
                {'max_frames': 4000, 'timeout_seconds': 240}},
            {'name': 'default'}]}
        process_timeout = max(120, *(s.get('execution_budget', {}).get(
            'timeout_seconds', 120) for s in prepared['scenarios']))
        for scenario in prepared['scenarios']:
            if 'execution_budget' not in scenario:
                scenario['execution_budget'] = {
                    'max_frames': _MAX_SPEC_FRAMES, 'timeout_seconds': 120}
        plans, errors = _authored_execution_plan(prepared, 180, process_timeout)
        assert errors == [], errors
        assert plans == [(180, 240), (180, 120)], plans
        # Longer authored work is admitted only by an explicit frame budget.
        long = {'frames': 3600, 'scenarios': [{'name': 'long',
            'execution_budget': {'max_frames': 4000, 'timeout_seconds': 240}}]}
        assert _authored_execution_plan(long, 180, 240) == ([(3600, 240)], [])
        assert _authored_execution_plan({'frames': 3600,
            'scenarios': [{'name': 'unbudgeted'}]}, 180, 120)[1]
        assert _authored_execution_plan({'scenarios': [{'name': 'too-slow',
            'execution_budget': {'max_frames': 4000, 'timeout_seconds': 241}}]},
            180, 240)[1]
        module = Path(harness.__file__).resolve()
        assert module == Path('/opt/aitelier-gate-imports/docker/godot/godot_harness.py')
        print(json.dumps({'plans': plans, 'process_timeout': process_timeout,
            'origin': str(module), 'sha256': hashlib.sha256(module.read_bytes()).hexdigest()}))
    """)
    assert not result['timed_out'], result
    assert result['returncode'] == 0, result
    measured = json.loads(result['stdout'])
    assert measured['plans'] == [[180, 240], [180, 120]]
    assert measured['process_timeout'] == 240
    assert measured['sha256'] == hashlib.sha256(
        (ROOT / 'docker/godot/godot_harness.py').read_bytes()).hexdigest()
    assert measured['origin'] == str(EXPORT / 'docker/godot/godot_harness.py')


def test_export_is_narrow_and_backend_source_is_not_an_import_root():
    # Packaging regression: a missing image export fails; it never becomes skip
    # or a fabricated pass. The root contains no backend code or credentials.
    files = {p.relative_to(EXPORT).as_posix() for p in EXPORT.rglob('*') if p.is_file()}
    assert files == {'docker/__init__.py', 'docker/godot/__init__.py',
                     'docker/godot/godot_harness.py'}, files
    assert (EXPORT / 'docker/__init__.py').read_bytes() == b''
    assert (EXPORT / 'docker/godot/__init__.py').read_bytes() == b''


def test_non_gate_child_retains_project_only_import_boundary(tmp_path):
    result = _gate(tmp_path, """
        import json, os
        from pathlib import Path
        import project_marker
        paths = os.environ['PYTHONPATH'].split(os.pathsep)
        assert paths == [str(Path.cwd()), str(Path.cwd() / 'src')], paths
        assert 'AITELIER_ADMIN_TOKEN' not in os.environ
        print(json.dumps({'ok': project_marker.VALUE}))
    """, report=False)
    assert result['returncode'] == 0 and not result['timed_out'], result
    assert json.loads(result['stdout']) == {'ok': 'project-source-visible'}


def test_missing_arbitrary_dependency_is_a_real_child_failure(tmp_path):
    result = _gate(tmp_path, 'import dependency_not_shipped_in_the_reviewed_image')
    assert result['returncode'] != 0 and not result['timed_out'], result
    assert 'ModuleNotFoundError' in result['stderr'], result
    assert 'dependency_not_shipped_in_the_reviewed_image' in result['stderr']
