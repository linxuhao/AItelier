"""Run bounded, reproducible test batches without touching production state."""
from __future__ import annotations
import argparse
import ctypes
import time
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("suite", choices=["unit", "other", "engine"])
parser.add_argument("--part", type=int, default=0)
parser.add_argument("--tests", nargs="+")
parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument("--engine", type=Path, default=Path("/home/linxuhao/stepflow/.claude/worktrees/artifact-revision-20260912"))
parser.add_argument("--installed", action="store_true")
parser.add_argument("--package-root", type=Path)
parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
args = parser.parse_args()
args.repo = args.repo.resolve()
args.engine = args.engine.resolve()
args.out = args.out.resolve()
repo = args.engine if args.suite == "engine" else args.repo
if args.suite == "unit":
    files = sorted(str(p.relative_to(repo)) for p in (repo / "tests/unit").glob("test_*.py"))
    selected = files[args.part::4]
elif args.suite == "other":
    selected = sorted(str(p.relative_to(repo)) for p in (repo / "tests").rglob("test_*.py")
                      if "unit" not in p.relative_to(repo).parts)
else:
    selected = ["tests", "src/skillflow/plugins"]
if args.tests:
    selected = args.tests
assert selected, "No selected tests"
tools = Path("/tmp/aitelier-output-migration")
env = dict(os.environ)
env.update(PATH=f"{tools}/git/usr/bin:{tools}/venv/bin:" + env["PATH"],
           GIT_EXEC_PATH=f"{tools}/git/usr/lib/git-core",
           GIT_TEMPLATE_DIR=f"{tools}/git/usr/share/git-core/templates",
           GIT_CONFIG_GLOBAL=f"{tools}/gitconfig", HOME=f"{tools}/takeover-test-home")
env.setdefault("SEARXNG_URL", "http://searxng.test.invalid")  # HTTP is mocked by unit tests
env["PYTHONPATH"] = str(repo) if args.installed else f"{args.engine}/src:{repo}"
if args.package_root is not None:
    assert args.installed
    env["PYTHONPATH"] = f"{args.package_root}:" + env["PYTHONPATH"]
args.out.mkdir(parents=True, exist_ok=True)
label = f"{args.suite}-{args.part}-{'wheel' if args.installed else 'source'}"
command = [sys.executable, "-m", "pytest", *selected, "-q", "--disable-warnings", "--tb=short",
           "--junitxml=" + str(args.out / (label + ".xml"))]
# This tool container does not promptly reap orphaned test grandchildren.
# Own/reap only descendants of THIS harness, mirroring an init process. Do not
# change product cancellation logic or signal any unrelated process.
subreaper = False
if sys.platform.startswith("linux"):
    subreaper = ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
with (args.out / (label + ".log")).open("w") as stream:
    process = subprocess.Popen(command, cwd=repo, env=env, stdout=stream, stderr=subprocess.STDOUT)
    while process.poll() is None:
        if subreaper:
            children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().split()
            for child in children:
                if int(child) != process.pid:
                    try:
                        os.waitpid(int(child), os.WNOHANG)
                    except ChildProcessError:
                        pass
        time.sleep(0.02)
    result = subprocess.CompletedProcess(command, process.returncode)
print(f"{label}: {len(selected)} selected files/directories; exit={result.returncode}")
lines = (args.out / (label + ".log")).read_text().splitlines()
print("\n".join(lines[-55:]))
raise SystemExit(result.returncode)
