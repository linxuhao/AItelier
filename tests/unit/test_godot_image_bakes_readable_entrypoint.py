"""The sidecar image must set the mode of every file it installs under /srv itself.

Dockerfile.godot installs the harness with a plain `COPY`. Docker's COPY keeps the
mode of the file in the build context, so a checkout whose
docker/godot/godot_harness.py is 0600 (umask 077 on the builder) bakes
/srv/godot_harness.py as 0600 root:root. The container runs as uid 1000
(docker-compose.yml `user:`), so python3 cannot open the file and the sidecar dies
at start with EACCES, while the baked sha256 is already correct and an identity
check still passes. Measured 2026-11-08.

This file reads Dockerfile.godot at the repository root, not a copy of it, and
refuses:

  * a COPY or ADD whose destination is under /srv and that carries no explicit mode
    - a `--chmod=` flag on the instruction, or a later `RUN chmod` naming its
    destination. The refusal names the source file;
  * a /srv install whose stated mode leaves it unreadable by other users, because
    the container runs as an arbitrary uid, not as the builder;
  * a Dockerfile with no /srv install, or whose CMD runs a /srv script that no
    COPY/ADD installs, so renaming or deleting the install path cannot pass by
    finding nothing to check.

It reads the Dockerfile rather than the image, so a regression fails at review
time, before a rebuild is spent. Limitation, stated on purpose: a RUN chmod is
matched on absolute destinations only. A relative one is not seen, so the install
is reported as unmoded - the failure is loud, never silent.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.godot"

OCTAL_MODE = re.compile(r"^0?[0-7]{3,4}$")


@dataclass(frozen=True)
class SrvInstall:
    line: int
    keyword: str
    sources: tuple[str, ...]
    dest: str
    chmod: str | None      # value of a --chmod= flag on this instruction, if any
    run_chmod: str | None  # mode of a later RUN chmod naming dest, if any

    @property
    def mode(self) -> str | None:
        return self.chmod or self.run_chmod

    def describe(self) -> str:
        return (f"{DOCKERFILE.name}:{self.line}: {self.keyword} "
                f"{' '.join(self.sources)} -> {self.dest}")


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """(first line, KEYWORD, args) per instruction, `\\` continuations joined.

    Blank and `#` lines are dropped even inside a continuation, as Docker drops them."""
    instructions: list[tuple[int, str, str]] = []
    start: int | None = None
    current = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if start is None:
            start = number
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        keyword, _, args = current.partition(" ")
        instructions.append((start, keyword.upper(), args.strip()))
        start, current = None, ""
    if current:
        keyword, _, args = current.partition(" ")
        instructions.append((start or 0, keyword.upper(), args.strip()))
    return instructions


def _split_args(args: str) -> tuple[list[str], list[str]]:
    """Leading `--flag`s, then the operands in shell or JSON exec form."""
    flags: list[str] = []
    rest = args.strip()
    while rest.startswith("--"):
        flag, _, rest = rest.partition(" ")
        flags.append(flag)
        rest = rest.strip()
    operands = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
    return flags, operands


def _chmod_mode_naming(run_args: str, dest: str) -> str | None:
    """The mode a RUN's `chmod` gives `dest`, or None when this RUN does not name it."""
    if "chmod" not in run_args:
        return None
    if run_args.lstrip().startswith("["):
        segments = [" ".join(json.loads(run_args))]
    else:
        # `&&`, `||`, `;` and `|` each start a new command in the same RUN.
        segments = re.split(r"&&|\|\||;|\|", run_args)
    for segment in segments:
        tokens = [token.strip("'\"") for token in segment.split()]
        for index, token in enumerate(tokens):
            if posixpath.basename(token) != "chmod":
                continue
            values = [t for t in tokens[index + 1:] if not t.startswith("-")]
            if len(values) < 2:
                continue
            mode, targets = values[0], values[1:]
            if any(posixpath.normpath(target) == dest for target in targets):
                return mode
    return None


def _run_chmod_after(instructions: list[tuple[int, str, str]], index: int, dest: str) -> str | None:
    """A RUN chmod naming dest later in the same stage. A COPY that runs before the
    RUN is what that RUN re-modes; one that runs after it overwrites the mode, so
    only RUNs after the COPY count."""
    for _, keyword, args in instructions[index + 1:]:
        if keyword == "FROM":
            break
        if keyword == "RUN":
            mode = _chmod_mode_naming(args, dest)
            if mode is not None:
                return mode
    return None


def _installs(text: str) -> list[SrvInstall]:
    """Every COPY/ADD whose resolved destination is /srv or under it."""
    instructions = _instructions(text)
    installs: list[SrvInstall] = []
    workdir = "/"
    for index, (line, keyword, args) in enumerate(instructions):
        if keyword == "FROM":
            workdir = "/"
        elif keyword == "WORKDIR":
            workdir = posixpath.normpath(posixpath.join(workdir, args))
        elif keyword in ("COPY", "ADD"):
            flags, operands = _split_args(args)
            if len(operands) < 2:
                continue
            sources, raw_dest = tuple(operands[:-1]), operands[-1]
            dest = posixpath.normpath(posixpath.join(workdir, raw_dest))
            if dest != "/srv" and not dest.startswith("/srv/"):
                continue
            chmod = next((f.split("=", 1)[1] for f in flags if f.startswith("--chmod=")), None)
            installs.append(SrvInstall(
                line=line, keyword=keyword, sources=sources, dest=dest,
                chmod=chmod,
                run_chmod=_run_chmod_after(instructions, index, dest)))
    return installs


