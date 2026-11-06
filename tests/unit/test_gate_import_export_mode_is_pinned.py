"""The image-owned gate client must be readable by the user the backend runs as.

Every authored repository gate imports /opt/aitelier-gate-imports/docker/godot/
godot_harness.py before it can measure anything. A plain COPY preserves the BUILD
HOST's file mode, and this host's working tree carries that file as 0600 (umask
077; git says 100644, so only a fresh checkout would bake 0644). The image baked
0600 root:root while the backend runs as uid 1000, so the import raised
PermissionError - measured in the live container:

    $ docker exec -u 1000 aitelier head -c1 /opt/.../godot_harness.py
    Permission denied

That is invisible until a gate runs, and a gate that cannot run looks like a gate
that failed. This test reads the Dockerfile rather than the image so it can fail
at review time, before a rebuild is spent.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORT = "/opt/aitelier-gate-imports"
TARGET = "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"


def test_the_baked_gate_client_pins_its_mode():
    text = (ROOT / "Dockerfile").read_text()
    copies = [ln for ln in text.splitlines()
              if ln.strip().upper().startswith("COPY") and TARGET in ln]
    assert copies, f"no COPY bakes {TARGET}; the gate client must come from the image"
    for line in copies:
        assert re.search(r"--chmod=0?6?4?4|--chmod=0[0-7]{3}", line), (
            f"this COPY inherits the build host's mode and can bake 0600 root:root:\n  {line}\n"
            "Pin it with --chmod=0644: the backend runs as uid 1000 and cannot read 0600.")


def test_no_other_opt_copy_of_repository_source_is_left_unpinned():
    """Same class of bug: an image-owned copy of repository source read by a
    non-root runtime must not inherit the host's mode."""
    text = (ROOT / "Dockerfile").read_text()
    offenders = [ln for ln in text.splitlines()
                 if ln.strip().upper().startswith("COPY") and "/opt/" in ln
                 and "--chmod" not in ln and "--from=" not in ln]
    assert not offenders, (
        "these COPY lines bake repository source outside /app without pinning a mode:\n  "
        + "\n  ".join(offenders))


def _run_commands(text: str) -> list[str]:
    """Every RUN command, with its `\\` continuations joined into one line."""
    commands: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            if stripped.upper().startswith("RUN "):
                current = stripped[4:]
            else:
                continue
        else:
            current = current + " " + stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip() + " "
            continue
        commands.append(" ".join(current.split()))
        current = None
    if current is not None:
        commands.append(" ".join(current.split()))
    return commands


