"""The contract core/dotenv_boot.py actually keeps — parser, tolerances, load order.

Three things are pinned here, each because it was previously asserted nowhere:

1. PARSER vs the loops it replaced. `parse_dotenv` used `str.splitlines()`,
   which also breaks on \\x0b, \\x0c, \\x1c-\\x1e, \\x85, \\u2028 and \\u2029 —
   characters that are NOT newlines to `for _line in _f`, the file iteration of
   the three inline loops this module replaces. A value carrying one of them was
   therefore cut into a bogus second assignment. The parser now splits on "\\n"
   and strips one trailing "\\r" per line (what universal-newline file iteration
   produced for a CRLF file: the line WITHOUT its "\\r").

2. LOAD ORDER, through the real boot entries. `cli/server.py` computes
   `_DEFAULT_PORT` / `_DEFAULT_URL` from os.environ, so `load_repo_dotenv()`
   MUST run before those constants are built; `cli/client.py` likewise. Nothing
   failed if the call moved below the constant that consumes it — the port just
   silently stayed 4444. The witness imports each boot entry in its OWN
   SUBPROCESS, pointed at a temporary copy of the tree whose root `.env` sets
   AITELIER_PORT to a non-default port, so the file the loader reads is the
   fixture and no assertion here touches this process's environment. One child
   per entry, because importing one entry applies the `.env` to its process,
   and a shared child would let the second entry borrow the first one's load.

3. TOLERANCES vs the docstring. Missing path, a directory and an unreadable
   file return None (OSError is swallowed); a file that exists and opens but
   cannot be DECODED raises UnicodeDecodeError at the caller — which is what
   `load_repo_dotenv`'s docstring now says, and what a boot entry lives with.

The child is launched the same way tests/integration/test_api_main_dotenv_isolation.py
does it: the script lives NEXT TO the tree, never inside it, and `sys.path` (not
PYTHONPATH) points it at the copy — the container deliberately keeps /app off
PYTHONPATH.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A port the code never defaults to; `_DEFAULT_PORT` falls back to "4444".
FIXTURE_PORT = "47811"

DOTENV_TEXT = f"# fixture .env for tests/unit/test_dotenv_boot_contract.py\nAITELIER_PORT={FIXTURE_PORT}\n"

# What a boot entry needs to import: its own package and the loader's.
TREE_PACKAGES = ("core", "cli")


# ── the temporary copy the children import from ──────────────────────────


def _copy_boot_tree(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in TREE_PACKAGES:
        shutil.copytree(REPO_ROOT / name, dst / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dst / ".env").write_text(DOTENV_TEXT)
    return dst


@pytest.fixture(scope="module")
def boot_tree(tmp_path_factory) -> Path:
    """A disposable copy of the tree whose ROOT holds the fixture `.env`."""
    copied = _copy_boot_tree(tmp_path_factory.mktemp("dotenv-contract") / "tree")
    assert (copied / "cli" / "server.py").is_file()
    assert (copied / "core" / "dotenv_boot.py").is_file()
    assert (copied / ".env").read_text() == DOTENV_TEXT
    return copied


def _child_env() -> dict:
    """The parent's environment with every decision-maker removed.

    Any AITELIER_* the host exported is dropped so `AITELIER_PORT` can only
    enter through the fixture `.env`, and PYTHONPATH goes too: `sys.path` is
    what points the child at the copy.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("AITELIER_")}
    env.pop("PYTHONPATH", None)
    return env


def _run_child(tree: Path, body: str) -> dict:
    """Run `body` in a child interpreter pointed at `tree`; return its RESULT.

    The child lives NEXT TO the tree, never inside it: python-dotenv's
    find_dotenv() walks up from the running script, so a child inside the tree
    root would have its `.env` applied by the tooling, not by the loader under
    test.
    """
    workdir = tree.parent / "probe"
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "child.py"
    script.write_text("import json, os, sys\n"
                      f"TREE = {str(tree)!r}\n"
                      "sys.path.insert(0, TREE)\n"
                      + textwrap.dedent(body)
                      + "\nprint('RESULT ' + json.dumps(_result))\n")
    proc = subprocess.run([sys.executable, str(script)], cwd=workdir,
                          env=_child_env(), capture_output=True, text=True,
                          timeout=300)
    assert proc.returncode == 0, (
        f"child exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}")
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, f"child produced no RESULT line\n{proc.stdout}\n{proc.stderr[-4000:]}"
    return json.loads(line[-1][len("RESULT "):])


# ── 1. load order through the real boot entries ──────────────────────────