def _cmd_script(text: str) -> str | None:
    """The /srv script the final CMD runs, or None when the CMD names none."""
    cmd = [args for _, keyword, args in _instructions(text) if keyword == "CMD"]
    if not cmd:
        return None
    _, operands = _split_args(cmd[-1])
    for operand in operands:
        if operand.startswith("/srv/") and operand.endswith(".py"):
            return posixpath.normpath(operand)
    return None


def _no_install_message(text: str) -> str | None:
    """Why this Dockerfile installs nothing the sidecar can run, or None when it does."""
    installs = _installs(text)
    if not installs:
        return (f"{DOCKERFILE.name} has no COPY or ADD under /srv. The sidecar runs its "
                "harness from /srv, so a renamed or deleted install path must be refused "
                "here rather than pass because there was nothing to check.")
    script = _cmd_script(text)
    if script is not None and script not in {install.dest for install in installs}:
        return (f"{DOCKERFILE.name}'s CMD runs {script}, but no COPY or ADD installs that "
                "path under /srv, so the container starts a file it never placed.")
    return None


def _unmoded_messages(text: str) -> list[str]:
    """One refusal per /srv install that carries no explicit mode, naming its source."""
    return [
        f"{install.describe()} carries no explicit mode, so it keeps the builder's "
        "working-copy mode: a 0600 checkout bakes 0600 root:root, and the uid-1000 "
        f"container dies with EACCES. Source file: {', '.join(install.sources)}. "
        f"Add --chmod=0644 to that COPY, or name {install.dest} in a RUN chmod."
        for install in _installs(text) if install.mode is None
    ]


def _unreadable_messages(text: str) -> list[str]:
    """One refusal per /srv install whose stated mode other users cannot read."""
    problems: list[str] = []
    for install in _installs(text):
        if install.mode is None:
            continue  # reported by _unmoded_messages
        if not OCTAL_MODE.match(install.mode):
            problems.append(
                f"{install.describe()}: mode {install.mode!r} is not octal, so its "
                "readability cannot be measured; write it as 0644")
        elif not int(install.mode, 8) & 0o004:
            problems.append(
                f"{install.describe()}: mode {install.mode} is unreadable by other users, "
                "and the container runs as uid 1000, not as the builder. Source file: "
                f"{', '.join(install.sources)}")
    return problems


def test_the_sidecar_installs_the_script_it_runs_under_srv():
    message = _no_install_message(DOCKERFILE.read_text())
    assert message is None, message


def test_every_file_installed_under_srv_says_its_mode_in_the_dockerfile():
    problems = _unmoded_messages(DOCKERFILE.read_text())
    assert not problems, "\n".join(problems)


def test_every_file_installed_under_srv_is_readable_by_other_users():
    problems = _unreadable_messages(DOCKERFILE.read_text())
    assert not problems, "\n".join(problems)


def test_the_checks_refuse_the_defect_and_accept_each_explicit_form():
    """Self-check: the real assertions above must be able to fail, and must name
    the offending source. Each case is a synthetic Dockerfile judged by the same
    helpers the real-file tests use."""
    base = "FROM python:3.12-slim\nCMD [\"python3\", \"/srv/godot_harness.py\", \"--serve\"]\n"

    bare = base + "COPY docker/godot/godot_harness.py /srv/godot_harness.py\n"
    problems = _unmoded_messages(bare)
    assert len(problems) == 1 and "docker/godot/godot_harness.py" in problems[0], problems
    assert _no_install_message(bare) is None

    pinned = base + "COPY --chmod=0644 docker/godot/godot_harness.py /srv/godot_harness.py\n"
    assert _unmoded_messages(pinned) == [] and _unreadable_messages(pinned) == []

    continued = (base + "COPY --chmod=0644 \\\n    docker/godot/godot_harness.py \\\n"
                 "    /srv/godot_harness.py\n")
    assert _unmoded_messages(continued) == [] and _unreadable_messages(continued) == []

    named = (base + "COPY docker/godot/godot_harness.py /srv/godot_harness.py\n"
                    "RUN chmod 0644 /srv/godot_harness.py\n")
    assert _unmoded_messages(named) == [] and _unreadable_messages(named) == []

    unreadable = base + "COPY --chmod=0600 docker/godot/godot_harness.py /srv/godot_harness.py\n"
    problems = _unreadable_messages(unreadable)
    assert len(problems) == 1 and "0600" in problems[0], problems

    # A RUN chmod for a different path, or one that runs before the COPY, sets nothing.
    other_path = base + "COPY a.py /srv/godot_harness.py\nRUN chmod 0644 /srv/other.py\n"
    assert len(_unmoded_messages(other_path)) == 1
    before = base + "RUN chmod 0644 /srv/godot_harness.py\nCOPY a.py /srv/godot_harness.py\n"
    assert len(_unmoded_messages(before)) == 1

    # The relative form resolves against WORKDIR; a path outside /srv is not an install.
    relative = ("FROM python:3.12-slim\nWORKDIR /srv\n"
                "COPY --chmod=0644 a.py ./godot_harness.py\n"
                'CMD ["python3", "/srv/godot_harness.py"]\n')
    assert _unmoded_messages(relative) == [] and _no_install_message(relative) is None
    elsewhere = "FROM python:3.12-slim\nCOPY a.py /tmp/a.py\n"
    assert _installs(elsewhere) == []
    assert "no COPY or ADD under /srv" in (_no_install_message(elsewhere) or "")

    # A CMD that runs a /srv script nothing installs is refused.
    unplaced = ("FROM python:3.12-slim\nCOPY --chmod=0644 a.py /srv/other.py\n"
                'CMD ["python3", "/srv/godot_harness.py"]\n')
    assert "no COPY or ADD installs" in (_no_install_message(unplaced) or "")
