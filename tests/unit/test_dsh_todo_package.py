"""dsh_todo (integrations/dsh-director/todo/dsh_todo) stands alone, and AItelier still runs it as before.

* Standalone: a python whose import path holds ONLY the package directory (plus the standard
  library and the directory PyYAML lives in; no repository root, no `core`, no site hooks such
  as the editable install's finder) runs `python -m dsh_todo templates` and `list`, and drives a
  template plan from `create` to a candidate on a temporary git repository and plan store.
* Pure: no file of the package imports `core` or names AItelier, except the CLI's existing
  environment contract with the driver runtime (whitelisted below).
* Wired: through the core.todo_* forwards and through the driver's `python -m dsh_todo`
  environment, the hooks are AItelier's own (core.gate_deferral, the same template directory).

Nothing here imports dsh_todo except through the forwards: a bare import before them would
bind the package's default hooks for the rest of the session.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TODO_ROOT = REPO_ROOT / "integrations" / "dsh-director" / "todo"
PACKAGE = TODO_ROOT / "dsh_todo"
MODULES = {"__init__.py", "__main__.py", "cli.py", "plan.py", "runtime.py", "templates.py", "batch.py",
           "hooks.py", "default_hooks.py", "security_jail.py"}
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

#: `python -I -S` ignores PYTHON* variables, the cwd and the user site, and runs no .pth file;
#: the path is then set to exactly the package directory, the standard library and PyYAML's
#: directory. runpy with alter_sys is what `-m` does.
BOOT = """
import importlib.util, runpy, sys
todo, yaml_dir = sys.argv[1], sys.argv[2]
sys.path[:0] = [todo]
sys.path.append(yaml_dir)
for host in ("core", "aitelier", "api"):
    if importlib.util.find_spec(host) is not None:
        sys.exit(f"{host} is importable from {importlib.util.find_spec(host).origin}")
sys.argv = ["dsh_todo", *sys.argv[3:]]
runpy.run_module("dsh_todo", run_name="__main__", alter_sys=True)
"""

MINI = """\
name: mini
version: 1
description: change one file inside a scope
params:
  paths:
    type: list[str]
    required: true
  scope:
    type: list[str]
    required: true
items:
  - id: write
    title: "Write ${paths}"
    predicates:
      - predicate: files_changed
        args: {paths: $paths}
      - predicate: changes_within
        args: {scope: $scope}
