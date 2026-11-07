"""The image's Python dependencies must come from a committed lock, not from PyPI-at-build-time.

The Dockerfile installed with a bare `pip install -e .`, so the resolver ran on
the build machine on the build day. Two images built from the SAME
pyproject.toml (identical blob, sha256 de5d4b47540d...) then carried different
versions: measured 2026-11-06 on the deployed image e0f264d4cf2e against a
rebuild 42b962ad54, 11 of 95 distributions moved (boto3/botocore, click, jiter,
platformdirs, pydantic, pydantic_core, regex, ruff, typer, typing-inspection).
Same source, different runtime. `requirements.lock` + `-c` is the fix.

This file pins the MECHANISM and never the numbers: it says the lock is what the
image reads, that the lock is a well-formed constraints file, and that it names
a version for everything pyproject.toml declares. It deliberately does not
assert any particular version — a test that hardcoded them would have to be
edited on every deliberate upgrade, which trains people to edit the guard
instead of reading it. What the versions actually come out as can only be
measured inside a built image (pip freeze), and that measurement belongs to the
deployment that builds it.

It reads the Dockerfile rather than the image so a regression fails at review
time, before a rebuild is spent.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK_NAME = "requirements.lock"
LOCK = ROOT / LOCK_NAME

# A constraints line: a distribution name and an exact pinned version, nothing else.
# No `-e`, no bare name, no range, no extras, no environment marker, no URL.
PINNED = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s=<>!~;,]+)$")


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so `PyYAML`/`pyyaml` and `prompt_toolkit`/
    `prompt-toolkit` are the same distribution rather than two."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _instructions() -> list[str]:
    """Every Dockerfile instruction, `\\` continuations joined into one line."""
    instructions: list[str] = []
    current: str | None = None
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        stripped = line.strip()
        if current is None:
            if not stripped or stripped.startswith("#"):
                continue
            current = stripped
        else:
            current = current + " " + stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip() + " "
            continue
        instructions.append(" ".join(current.split()))
        current = None
    if current is not None:
        instructions.append(" ".join(current.split()))
    return instructions


def _pip_installs() -> list[list[str]]:
    """Tokenised argv of every pip install run in the Dockerfile."""
    installs: list[list[str]] = []
    for instruction in _instructions():
        if not instruction.upper().startswith("RUN "):
            continue
        tokens = shlex.split(instruction[4:])
        for index, token in enumerate(tokens):
            # `pip install ...`, `python -m pip install ...`, `/usr/bin/pip3 install ...`
            if Path(token).name.startswith("pip") and tokens[index + 1:index + 2] == ["install"]:
                installs.append(tokens[index:])
                break
    return installs


def _constraint_arguments(argv: list[str]) -> list[str]:
    """What each `-c` / `--constraint` flag in this argv points at."""
    found: list[str] = []
    for index, token in enumerate(argv):
        if token in ("-c", "--constraint"):
            nxt = argv[index + 1:index + 2]
            found.extend(nxt if nxt else [""])
        elif token.startswith("--constraint="):
            found.append(token.split("=", 1)[1])
        elif token.startswith("-c") and token != "-c":
            found.append(token[2:])
    return found


def _lock_entries() -> list[tuple[str, str]]:
    """(name, version) for every line of the lock, plus its blank lines ignored."""
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(LOCK.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        match = PINNED.match(line.strip())
        assert match, (
            f"{LOCK_NAME}:{number} is not an exact `name==version` pin:\n  {line}\n"
            "A constraints file is only a lock if every line names one exact version; "
            "a bare name, a range or an editable line leaves the resolver free, which "
            "is the defect this file exists to prevent.")
        entries.append((match.group("name"), match.group("version")))
    return entries


def _declared_dependencies() -> list[str]:
    """The distribution names `[project].dependencies` declares in pyproject.toml."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names: list[str] = []
    for requirement in data["project"]["dependencies"]:
        # Strip extras (`pyjwt[crypto]>=2.8`), specifiers and markers; keep the name.
        name = re.split(r"[\s;<\>=!~\[(@]", requirement.strip(), maxsplit=1)[0]
        assert name, f"cannot read a distribution name out of {requirement!r}"
        names.append(name)
    return names


