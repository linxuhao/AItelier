"""The app must not read the tree's `.env` at import; the BOOT entries still do.

Two things are asserted here, and they are deliberately different:

ISOLATION — importing the app adds NO environment key. `api/main.py` used to
read `<repo root>/.env` at import time (before `from core import cf_access`,
which reads AITELIER_CF_TEAM_DOMAIN / AITELIER_CF_AUD once). So any process
that merely imported the app wore the deployment's write gate: on 2026-11-08 two
integration tests were refused `403 write_denied_not_authenticated` on a host
checkout whose root has an untracked `.env`, while the same tests passed in a
worktree that has none (iss-f909af237ae944af).

BOOT — the loader must still WORK where it is genuinely needed. Moving it out
of api/main.py is only correct if the CLI boot entries keep applying it, so a
marker key in the tree's `.env` must still reach the environment of a process
that boots `cli.server` / `cli.client` (and it must arrive AFTER importing the
app, not during it), with an already-set variable still winning.

Everything that touches the environment runs in subprocesses: the fixture `.env`
is real (written where the loader looks — a temporary COPY of the tree), so the
parent pytest process's own environment is never touched, and no assertion here
is about a function merely existing. The few in-process checks at the end call
`parse_dotenv` / `load_repo_dotenv(environ=...)` on a plain dict and never write
to os.environ.

DUPLICATES — the fixture `.env` assigns DUP_KEY twice, first `first` then
`second`. The three inline loops this module replaces kept the FIRST value
(`if key not in os.environ` / `os.environ.setdefault`), and the boot path must
too. A last-wins parser would make the same file yield "second" — measured on
2026-11-08 as "first" under `import api.main` at 7c22e42f47 and "second" under a
last-wins core/dotenv_boot.py, which is why it is asserted both through the boot
path and directly against parse_dotenv.

Two details of how the children are launched are load-bearing, not style:

* The child script lives NEXT TO the tree, never in it, and the child is never
  `python -c`. python-dotenv's `find_dotenv()` walks up from the running script
  — and treats `python -c` / a REPL as "use the cwd" — while `litellm/__init__`
  calls `load_dotenv()` at import. A child sitting in the tree root would
  therefore have its `.env` applied by LITELLM, and this test would report the
  opposite of the truth.
* `sys.path` is what points the child at the copied tree (passed as argv), not
  PYTHONPATH: the container deliberately keeps `/app` off PYTHONPATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The keys a deployment's `.env` carries for the write gate — the ones whose
# import-time injection turned two integration tests into 403s.
GATE_KEYS = (
    "AITELIER_CF_TEAM_DOMAIN",
    "AITELIER_CF_AUD",
    "AITELIER_WRITERS",
    "AITELIER_ADMIN_TOKEN",
)

# Written by the loader and set by nobody else: its presence is proof the file
# WAS applied, its absence proof it was not.
MARKER = "DOTENV_ISOLATION_MARKER"
EXISTING_WINS = "DOTENV_EXISTING_WINS"

# A key assigned twice. The first line must win (see the module docstring).
DUP_KEY = "DOTENV_DUP_KEY"
DUP_FIRST = "first"
DUP_SECOND = "second"

DOTENV_TEXT = f"""# fixture .env for tests/integration/test_api_main_dotenv_isolation.py
AITELIER_CF_TEAM_DOMAIN=fixture-team.cloudflareaccess.com
AITELIER_CF_AUD=fixture-aud
export AITELIER_WRITERS=fixture@example.com
AITELIER_ADMIN_TOKEN="fixture-admin-token"
{MARKER}=marker-from-dotenv
{EXISTING_WINS}=file-value
{DUP_KEY}={DUP_FIRST}
# a comment between the two assignments of the same key
{DUP_KEY}={DUP_SECOND}
"""

# What `import api.main` needs to resolve at import time: its own package and
# the modules its routers/scheduler import. Copied (not symlinked) so that
# `Path(__file__).resolve()` inside the copy points at the copy — a symlinked
# tree would resolve back to /app and the fixture `.env` would never be read.
TREE_PACKAGES = ("api", "core", "models", "aitelier", "cli",
                 "configs", "agent_configs", "templates")
TREE_FILES = ("llm_providers.example.json", "model_routes.example.json")


def _copy_tree(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in TREE_PACKAGES:
        shutil.copytree(REPO_ROOT / name, dst / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in TREE_FILES:
        shutil.copy2(REPO_ROOT / name, dst / name)
    (dst / ".env").write_text(DOTENV_TEXT)
    return dst


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Path:
    """A disposable copy of the tree whose ROOT has a fixture `.env`."""
    root = tmp_path_factory.mktemp("dotenv-isolation")
    copied = _copy_tree(root / "tree")
    assert (copied / "api" / "main.py").is_file()
    assert (copied / ".env").read_text() == DOTENV_TEXT
    return copied


def _child_env(**extra: str) -> dict:
    """The parent's environment, minus anything that would decide the answer.

    Any AITELIER_* the host exported is dropped, so "absent before" is a real
    statement and not an artefact of the machine this runs on; the fixture keys
    then enter ONLY through a `.env` on disk.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("AITELIER_")}
    env.pop("PYTHONPATH", None)
    env.pop(MARKER, None)
    env.pop(EXISTING_WINS, None)
    env.pop(DUP_KEY, None)
    env.update(extra)
    return env