# The module-level constants each boot entry builds from os.environ, and the
# value the fixture `.env` must give them. Each entry is imported ALONE, so a
# constant can only carry the fixture port if that entry's own loader call ran
# before the constant was built.
ENTRY_CONSTANTS = {
    "cli.server": {
        "_DEFAULT_PORT": FIXTURE_PORT,
        "_DEFAULT_URL": f"http://localhost:{FIXTURE_PORT}",
    },
    "cli.client": {
        "_DEFAULT_URL": f"http://localhost:{FIXTURE_PORT}",
    },
}


@pytest.mark.parametrize("entry", sorted(ENTRY_CONSTANTS))
def test_boot_entry_constants_carry_the_env_file_port(boot_tree, entry):
    """Each boot entry builds its URL constants AFTER its own loader call.

    Moving `load_repo_dotenv()` below the constant that consumes it changes
    nothing else that is asserted: the entry would dial the default port while
    the fixture `.env` names another one. Each entry gets its OWN child process,
    and AITELIER_PORT is asserted ABSENT before the import, so the port can only
    arrive through this entry's loader call. (One shared child would not do:
    importing cli.server applies the `.env` to that process, and cli.client
    would then read the port from the environment whether or not its own loader
    call had run.)
    """
    names = sorted(ENTRY_CONSTANTS[entry])
    result = _run_child(boot_tree, f"""
        _before_port = os.environ.get("AITELIER_PORT")
        import core.dotenv_boot as db
        loader_path = str(db.DOTENV_PATH)
        import importlib
        boot = importlib.import_module({entry!r})
        _result = {{
            "entry_file": getattr(boot, "__file__", None),
            "loader_file": loader_path,
            "before_port": _before_port,
            "env_port": os.environ.get("AITELIER_PORT"),
            "constants": {{name: getattr(boot, name) for name in {names!r}}},
        }}
    """)

    # Positive control: the fixture IS the file the loader reads, and it is the
    # copy's, not this checkout's. Without this the assertions below could pass
    # against the real repo or an inherited environment.
    assert result["loader_file"] == str(boot_tree / ".env"), result["loader_file"]
    assert result["entry_file"] == str(boot_tree / Path(*entry.split(".")).with_suffix(".py"))
    assert result["before_port"] is None, (
        f"AITELIER_PORT was already set before {entry} was imported, so this "
        f"child cannot show the loader supplied it: {result}")
    assert result["env_port"] == FIXTURE_PORT, (
        f"AITELIER_PORT did not reach {entry}'s process from the fixture .env: "
        f"{result}")

    for name, expected in ENTRY_CONSTANTS[entry].items():
        got = result["constants"][name]
        assert got == expected, (
            f"{entry}.{name} is {got!r}, not {expected!r} the fixture .env names: "
            f"load_repo_dotenv() in {entry.replace('.', '/')}.py runs after "
            f"{name} is built")


# ── 2. the parser matches the file iteration it replaced ─────────────────


@pytest.mark.parametrize("char", ["\x0b", "\x0c", "\x1c", "\x1e", "\x85", "\u2028", "\u2029"])
def test_parse_dotenv_keeps_no_newline_character_split(char):
    """A value containing a character splitlines() treats as a break survives whole.

    `for _line in _f` — what the three inline loops did — splits on newline
    sequences only, so `A=1<CHAR>B=2` is ONE assignment whose value carries the
    character. `str.splitlines()` made it two (a bogus `B=2`).
    """
    from core.dotenv_boot import parse_dotenv

    text = f"A=1{char}B=2\nC=3\n"
    assert parse_dotenv(text) == {"A": f"1{char}B=2", "C": "3"}, (
        f"a value containing {char!r} was cut apart: {parse_dotenv(text)!r}")


def test_parse_dotenv_vertical_tab_survives_verbatim():
    """The brief's exact target, as a literal: no character is lost or added."""
    from core.dotenv_boot import parse_dotenv

    parsed = parse_dotenv("A=1\x0bB=2\nC=3\n")
    assert parsed == {"A": "1\x0bB=2", "C": "3"}, parsed
    assert parsed["A"] == "1\x0bB=2"