def test_the_dockerfile_installs_python_through_the_lock():
    installs = _pip_installs()
    assert installs, "the Dockerfile runs no `pip install`; the image would have no dependencies"
    assert len(installs) == 1, (
        "more than one pip install run in the Dockerfile means a second install path, "
        "and the one that is not constrained can still float anything it resolves:\n  "
        + "\n  ".join(" ".join(argv) for argv in installs))
    argv = installs[0]
    constraints = _constraint_arguments(argv)
    assert constraints, (
        "the image's pip install does not consume a constraints file, so it resolves "
        "from PyPI at build time and two builds of the same pyproject.toml install "
        "different versions (measured 2026-11-06: 11 of 95 distributions):\n  "
        + " ".join(argv))
    assert constraints == [LOCK_NAME], (
        f"the pip install is constrained by {constraints!r}, not by the committed "
        f"{LOCK_NAME}. A lock the image does not read is documentation, not a lock:\n  "
        + " ".join(argv))
    assert "-e" in argv and argv[argv.index("-e") + 1:argv.index("-e") + 2] == ["."], (
        "the project itself must stay an editable install (`-e .`); the image is "
        "bind-mounted at /app at runtime:\n  " + " ".join(argv))
    assert "--no-cache-dir" in argv, (
        "`--no-cache-dir` keeps the pip cache out of the image layer:\n  " + " ".join(argv))


def test_the_lock_is_not_excluded_from_the_build_context():
    """`COPY . /app` is what carries the lock into the image, and .dockerignore
    silently removes paths from it — a locked install then fails at build with
    "requirements.lock not found", which looks like a missing file, not like an
    ignore rule."""
    ignore_file = ROOT / ".dockerignore"
    if not ignore_file.exists():
        return
    for line in ignore_file.read_text().splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!"):
            continue
        # A trailing slash matches directories only, and the lock is a root file.
        if pattern.endswith("/"):
            continue
        if fnmatch.fnmatchcase(LOCK_NAME, pattern) or fnmatch.fnmatchcase(f"./{LOCK_NAME}", pattern):
            raise AssertionError(
                f".dockerignore line {pattern!r} excludes {LOCK_NAME} from the build "
                "context, so `pip install -c requirements.lock` cannot read it inside "
                "the image.")


def test_the_lock_parses_as_exact_pins_only():
    entries = _lock_entries()
    assert entries, f"{LOCK_NAME} is empty; the image would install whatever PyPI serves"
    seen: dict[str, str] = {}
    for name, version in entries:
        key = _normalise(name)
        assert key not in seen, (
            f"{LOCK_NAME} pins {name} twice ({seen[key]} and {version}); pip would "
            "silently honour whichever line it read first")
        seen[key] = version


def test_the_lock_is_sorted():
    """Sorted is not cosmetic: an unsorted lock makes the diff against the image's
    `pip freeze` noisy, and that diff is the only cheap way to see a rebuild drift.

    The order is `pip freeze`'s own: the name with case folded, compared as
    written — NOT PEP 503-normalised, because pip compares the literal characters
    (`-` sorts before `_`, so `pydantic-settings` comes before `pydantic_core`,
    which is the order the image's own freeze prints). Sorting the lock any other
    way would produce a diff for a lock that is in fact correct.
    """
    names = [name for name, _ in _lock_entries()]
    ordered = sorted(names, key=str.lower)
    assert names == ordered, (
        f"{LOCK_NAME} is not sorted by distribution name; first divergence at "
        f"{next(i for i, (a, b) in enumerate(zip(names, ordered)) if a != b)}: "
        f"{next(a for a, b in zip(names, ordered) if a != b)!r} should be "
        f"{next(b for a, b in zip(names, ordered) if a != b)!r}")


def test_the_lock_names_a_version_for_every_declared_dependency():
    """A distribution pyproject.toml asks for but the lock does not mention is
    resolved freely at build time — the exact hole this lock closes."""
    locked = {_normalise(name): version for name, version in _lock_entries()}
    declared = _declared_dependencies()
    assert declared, "pyproject.toml declares no runtime dependencies to lock?"
    missing = [name for name in declared if _normalise(name) not in locked]
    assert not missing, (
        f"{LOCK_NAME} names no version for these dependencies declared in "
        "pyproject.toml:\n  " + "\n  ".join(missing)
        + "\nThey would be resolved by PyPI at build time. Pin them in the lock "
          "rather than removing them from the declaration.")


def test_the_lock_does_not_pin_the_project_itself():
    """`aitelier` is installed from the checkout (`-e .`); a lock entry for it
    would be a constraint the editable install cannot satisfy."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["name"]
    pinned = {_normalise(name) for name, _ in _lock_entries()}
    assert _normalise(project) not in pinned, (
        f"{LOCK_NAME} pins the project itself ({project}), which the image installs "
        "with `-e .` from the checkout. The lock lists what the image INSTALLS FROM "
        "PYPI; the project is excluded on purpose.")
