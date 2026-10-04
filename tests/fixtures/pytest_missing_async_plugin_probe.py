import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import aitelier.tools.run_tests.impl as rt

assert importlib.util.find_spec("pytest") is not None
assert importlib.util.find_spec("pytest_asyncio") is None
print("PRECONDITION pytest present; pytest_asyncio absent", flush=True)
with tempfile.TemporaryDirectory(prefix="deputy-async-repo-") as directory:
    repo = Path(directory)
    (repo / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (repo / "test_async.py").write_text(
        "import asyncio\nfrom pathlib import Path\n"
        "async def test_body_executes():\n"
        "    await asyncio.sleep(0)\n"
        "    Path('async-body-executed').write_text('executed')\n"
        "    assert 6 * 7 == 42\n")
    report = {}
    resolved, cleanup = rt._resolve_pytest_python(repo, report)
    print(json.dumps({"resolved": resolved, "current": sys.executable,
                      "cleanup": cleanup, "report": report}), flush=True)
    try:
        result = subprocess.run([resolved, "-m", "pytest", "-q"],
                                cwd=repo, text=True, capture_output=True)
        print(result.stdout, flush=True)
        print(result.stderr, flush=True)
        print("ASYNC_EXIT", result.returncode, flush=True)
        assert result.returncode == 0
        assert (repo / "async-body-executed").read_text() == "executed"
        assert resolved != sys.executable
        assert cleanup and Path(cleanup).exists()
        print("ASYNC_BODY_EXECUTED", flush=True)
    finally:
        if cleanup:
            shutil.rmtree(cleanup)
            assert not Path(cleanup).exists()
            print("RESOLVER_VENV_CLEANED", flush=True)
    # Exercise caller-owned cleanup with real provisioning and actual async body.
    (repo / "async-body-executed").unlink()
    caller_out = repo / "out"
    created = []
    original_mkdtemp = rt.tempfile.mkdtemp
    def track_venv(**kwargs):
        path = original_mkdtemp(**kwargs)
        created.append(Path(path))
        return path
    rt.tempfile.mkdtemp = track_venv
    res = rt.run_tests(project_root=str(repo), out_dir=str(caller_out))
    assert res["passed"] is True
    report = json.loads((caller_out / "test_report.json").read_text())
    assert report["returncode"] == 0
    assert (repo / "async-body-executed").exists()
    assert created and all(not path.exists() for path in created)
    print("REAL_CALL_PATH_PASS_AND_CLEANUP", flush=True)