def _simple_commands(command: str) -> list[list[str]]:
    """Split one RUN into its simple commands as words, on `&&`, `||` and `;`."""
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:                      # a quoting shape shlex rejects must not crash this file
        tokens = command.split()
    words: list[str] = []
    for token in tokens:
        for separator in ("&&", "||", ";"):
            token = token.replace(separator, f" {separator} ")
        words.extend(part for part in token.split() if part)
    commands: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if word in ("&&", "||", ";"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(word)
    if current:
        commands.append(current)
    return commands


def _is_recursive(flags: list[str]) -> bool:
    """True for `-R`, `--recursive`, and every combined short flag that carries R.

    `chmod -Rf 0755 <dir>` is as recursive as `chmod -R 0755 <dir>`: GNU chmod reads
    a short-flag cluster letter by letter. Matching the literal word `-R` read the
    combined spelling as NON-recursive, which left `chmod -Rf 0755
    /opt/aitelier-gate-imports` green while it took godot_harness.py to 0755 (found
    by the non-author review of the first version of this test, and reproduced).
    `-r` is kept as recursive even though GNU reads it as "remove read": the guard
    fails closed where it is unsure.
    """
    for flag in flags:
        if flag == "--recursive":
            return True
        if flag == "-r":
            return True
        if flag.startswith("-") and not flag.startswith("--") and "R" in flag[1:]:
            return True
    return False


def _unparsed_mode_instructions(text: str) -> list[str]:
    """RUN commands that set a mode in a shape this file cannot read.

    A guard that only checks the spellings it happens to recognise is a list, not
    an invariant: `sh -c 'chmod -R 0755 ...'`, `find ... -exec chmod 0777 ... {} +`,
    `xargs chmod`, and a heredoc RUN body all widen the export tree while every
    assertion above stays quiet. So an instruction that names `chmod` but is not a
    plain `chmod` simple command is refused instead of skipped - fail closed, and
    say which instruction it was.
    """
    offenders: list[str] = []
    for command in _run_commands(text):
        if "<<" in command:             # a heredoc body is not in the collected text
            offenders.append(f"a RUN opens a heredoc, whose body this file never reads: {command}")
            continue
        for words in _simple_commands(command):
            while words and words[0].startswith("--"):
                words = words[1:]
            if not words:
                continue
            if words[0].rsplit("/", 1)[-1] == "chmod":
                continue
            if "chmod" in " ".join(words):
                offenders.append(
                    "a RUN changes a mode in a shape this file cannot read, so it cannot "
                    f"be shown to leave the pinned paths alone: {' '.join(words)}")
    return offenders


def _canonical_mode(token: str) -> str | None:
    """`0755`, `755`, `00755` -> `0755`; a symbolic mode (`u+rwx`) -> None."""
    if not token or not set(token) <= set("01234567"):
        return None
    return f"0{int(token, 8):o}"


@dataclass(frozen=True)
class _ModeChange:
    """One instruction that sets a mode, with that mode bound to the paths it names."""

    command: str
    mode: str | None
    paths: tuple[str, ...]
    recursive: bool


def _mode_changes(text: str) -> list[_ModeChange]:
    """Every chmod (and `install -d -m`) in the Dockerfile, parsed rather than grepped."""
    changes: list[_ModeChange] = []
    for command in _run_commands(text):
        for words in _simple_commands(command):
            while words and words[0].startswith("--"):
                # `RUN --mount=... chmod ...`: RUN options are not part of the shell
                # command, so the head word of the instruction is behind them.
                words = words[1:]
            if not words:
                continue
            if words[0].rsplit("/", 1)[-1] == "chmod":   # `/bin/chmod` sets a mode too
                flags = [word for word in words[1:] if word.startswith("-")]
                operands = [word for word in words[1:] if not word.startswith("-")]
                if len(operands) < 2:       # a mode with no path sets nothing
                    continue
                changes.append(_ModeChange(
                    "chmod", _canonical_mode(operands[0]), tuple(operands[1:]),
                    _is_recursive(flags)))
            elif words[0] == "install":
                mode = None
                paths: list[str] = []
                index = 1
                while index < len(words):
                    word = words[index]
                    if word == "-m" and index + 1 < len(words):
                        mode = _canonical_mode(words[index + 1])
                        index += 2
                        continue
                    if not word.startswith("-"):
                        paths.append(word)
                    index += 1
                if "-d" in words and mode is not None:
                    changes.append(_ModeChange("install", mode, tuple(paths), False))
    return changes


#: Paths the image must bake with a mode, and the mode each one needs. The three
#: directories have to be traversable by uid 1000 (0644 cannot be entered); the two
#: `touch`ed markers and the copied client have to be readable, and no wider.
REQUIRED_MODES = {
    EXPORT: "0755",
    f"{EXPORT}/docker": "0755",
    f"{EXPORT}/docker/godot": "0755",
    f"{EXPORT}/docker/__init__.py": "0644",
    f"{EXPORT}/docker/godot/__init__.py": "0644",
}


def _covers(change_path: str, required: str) -> bool:
    """True when a chmod of `change_path` (recursively) also lands on `required`."""
    root = change_path.rstrip("/")
    return required == root or required.startswith(root + "/")


def test_the_import_tree_is_searchable_and_not_only_the_file():
    """A pinned FILE inside an untraversable DIRECTORY is still unreadable.

    `COPY --chmod=0644 <file> /opt/.../godot/<file>` also applies 0644 to the
    parent directories BuildKit creates for it. Measured on an image built from
    82bb9718f7 (2026-11-06, image e177dafdb87f): /opt/aitelier-gate-imports,
    /opt/aitelier-gate-imports/docker and .../docker/godot all came out
    0644 root:root, so as uid 1000 `ls` answered "Permission denied" and
    `import docker.godot.godot_harness` was a ModuleNotFoundError - a worse
    failure than the 0600 file the neighbouring test fixed, while that test
    stayed green because the Dockerfile does say `--chmod=0644`.

    This file reads the Dockerfile because the alternative is finding out from
    a deployed gate. What it can assert is the normalisation step; the modes
    actually baked can only be measured inside a built image, and that
    measurement belongs to the deployment that builds it.

    The mode is bound to the paths the instruction NAMES, not searched for as a
    substring of the whole RUN (iss-61d0ff8b8e564546, from the non-author review
    of the change that added this test): a substring test let
    `chmod -R 0755 /opt/aitelier-gate-imports` satisfy it - which takes
    docker/godot/godot_harness.py to 0755 - let `chmod 0777` satisfy it, and let
    any later RUN re-mode the client while both this and the neighbouring test
    stayed green. So: recursive chmod is refused wherever it would reach a
    pinned path, a pinned path named with another mode is refused, a chmod that
    names anything else under the export root is refused (a `*.py` glob would
    re-mode the client without naming it), and no RUN may name godot_harness.py
    at all - its mode belongs solely to the `COPY --chmod=0644` line above.
    """
    text = (ROOT / "Dockerfile").read_text()
    changes = _mode_changes(text)
    problems: list[str] = []

    for path, wanted in REQUIRED_MODES.items():
        naming = [change for change in changes if path in change.paths]
        if not any(change.mode == wanted and not change.recursive for change in naming):
            seen = "; ".join(
                f"{change.command}{' -R' if change.recursive else ''} "
                f"{change.mode or 'symbolic'} -> {' '.join(change.paths)}"
                for change in naming) or "no chmod or install names it at all"
            problems.append(
                f"{path} is not set to {wanted} by an instruction that names it "
                f"({seen}). A directory at 0644 cannot be traversed, so the gate "
                "client inside it is unreadable even though the file itself is 0644.")

    for change in changes:
        for path in REQUIRED_MODES:
            if change.recursive and any(_covers(named, path) for named in change.paths):
                problems.append(
                    f"`{change.command} {' '.join(change.paths)}` is recursive and reaches {path}: "
                    "it would re-mode that path (and, over the export root, take "
                    "docker/godot/godot_harness.py to the directory mode) while naming no file.")
            elif not change.recursive and path in change.paths and change.mode != REQUIRED_MODES[path]:
                problems.append(
                    f"`{change.command} {' '.join(change.paths)}` sets {path} to "
                    f"{change.mode or 'a symbolic mode'} instead of {REQUIRED_MODES[path]}.")
        for named in change.paths:
            if _covers(EXPORT, named):
                if named not in REQUIRED_MODES:
                    problems.append(
                        f"`{change.command}` names {named} under {EXPORT}, which is not one of the "
                        "pinned paths: a parent directory or a glob there can change the client's "
                        "mode without naming it. Pin the five paths themselves.")

    problems.extend(_unparsed_mode_instructions(text))

    for command in _run_commands(text):
        if "godot_harness.py" in command:
            problems.append(
                "a RUN instruction names godot_harness.py, whose mode belongs to the "
                f"`COPY --chmod=0644` line: {command}")

    assert not problems, "\n\n".join(problems)
