"""The one authentic owned working tree the Coding Mode / dsh-work suites share.

These fixtures used to live in `tests/integration/test_coding_workspace_integration.py`
next to the butler-chat tests that owned them. That backend was retired on
2026-11-17 and the test file went with it; the fixture did not — `test_dsh_project_work.py`
still drives real State work against it, so it lives here where a non-test module
can hold it.

It is the ordinary product path: the real `AItelierSkillFlow` engine, run
isolation (no-checkout registration, raw materialisation), the exclusive
`coding_workspace` admission, closed Git and the RunnerService/SDK checkpoint
CAS. The ONLY substitute is the model: `_stream_llm` replays a deterministic
script of tool calls. No run, claim, origin or admission row is forged.

Requires ``CODING_WORKSPACE_FIXTURE`` (an owned disposable directory); the data
root lives under it so every record can be archived as evidence.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

FIXTURE = os.environ.get("CODING_WORKSPACE_FIXTURE")
# The data root is fixed at IMPORT, before any later-collected module composes
# the app: its DB singletons bind to AITELIER_HOME at their own import. A root
# another conftest already chose (app already imported) is adopted as is.
if FIXTURE and "api.dependencies" not in sys.modules:
    _HOME = Path(FIXTURE) / ("h-" + uuid.uuid4().hex[:8])
    _HOME.mkdir(parents=True, exist_ok=True)
    os.environ["AITELIER_HOME"] = str(_HOME)
RAW = Path(os.environ.get("CODING_WORKSPACE_RAW", "/raw"))
SCRIPTS: dict[str, list] = {}
PALETTES: list[dict] = []


def git(repo, *args, check=True, env=None):
    base = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "f@invalid", "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "f@invalid"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check, env={**base, **(env or {})})


def evidence(name, obj):
    try:
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))
    except OSError:
        pass


@pytest.fixture(scope="session")
def env():
    from core import datadir
    home = datadir.aitelier_home()  # the root the DB singletons were bound to
    os.environ["AITELIER_ZVEC_LIFECYCLE"] = "0"
    os.environ.pop("AITELIER_HOST_LAUNCHER_SOCKET", None)
    from fastapi.testclient import TestClient
    import skillflow
    from api.main import app
    from api.dependencies import get_db_manager, get_skillflow
    from core import coding_workspace as cw, meta_agent
    from core.meta_agent import MetaAgent

    async def scripted(self, messages):
        PALETTES.append({"session": self.session_id,
                         "tools": [d["function"]["name"] for d in self._tool_palette()]})
        turn = SCRIPTS[self.session_id].pop(0)
        if callable(turn):
            turn = await turn() if asyncio.iscoroutinefunction(turn) else turn()
        if isinstance(turn, str):
            yield {"_type": "text_delta", "content": turn}
            yield {"_type": "collected", "text": turn, "tool_calls": [], "usage": {}}
            return
        calls = [{"id": f"call-{uuid.uuid4().hex[:8]}", "name": n, "args": a} for n, a in turn]
        yield {"_type": "collected", "text": "", "tool_calls": calls, "usage": {}}

    MetaAgent._stream_llm = scripted
    app.state._test_mode = True
    identity = {"python": sys.version, "skillflow": skillflow.__file__,
                "skillflow_sha256": hashlib.sha256(Path(skillflow.__file__).read_bytes()).hexdigest(),
                "skillflow_core_sha256": hashlib.sha256(
                    (Path(skillflow.__file__).parent / "core.py").read_bytes()).hexdigest(),
                "coding_workspace": cw.__file__, "meta_agent": meta_agent.__file__,
                "modules": {m: hashlib.sha256(Path(f"/src/{m}").read_bytes()).hexdigest()
                            for m in ("core/coding_workspace.py", "core/meta_agent.py",
                                      "core/closed_git.py", "core/run_resources.py",
                                      "core/skillflow_host.py",
                                      "core/coding_shell.py", "core/run_isolation.py",
                                      "configs/coding_workspace.yaml")},
                "home": str(home), "sys_path": sys.path[:6]}
    evidence("runtime-identity", identity)
    with TestClient(app) as client:
        yield {"client": client, "db": get_db_manager(), "sf": get_skillflow(), "home": home,
               "cw": cw}
    app.state._test_mode = False


@pytest.fixture
def project(env):
    """A real existing-repo project with a dirty Source (tracked edit, staged, untracked)."""
    def make(files=None, dirty=True):
        pid = "cwi-" + uuid.uuid4().hex[:10]
        src = Path(FIXTURE) / "repos" / pid
        src.mkdir(parents=True)
        git(src, "init", "-q", "-b", "main")
        files = files or {"hello.py": "print('helo world')\n", "notes.md": "notes\n",
                          "lib/util.py": "def two():\n    return 2\n"}
        for rel, text in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        git(src, "add", "-A")
        git(src, "commit", "-qm", "base")
        if dirty:
            (src / "notes.md").write_text("notes\nstaged line\n")
            git(src, "add", "notes.md")
            (src / "scratch.txt").write_text("user untracked scratch\n")
        env["db"].ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
        return pid, src
    return make