"""

#: No exceptions: the package reads its settings as DSH_TODO_<name> through the setting()
#: hook; this deployment's older names live in core/dsh_todo_hooks.py, not in the package.
ALLOWED_NAMES = ()


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV})
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def standalone(*args: str, cwd: Path, env: dict) -> tuple[int, dict]:
    yaml_dir = str(Path(yaml.__file__).resolve().parents[1])
    done = subprocess.run([sys.executable, "-I", "-S", "-c", BOOT, str(TODO_ROOT), yaml_dir, *args],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    assert done.stdout.strip(), f"no stdout (rc={done.returncode}): {done.stderr}"
    return done.returncode, json.loads(done.stdout)


def test_the_package_runs_a_template_plan_with_nothing_but_its_own_directory(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "mini.yaml").write_text(MINI)
    repo, store, out = tmp_path / "ws", tmp_path / "store", tmp_path / "out"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    # No DSH_TODO_HOOKS, no PYTHONPATH: the package's own default hooks.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DSH_TODO_TEMPLATES": str(templates), **_GIT_ENV}

    def ok(*args: str) -> dict:
        code, result = standalone(*args, cwd=tmp_path, env=env)
        assert code == 0, result
        return result

    listed = ok("templates")
    assert listed["directory"] == str(templates)
    assert [t["name"] for t in listed["templates"]] == ["mini"] and "error" not in listed["templates"][0]
    assert ok("list", "--store", str(store)) == {"plans": []}

    created = ok("create", "--store", str(store), "--template", "mini",
                 "--params", json.dumps({"paths": ["app.py"], "scope": ["app.py"]}),
                 "--attempt", "att-1", "--node-key", "execution.demo", "--context-hash", "c" * 64,
                 "--base-sha", base, "--workspace", str(repo), "--goal", "change app.py")
    assert created["items"] == ["write", "deliver"], created

    ok("begin", "--store", str(store), "--attempt", "att-1", "--item", "write")
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "write")["outcome"] == "fail"
    (repo / "app.py").write_text("x = 2\n")
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "write")["outcome"] == "pass"
    git(repo, "commit", "-q", "-am", "work")
    ok("begin", "--store", str(store), "--attempt", "att-1", "--item", "deliver")
    assert ok("done", "--store", str(store), "--attempt", "att-1", "--item", "deliver")["outcome"] == "pass"

    finished = ok("finish", "--store", str(store), "--attempt", "att-1", "--out", str(out))
    assert finished["candidate"] is True and finished["artifact"] == git(repo, "rev-parse", "HEAD")
    report = json.loads(Path(finished["report"]).read_text())
    assert report["status"] == "candidate" and report["artifact"] == finished["artifact"]

    plans = ok("list", "--store", str(store))["plans"]
    assert [row["attempt_id"] for row in plans] == ["att-1"], plans


def test_no_file_of_the_package_imports_core_or_names_aitelier():
    files = sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert {p.name for p in files} == MODULES, "a file was added or lost: review it against this test"
    offenders = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b(from|import)\s+core\b", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
            stripped = line
            for name in ALLOWED_NAMES:
                stripped = stripped.replace(name, "")
            if "aitelier" in stripped.lower():
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_the_core_forwards_are_the_package_modules_with_aitelier_hooks():
    import core.todo_batch
    import core.todo_cli
    import core.todo_plan
    import core.todo_runtime
    import core.todo_templates
    from core import gate_deferral, dsh_todo_hooks
    import dsh_todo.hooks

    for name in ("batch", "cli", "plan", "runtime", "templates"):
        assert sys.modules[f"core.todo_{name}"] is sys.modules[f"dsh_todo.{name}"]
    assert core.todo_runtime.DeferralLedger is gate_deferral.DeferralLedger
    assert core.todo_runtime.read_absence is gate_deferral.read_absence
    assert core.todo_cli.candidate_rejection is dsh_todo_hooks.candidate_rejection
    assert dsh_todo.hooks.TEMPLATES_DIR == REPO_ROOT / "todo_templates"


def test_the_driver_environment_runs_the_cli_with_the_same_hooks_and_templates(tmp_path):
    """What core/dsh_driver.py hands the runtime: cwd TODO_ROOT, `python -m dsh_todo`, CLI_ENV."""
    from core.dsh_todo_hooks import CLI_ENV, TODO_ROOT as WIRED_ROOT
    assert WIRED_ROOT == TODO_ROOT
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DSH_TODO_", "AITELIER_TODO_"))}
    probe = subprocess.run([sys.executable, "-c", "import dsh_todo.hooks as h; "
                            "print(h.DeferralLedger.__module__, h.candidate_rejection.__module__)"],
                           cwd=TODO_ROOT, env={**env, **CLI_ENV}, capture_output=True, text=True, timeout=60)
    assert probe.stdout.split() == ["core.gate_deferral", "core.dsh_todo_hooks"], probe.stderr
    new = subprocess.run([sys.executable, "-m", "dsh_todo", "templates"], cwd=TODO_ROOT, env={**env, **CLI_ENV},
                         capture_output=True, text=True, timeout=120)
    old = subprocess.run([sys.executable, "-m", "core.todo_cli", "templates"], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert new.returncode == old.returncode == 0, (new.stderr, old.stderr)
    assert json.loads(new.stdout) == json.loads(old.stdout)
    assert json.loads(new.stdout)["directory"] == str(REPO_ROOT / "todo_templates")


def test_the_package_reads_only_its_own_settings_and_the_aitelier_hooks_add_the_legacy_names(tmp_path, monkeypatch):
    """Alone, the package's store comes from DSH_TODO_STATE_DIR only: the driver's older
    AITELIER_DSH_STATE_DIR means nothing to it, and with neither the refusal names its own
    variable. Through this deployment's hooks (core/dsh_todo_hooks.py) the older names still
    work, after the package's own: every setting, own name first."""
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    own, legacy = tmp_path / "own", tmp_path / "legacy"
    for d in (own, legacy):
        (d / "todo").mkdir(parents=True)
    code, result = standalone("list", cwd=tmp_path, env={**base, "DSH_TODO_STATE_DIR": str(own)})
    assert code == 0, result
    code, result = standalone("list", cwd=tmp_path, env={**base, "AITELIER_DSH_STATE_DIR": str(legacy)})
    assert code == 2 and "DSH_TODO_STATE_DIR" in json.dumps(result), result
    from core import dsh_todo_hooks
    for name, legacy_name in dsh_todo_hooks.LEGACY_SETTINGS.items():
        monkeypatch.delenv("DSH_TODO_" + name, raising=False)
        monkeypatch.setenv(legacy_name, "legacy")
        assert dsh_todo_hooks.setting(name) == "legacy", name
        monkeypatch.setenv("DSH_TODO_" + name, "own")
        assert dsh_todo_hooks.setting(name) == "own", name