def _run_child(tree: Path, body: str, **extra_env: str) -> dict:
    """Run `body` in a child interpreter pointed at `tree`; return its RESULT."""
    # NEXT TO the tree, never inside it: see the module docstring (litellm loads
    # the `.env` of the running script's directory or, under `-c`, of the cwd).
    workdir = tree.parent / "probe"
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "child.py"
    script.write_text("import json, os, sys\n"
                      f"TREE = {str(tree)!r}\n"
                      "sys.path.insert(0, TREE)\n"
                      f"KEYS = {list(GATE_KEYS + (MARKER, EXISTING_WINS, DUP_KEY))!r}\n"
                      + textwrap.dedent(body)
                      + "\nprint('RESULT ' + json.dumps(_result))\n")
    proc = subprocess.run([sys.executable, str(script)], cwd=workdir,
                          env=_child_env(**extra_env), capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}")
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"child produced no RESULT line\n{proc.stdout}\n{proc.stderr[-4000:]}"
    return json.loads(line[-1][len("RESULT "):])


def test_importing_the_app_adds_no_environment_key(tree):
    """`import api.main` must leave os.environ byte-for-byte as it found it.

    The tree root holds a `.env` setting the four write-gate keys (and a marker),
    i.e. exactly the file the old import-time read consumed.
    """
    result = _run_child(tree, """
        before = sorted(os.environ)
        import api.main
        _after = sorted(os.environ)
        from core.dotenv_boot import load_repo_dotenv
        applied = load_repo_dotenv()          # the fixture is REAL: control below
        _result = {
            "api_main_file": getattr(api.main, "__file__", None),
            "before": before,
            "after": _after,
            "control_path": str(applied) if applied else None,
            "control_values": {k: os.environ.get(k) for k in KEYS},
        }
    """)

    # The copy's own main.py was imported — otherwise this would be a statement
    # about some other file.
    assert result["api_main_file"] == str(tree / "api" / "main.py"), result["api_main_file"]

    before, after = set(result["before"]), set(result["after"])
    for key in GATE_KEYS + (MARKER,):
        assert key not in before, f"{key} was already in the child env; the fixture is not clean"
        assert key not in after, (
            f"importing api.main put {key} into the environment — a process that "
            f"imports the app must not inherit the tree's .env "
            f"(iss-f909af237ae944af)")

    # Strongest form: no key appears or disappears at all, not just the four.
    added, removed = sorted(after - before), sorted(before - after)
    assert not added, f"importing api.main added environment keys: {added}"
    assert not removed, f"importing api.main removed environment keys: {removed}"

    # Positive control: the fixture `.env` IS at the path the shared loader
    # reads, and it does carry these keys. Without this, a `.env` written
    # somewhere nobody looks would make the assertions above vacuous.
    assert result["control_path"] == str(tree / ".env"), result["control_path"]
    for key in GATE_KEYS + (MARKER,):
        assert result["control_values"][key], (
            f"fixture .env does not reach the loader for {key}: {result['control_values']}")


