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

import fnmatch
import posixpath
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


def _directives(text: str) -> list[tuple[str, str]]:
    """Every instruction in file order, as (KEYWORD, arguments) with continuations joined.

    Blank lines, comments and bare keywords are dropped. The keyword is kept because
    two readers need different ones: the RUN parser wants the commands, the COPY check
    wants the destinations AND the WORKDIR of the stage each one appears in.
    """
    directives: list[tuple[str, str]] = []
    current: tuple[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            head, _, rest = stripped.partition(" ")
            if not rest or head.startswith("#"):
                continue
            current = (head.upper(), rest)
        else:
            current = (current[0], current[1] + " " + stripped)
        keyword, args = current
        if args.endswith("\\"):
            current = (keyword, args[:-1].rstrip() + " ")
            continue
        directives.append((keyword, " ".join(args.split())))
        current = None
    if current is not None:
        directives.append((current[0], " ".join(current[1].split())))
    return directives


def _instructions(text: str, keyword: str) -> list[str]:
    """Every `keyword` instruction's arguments, its `\\` continuations joined."""
    return [args for head, args in _directives(text) if head == keyword]


def _run_commands(text: str) -> list[str]:
    """Every RUN command, with its `\\` continuations joined into one line."""
    return _instructions(text, "RUN")


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

    The rule is deliberately not limited to commands that name the export tree, and
    it applies in every stage: this file cannot tell which of those wrappers reaches
    a pinned path, so a chmod it cannot parse anywhere is a question for the author,
    while one it waves through is a rebuild spent finding out. Neither the working
    directory nor the stage is tracked, and a `chmod` in a later builder stage is
    still cheaper to re-read here than a deployed gate is to debug.
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
                if len(operands) < 2:
                    # ...unless it names the export tree and we cannot read its mode:
                    # `chmod -x DIR` (a leading-dash symbolic mode) makes a directory
                    # untraversable, and `chmod --reference=OTHER DIR` takes a mode we
                    # never see. Both reach the pinned paths, so both are refused - and
                    # so is a glob, a variable or a relative word that could become one
                    # of them (`chmod -R 0755 /opt/*` names no pinned path at all).
                    named = tuple(_normalise_path(word) for word in words[1:]
                                  if _touches_export(word))
                    if named:
                        changes.append(_ModeChange(
                            "chmod", None, named, _is_recursive(flags)))
                    continue
                changes.append(_ModeChange(
                    "chmod", _canonical_mode(operands[0]),
                    tuple(_normalise_path(word) for word in operands[1:]),
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
                        paths.append(_normalise_path(word))
                    index += 1
                if "-d" in words and mode is not None:
                    changes.append(_ModeChange("install", mode, tuple(paths), False))
                elif any(_touches_export(named) for named in paths):
                    # `install -dm0777 DIR` / `--mode=` spellings this parser does not
                    # read, on a path inside the export tree: refused, not skipped.
                    changes.append(_ModeChange("install", None, tuple(paths), False))
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
    """True when a chmod of `change_path` (recursively) also lands on `required`.

    Both sides are normalised absolute paths. A word the shell would expand or
    resolve against the RUN's working directory never reaches here: `_reaches_pinned`
    refuses it instead of guessing what it becomes.
    """
    root = change_path.rstrip("/")
    return required == root or required.startswith(root + "/")


def _normalise_path(word: str) -> str:
    """The path an operand names, with `//`, `/./` and `..` collapsed.

    The shell collapses those, so a raw-text comparison does not: measured on the
    second non-author review, `chmod -R 0755 /opt//aitelier-gate-imports` and
    `chmod -R 0755 /opt/aitelier-gate-imports/docker/../..` both left this file green
    while the shell re-moded the pinned tree. A word the shell would EXPAND (a
    variable, a command substitution, a glob) or resolve elsewhere (a relative path)
    keeps its spelling: it is not a path this file can place, and `_reaches_pinned`
    refuses it rather than guess.
    """
    if word.startswith("/") and not any(mark in word for mark in "*?[$`"):
        return posixpath.normpath(word)
    return word


def _paths_above(path: str) -> tuple[str, ...]:
    """Every directory from the root down to `path`, `path` included."""
    parts = path.strip("/").split("/")
    return tuple("/" + "/".join(parts[:index]) for index in range(1, len(parts) + 1))


#: Every pinned path and every directory above it. A glob is matched against all of
#: them, not only against the pinned paths: `fnmatch`'s `*` crosses `/` today, but a
#: pattern is refused because it COULD cover the tree, not because of one library's
#: wildcard semantics.
GUARDED_PATHS: tuple[str, ...] = tuple(dict.fromkeys(
    prefix
    for path in (*REQUIRED_MODES, TARGET)
    for prefix in _paths_above(path)
))


def _reaches_pinned(word: str) -> bool:
    """True when an operand cannot be shown to leave the pinned paths alone.

    Three spellings are unplaceable, and each is refused the way it could escape
    rather than skipped:
      * a glob (`/opt/*`, `/opt/aitelier-gate-imports*`, `godot?`, `[a]itelier-...`)
        is matched with `fnmatch` against every pinned path and every directory above
        one - `/opt/*` names no pinned path at all and still re-modes all of them;
      * a variable or a command substitution (`$EXPORT_ROOT`, `` `pwd` ``) is refused
        whatever it expands to, because this file never runs the shell;
      * a relative word (`cd /opt && chmod -R 0755 aitelier-gate-imports`) is refused
        because the RUN's working directory is not tracked here.
    """
    if "$" in word or "`" in word:
        return True
    if not word.startswith("/"):
        return True
    normalised = posixpath.normpath(word)
    if any(mark in word for mark in "*?["):
        # The OPERAND is the pattern and the pinned path is the name it may cover.
        # Getting those two the wrong way round is not a crash: it answers False for
        # every wildcard operand and leaves `/opt/*` green, which is why the mutants
        # below are run rather than reasoned about.
        return any(fnmatch.fnmatchcase(guarded, normalised)
                   for guarded in GUARDED_PATHS)
    return any(_covers(normalised, guarded) for guarded in GUARDED_PATHS)


def _touches_export(named: str) -> bool:
    """True when this operand names the export tree, something inside it, or a word
    that can reach it (an ancestor, a glob, a variable, a relative path)."""
    return _covers(EXPORT, named) or _reaches_pinned(named)


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

    Every operand is compared as `posixpath.normpath` would place it, and a word that
    is not a plain absolute path is refused instead of read literally: the second
    non-author review measured `chmod -R 0755 /opt/*` (no pinned path named at all),
    `/opt//aitelier-gate-imports`, `/opt/aitelier-gate-imports*`, a `?` in a middle
    component, `$EXPORT_ROOT` and `cd /opt && chmod -R 0755 aitelier-gate-imports`
    all leaving this file green. `_reaches_pinned` says how each is refused.
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
        if change.mode is None and any(_touches_export(named) for named in change.paths):
            problems.append(
                f"`{change.command} {' '.join(change.paths)}` names the export tree but its mode "
                "cannot be read here (a leading-dash symbolic mode, `--reference=`, or an "
                "`install` spelling this file does not parse): refuse it rather than let it "
                "set an unmeasured mode on a pinned path.")
        for named in change.paths:
            if named in REQUIRED_MODES:
                continue
            if _touches_export(named):
                problems.append(
                    f"`{change.command}` names {named}, which is not one of the pinned paths "
                    "but can still reach one (a parent directory, a glob, a variable, or a "
                    "relative word): a chmod of anything under the export root - `*.py`, "
                    "`docker/*`, `/opt/*` - can change the client's mode without naming it. "
                    f"Pin the paths themselves: {', '.join(REQUIRED_MODES)}.")

    problems.extend(_unparsed_mode_instructions(text))

    for command in _run_commands(text):
        if "godot_harness.py" in command:
            problems.append(
                "a RUN instruction names godot_harness.py, whose mode belongs to the "
                f"`COPY --chmod=0644` line: {command}")

    assert not problems, "\n\n".join(problems)


def _copy_destination(instruction: str) -> tuple[str, str | None]:
    """The destination word of one COPY/ADD instruction, and its `--chmod` value."""
    try:
        words = shlex.split(instruction, comments=False, posix=True)
    except ValueError:
        words = instruction.split()
    mode: str | None = None
    operands: list[str] = []
    for word in words:
        if word.startswith("--"):
            if word.startswith("--chmod="):
                mode = word[len("--chmod="):]
            continue
        operands.append(word)
    return (operands[-1] if operands else ""), mode


@dataclass(frozen=True)
class _Copy:
    """One COPY/ADD, with its destination placed against its stage's WORKDIR."""

    instruction: str
    destination: str
    mode: str | None
    unreadable: str | None


def _copies(text: str) -> list[_Copy]:
    """Every COPY/ADD in the Dockerfile, with the path it actually writes to.

    A COPY destination is resolved against the WORKDIR of the stage it appears in -
    `COPY web/ ./` is the normal spelling here, not an escape - so a relative
    destination is placed, not refused. One that appears in a stage which has not
    declared a WORKDIR cannot be placed at all (the base image's is unknown), and a
    JSON-form instruction's destination is not read by this file: both are refused.
    """
    copies: list[_Copy] = []
    workdir: str | None = None
    for keyword, args in _directives(text):
        if keyword == "FROM":
            workdir = None
            continue
        if keyword == "WORKDIR":
            words = args.split()
            if not words:
                workdir = None
            elif words[0].startswith("/"):
                workdir = posixpath.normpath(words[0])
            elif workdir is not None:
                workdir = posixpath.normpath(posixpath.join(workdir, words[0]))
            continue
        if keyword not in ("COPY", "ADD"):
            continue
        instruction = f"{keyword} {args}"
        if args.startswith("["):
            copies.append(_Copy(instruction, "", None,
                                "a JSON-form destination this file does not read"))
            continue
        destination, mode = _copy_destination(args)
        if not destination:
            copies.append(_Copy(instruction, "", mode, "no destination word"))
        elif destination.startswith("/"):
            copies.append(_Copy(instruction, posixpath.normpath(destination), mode, None))
        elif workdir is None:
            copies.append(_Copy(instruction, destination, mode,
                                f"`{destination}` is relative and its stage declares no "
                                "WORKDIR, so where it lands is unknown"))
        else:
            copies.append(_Copy(
                instruction, posixpath.normpath(posixpath.join(workdir, destination)),
                mode, None))
    return copies


def test_nothing_but_the_pinned_copy_writes_into_the_export_tree():
    """A directory DESTINATION takes the pinned directories with it.

    `COPY --chmod=0755 docker/godot/ /opt/aitelier-gate-imports/docker/godot/` names
    no file, so the pin above - which looks for the pinned FILE's path in the
    instruction text - never sees it, while BuildKit applies that mode to the
    destination directory and to everything it creates under it: the same failure the
    neighbouring test exists for, reached from the other side. The same instruction
    with `--chmod=0777` on the pinned file passes that check too, because
    `0[0-7]{3}` accepts it.

    That regex is left exactly as it was (its own issue tracks a stricter spelling).
    This assertion reads the DESTINATION instead, and requires that the only thing
    ever copied anywhere into the export tree is the one pinned line, at the pinned
    target, with exactly 0644 - and refuses a destination it cannot place.
    """
    text = (ROOT / "Dockerfile").read_text()
    problems: list[str] = []
    for copy in _copies(text):
        if copy.unreadable:
            problems.append(
                f"`{copy.instruction}` cannot be shown to stay out of {EXPORT}: "
                f"{copy.unreadable}. Write the destination as an absolute path this "
                "file can compare, or say here why it cannot be.")
            continue
        if not _touches_export(copy.destination):
            continue
        if copy.destination != TARGET or _canonical_mode(copy.mode or "") != "0644":
            problems.append(
                f"`{copy.instruction}` writes into {copy.destination} with mode "
                f"{copy.mode or 'inherited from the build host'}. The only instruction "
                f"allowed to write into {EXPORT} is `COPY --chmod=0644 "
                f"docker/godot/godot_harness.py {TARGET}`: a directory destination "
                "re-modes the pinned directories, and every file under them, to "
                "whatever that COPY carries.")
    assert not problems, "\n\n".join(problems)
