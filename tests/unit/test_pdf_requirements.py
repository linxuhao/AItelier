"""The PDF reader's one dependency is pinned by hash and installed apart from the project.

pypdfium2 is imported only by the steward's PDF reader subprocess. It is kept
out of pyproject.toml and requirements.lock on purpose: the project install
(`pip install -c requirements.lock -e .`) must not learn about it, and the
image installs it from requirements-pdf.txt, a single line pinned by sha256.

This file reads the Dockerfile rather than an image, so a regression fails at
review time, before a rebuild is spent. It pins:
  * requirements-pdf.txt is exactly one hash-pinned line;
  * the Dockerfile installs it with --require-hashes, --only-binary=:all: and
    --no-deps, before the `-e .` install and after `COPY . /app`;
  * .dockerignore does not drop the file from the build context;
  * pypdfium2 appears in neither pyproject.toml nor requirements.lock.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQ_NAME = "requirements-pdf.txt"
REQ = ROOT / REQ_NAME
EXPECTED_LINE = (
    "pypdfium2==5.15.2 "
    "--hash=sha256:dc31aea5b0fb9fefc9b537a65f4113980b2c40798445736a7e6565bc85161e37"
)


def _instructions() -> list[str]:
    """Every Dockerfile instruction, `\\` continuations joined, comments dropped."""
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


def _pip_installs() -> list[tuple[int, list[str]]]:
    """(instruction index, argv from `pip` on) for every pip install RUN."""
    installs: list[tuple[int, list[str]]] = []
    for position, instruction in enumerate(_instructions()):
        if not instruction.upper().startswith("RUN "):
            continue
        tokens = shlex.split(instruction[4:])
        for index, token in enumerate(tokens):
            if Path(token).name.startswith("pip") and tokens[index + 1:index + 2] == ["install"]:
                installs.append((position, tokens[index:]))
                break
    return installs


def _requirement_files(argv: list[str]) -> list[str]:
    found: list[str] = []
    for index, token in enumerate(argv):
        if token in ("-r", "--requirement"):
            found.extend(argv[index + 1:index + 2] or [""])
        elif token.startswith("--requirement="):
            found.append(token.split("=", 1)[1])
    return found


def _pdf_install() -> tuple[int, list[str]]:
    matches = [(pos, argv) for pos, argv in _pip_installs() if REQ_NAME in _requirement_files(argv)]
    assert len(matches) == 1, (
        f"the Dockerfile must install {REQ_NAME} exactly once; found {len(matches)}:\n  "
        + "\n  ".join(" ".join(argv) for _, argv in matches))
    return matches[0]


def _project_install() -> tuple[int, list[str]]:
    matches = [
        (pos, argv) for pos, argv in _pip_installs()
        if "-e" in argv and argv[argv.index("-e") + 1:argv.index("-e") + 2] == ["."]
    ]
    assert len(matches) == 1, f"expected one `pip install ... -e .` RUN, found {len(matches)}"
    return matches[0]


def test_requirements_pdf_is_exactly_one_hash_pinned_line():
    raw = REQ.read_text()
    assert raw.splitlines() == [EXPECTED_LINE], (
        f"{REQ_NAME} must hold exactly this one line and nothing else:\n  {EXPECTED_LINE}\n"
        f"found:\n  " + "\n  ".join(raw.splitlines()))
    assert raw.endswith("\n") and not raw.endswith("\n\n")


def test_dockerfile_installs_it_hash_locked_binary_only_without_deps():
    _, argv = _pdf_install()
    for flag in ("--require-hashes", "--only-binary=:all:", "--no-deps", "--no-cache-dir"):
        assert flag in argv, f"the {REQ_NAME} install lacks {flag}:\n  " + " ".join(argv)
    assert "-e" not in argv and "-c" not in argv and "--constraint" not in argv, (
        f"the {REQ_NAME} install must install that file alone:\n  " + " ".join(argv))
    # Nothing but the requirement file: no extra package names on the command line.
    positional = [
        tok for i, tok in enumerate(argv[2:], start=2)
        if not tok.startswith("-") and argv[i - 1] not in ("-r", "--requirement")
    ]
    assert positional == [], f"unexpected install targets {positional!r}:\n  " + " ".join(argv)


def test_it_runs_before_the_project_install_and_after_the_tree_copy():
    pdf_pos, _ = _pdf_install()
    project_pos, _ = _project_install()
    assert pdf_pos < project_pos, (
        f"the {REQ_NAME} install must come before `pip install -c requirements.lock -e .`")
    instructions = _instructions()
    copies = [i for i, ins in enumerate(instructions) if re.fullmatch(r"COPY (\S+ )*\. /app/?", ins)]
    assert copies and copies[0] < pdf_pos, (
        f"`COPY . /app` must come before the {REQ_NAME} install, or the file is not there")


def test_requirements_pdf_is_not_excluded_from_the_build_context():
    ignore_file = ROOT / ".dockerignore"
    if not ignore_file.exists():
        return
    for line in ignore_file.read_text().splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#") or pattern.startswith("!") or pattern.endswith("/"):
            continue
        assert not (fnmatch.fnmatchcase(REQ_NAME, pattern) or fnmatch.fnmatchcase(f"./{REQ_NAME}", pattern)), (
            f".dockerignore line {pattern!r} drops {REQ_NAME} from the build context")


def test_pypdfium2_is_in_neither_pyproject_nor_the_lock():
    for name in ("pyproject.toml", "requirements.lock"):
        text = (ROOT / name).read_text().lower()
        assert "pypdfium" not in text, (
            f"pypdfium2 must not appear in {name}; it lives only in {REQ_NAME}")