@pytest.mark.parametrize("entry", ["cli.server", "cli.client"])
def test_boot_entry_still_applies_the_tree_dotenv(tree, entry):
    """The loader was MOVED, not deleted: the CLI boot entries still apply it.

    Ordering is asserted too: importing the app first must not apply the file,
    and importing the boot entry afterwards must — that is what makes this a
    statement about the move rather than about the loader being gone.
    """
    result = _run_child(tree, f"""
        _before = {{k: os.environ.get(k) for k in KEYS}}
        import api.main
        _after_app = {{k: os.environ.get(k) for k in KEYS}}
        import importlib
        boot = importlib.import_module({entry!r})
        _after_boot = {{k: os.environ.get(k) for k in KEYS}}
        _result = {{
            "entry": {entry!r},
            "entry_file": getattr(boot, "__file__", None),
            "before": _before,
            "after_app_import": _after_app,
            "after_boot": _after_boot,
        }}
    """, **{EXISTING_WINS: "environment-value"})

    assert result["entry_file"] == str(tree / Path(*entry.split(".")).with_suffix(".py"))

    # Nothing before the boot entry ran.
    for phase in ("before", "after_app_import"):
        for key in GATE_KEYS + (MARKER,):
            assert result[phase][key] is None, (
                f"{key} was applied during {phase}: {result[phase]}")

    # The boot entry applied the tree's .env...
    booted = result["after_boot"]
    assert booted["AITELIER_CF_TEAM_DOMAIN"] == "fixture-team.cloudflareaccess.com", booted
    assert booted["AITELIER_CF_AUD"] == "fixture-aud", booted
    assert booted["AITELIER_WRITERS"] == "fixture@example.com", booted            # `export ` line
    assert booted["AITELIER_ADMIN_TOKEN"] == "fixture-admin-token", booted        # quoted value
    assert booted[MARKER] == "marker-from-dotenv", booted

    # ...and an environment variable that was already set still wins.
    assert booted[EXISTING_WINS] == "environment-value", booted


@pytest.mark.parametrize("entry", ["cli.server", "cli.client"])
def test_repeated_key_keeps_first_value_through_boot_path(tree, entry):
    """A key assigned twice in the tree `.env` reaches the boot entry as its FIRST value.

    The fixture assigns DUP_KEY as `first`, then a comment, then `second`. The
    inline loops this replaces kept `first`; a last-wins parser keeps `second`.
    """
    result = _run_child(tree, f"""
        import api.main
        _after_app = os.environ.get({DUP_KEY!r})
        import importlib
        importlib.import_module({entry!r})
        _result = {{
            "after_app_import": _after_app,
            "after_boot": os.environ.get({DUP_KEY!r}),
        }}
    """)

    assert result["after_app_import"] is None, result
    assert result["after_boot"] == DUP_FIRST, (
        f"{entry}: repeated key took the LAST assignment, not the first: {result}")


def test_parse_dotenv_keeps_first_value_for_repeated_key():
    """Direct statement about the parser: a repeated key keeps its first value.

    The other fixture keys are parsed too, so the file is not merely dropped in
    part — the duplicate is the only thing this case is about.
    """
    from core.dotenv_boot import parse_dotenv

    parsed = parse_dotenv(DOTENV_TEXT)
    assert parsed[DUP_KEY] == DUP_FIRST, parsed
    assert parsed[MARKER] == "marker-from-dotenv", parsed
    assert parsed["AITELIER_ADMIN_TOKEN"] == "fixture-admin-token", parsed
    assert parsed["AITELIER_WRITERS"] == "fixture@example.com", parsed    # `export ` line

    assert parse_dotenv("A=1\nA=2\nA=3\n") == {"A": "1"}
    assert parse_dotenv("export B=x\nB=y\n") == {"B": "x"}                # export + repeat


def test_load_repo_dotenv_repeated_key_and_existing_variable(tmp_path):
    """The loader applies the first value and still never overwrites an existing variable.

    Runs in-process on a plain dict passed as `environ`, so os.environ is untouched.
    """
    from core.dotenv_boot import load_repo_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{DUP_KEY}={DUP_FIRST}\n{DUP_KEY}={DUP_SECOND}\n"
                      f"{EXISTING_WINS}=file-value\n")
    env = {EXISTING_WINS: "environment-value"}

    applied = load_repo_dotenv(path=dotenv, environ=env)

    assert applied == dotenv
    assert env[DUP_KEY] == DUP_FIRST, env
    assert env[EXISTING_WINS] == "environment-value", env


def test_load_repo_dotenv_skips_empty_key_and_tolerates_unreadable_path(tmp_path):
    """Two deliberate tolerances the old inline loops did not have.

    A line whose key is empty is skipped (the old loop raised OSError
    [Errno 22] from os.environ). A `.env` path that is not a readable regular
    file — here a directory — returns None (the old `open()` raised).
    """
    from core.dotenv_boot import load_repo_dotenv, parse_dotenv

    assert parse_dotenv("=orphan\n  = spaced\nOK_KEY=1\n") == {"OK_KEY": "1"}

    env: dict[str, str] = {}
    assert load_repo_dotenv(path=tmp_path, environ=env) is None   # tmp_path is a directory
    assert env == {}
    assert load_repo_dotenv(path=tmp_path / "absent.env", environ=env) is None
    assert env == {}