def test_parse_dotenv_still_splits_crlf_like_the_old_file_loop():
    """CRLF files: universal-newline iteration handed back the line without `\\r`.

    Measured on the replaced loop (`for _line in open(path)`): a `A=1\\r\\nB=2\\r\\n`
    file yields `'A=1\\n'`, `'B=2\\n'`. So `B` is a key here, not part of A's
    value, and no value keeps a trailing carriage return.
    """
    from core.dotenv_boot import parse_dotenv

    assert parse_dotenv("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}


def test_parse_dotenv_keeps_every_other_grammar_rule():
    """Nothing else about the grammar moved: comments, export, quotes, dupes, empty keys."""
    from core.dotenv_boot import parse_dotenv

    assert parse_dotenv("A=1\nA=2\n") == {"A": "1"}                 # first wins
    assert parse_dotenv("export B=x\nB=y\n") == {"B": "x"}          # export + repeat
    assert parse_dotenv('# C=3\nD="quoted"\nE=\'single\'\n') == {"D": "quoted", "E": "single"}
    assert parse_dotenv("=orphan\n  = spaced\nOK_KEY=1\n") == {"OK_KEY": "1"}
    assert parse_dotenv("no_equals_here\n\n   \nF=1=2\n") == {"F": "1=2"}


def test_parse_dotenv_raw_text_lone_cr_is_not_a_separator():
    """The residual difference, pinned on RAW text (the only place it exists).

    parse_dotenv splits on "\\n" alone, so a lone "\\r" inside a line stays in the
    value: `A=1\\rB=2\\r` is one assignment, A = "1\\rB=2" (the trailing "\\r" is
    the line's own and is stripped). The replaced file loop (universal newlines)
    would have split it. The loader path does not see this difference, because
    read_text() has already translated the lone "\\r": see
    test_load_repo_dotenv_lone_cr_still_separates_assignments.
    """
    from core.dotenv_boot import parse_dotenv

    assert parse_dotenv("A=1\rB=2\r") == {"A": "1\rB=2"}


# ── 3. the tolerances the docstring states ──────────────────────────────


def test_load_repo_dotenv_returns_none_for_missing_and_directory(tmp_path):
    """Shapes that return None: no such path, and a path that is a directory."""
    from core.dotenv_boot import load_repo_dotenv

    env: dict[str, str] = {}
    assert load_repo_dotenv(path=tmp_path / "absent.env", environ=env) is None
    assert load_repo_dotenv(path=tmp_path, environ=env) is None      # a directory
    assert env == {}, env


def test_load_repo_dotenv_returns_none_for_unreadable_file(tmp_path):
    """An unreadable regular file is OSError too: None, and nothing applied."""
    from core.dotenv_boot import load_repo_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text("UNREADABLE_KEY=1\n")
    dotenv.chmod(0o000)
    try:
        # Precondition, asserted rather than assumed: this must be a process the
        # mode bits apply to (root reads it anyway, and then the case below
        # would prove nothing).
        try:
            dotenv.read_text()
        except OSError:
            pass
        else:
            pytest.fail(f"{dotenv} is readable even with mode 000 — this test "
                        f"needs a non-root process to state anything")

        env: dict[str, str] = {}
        assert load_repo_dotenv(path=dotenv, environ=env) is None
        assert env == {}, env
    finally:
        dotenv.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_load_repo_dotenv_raises_on_a_file_it_cannot_decode(tmp_path):
    """The docstring's other half: undecodable bytes still RAISE at the caller.

    `read_text()` decodes; only OSError is swallowed. So a `.env` holding
    invalid UTF-8 raises UnicodeDecodeError out of `load_repo_dotenv`, i.e. at
    the boot entry that called it — and that process does not start. This pins
    the sentence that replaced the old "a broken `.env` never stops a boot
    entry" claim.
    """
    from core.dotenv_boot import load_repo_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"GOOD_KEY=ok\nBAD_KEY=\xff\xfe\n")

    env: dict[str, str] = {}
    with pytest.raises(UnicodeDecodeError):
        load_repo_dotenv(path=dotenv, environ=env)
    assert env == {}, ("a file that cannot be decoded must apply nothing — "
                       f"the read fails before any assignment: {env}")


def test_load_repo_dotenv_lone_cr_still_separates_assignments(tmp_path):
    """Through the loader, a lone "\\r" still separates two assignments.

    The file holds b'A=1\\rB=2\\r'. read_text() opens in text mode (universal
    newlines), which translates the lone "\\r" to "\\n", so parse_dotenv sees two
    lines and both keys land. This pins the loader path that production uses,
    so the docstring's claim that there is no lone-CR difference through
    load_repo_dotenv is checked, not merely stated.
    """
    from core.dotenv_boot import load_repo_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"A=1\rB=2\r")

    env: dict[str, str] = {}
    assert load_repo_dotenv(path=dotenv, environ=env) == dotenv
    assert env == {"A": "1", "B": "2"}, (
        f"a lone CR no longer separates assignments on the loader path: {env}")


def test_load_repo_dotenv_applies_the_file_and_existing_keys_win(tmp_path):
    """The ordinary case, so the None/raise cases above are not the whole story."""
    from core.dotenv_boot import load_repo_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text("FILE_ONLY=from-file\nBOTH=from-file\n")
    env = {"BOTH": "from-environment"}

    assert load_repo_dotenv(path=dotenv, environ=env) == dotenv
    assert env == {"BOTH": "from-environment", "FILE_ONLY": "from-file"}, env
