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

What this file reads, and what it cannot
----------------------------------------
It reads the Dockerfile TEXT, and binds a mode to the path an instruction NAMES. That
closes the shapes it has met - four non-author reviews have each found spellings it
missed, and each round's own review is quoted where its rule lives - but it is a
review-time alarm, not a proof about the built artifact. It cannot see:

  * what a program a RUN invokes does with its arguments (`python -c`, a script in the
    build context, a Makefile). Only a command WORD this file reads is judged, and a
    command it does not read that names the tree is refused rather than followed;
  * a symlink, a bind mount or a binary that arrives from the build context, from
    `COPY --from` another stage, or from the base image;
  * what the KERNEL resolves an operand to: `/proc/self/root/opt/...` is the export
    tree through an alias path and re-modes it (fourth review, E3), so a recursive
    chmod whose operand is not the tree itself is refused, and so is `ln`/`mount`;
  * the EFFECTIVE modes in an image. Those are measurable only inside a built image:
    `evidence/gate-import-dir-modes-20261106/` is the probe that does it, and the
    deployment that builds the image owns that measurement.

Every spelling this file has met so far is either read or refused outright, and a
refusal names the shape it removes. The next one it has not met is why the built-image
probe exists.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
import shutil
import tempfile
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


#: The long options GNU chmod accepts. A long flag is resolved against this list the way
#: getopt_long resolves it: by UNAMBIGUOUS PREFIX. Anything else - an abbreviation shared
#: by two options, or a name not in the list - is read as recursive, the direction that
#: fails closed. `chmod --recurs 0755 /opt/aitelier-gate-imports` re-modes the whole tree
#: (godot_harness.py included) while the literal `--recursive` test read it as a
#: non-recursive chmod of the root to exactly the root's required mode: measured green on
#: the fourth non-author review (E7).
_CHMOD_LONG_OPTIONS = ("recursive", "reference", "changes", "silent", "quiet", "verbose",
                       "preserve-root", "no-preserve-root", "help", "version")


def _is_recursive(flags: list[str]) -> bool:
    """True for `-R`, `--recursive`, an abbreviation of it, and every short cluster with R.

    `chmod -Rf 0755 <dir>` is as recursive as `chmod -R 0755 <dir>`: GNU chmod reads
    a short-flag cluster letter by letter. Matching the literal word `-R` read the
    combined spelling as NON-recursive, which left `chmod -Rf 0755
    /opt/aitelier-gate-imports` green while it took godot_harness.py to 0755 (found
    by the non-author review of the first version of this test, and reproduced).
    `-r` is kept as recursive even though GNU reads it as "remove read": the guard
    fails closed where it is unsure - as it does for `--r`, which getopt_long reads as an
    ambiguity between `--recursive` and `--reference` and refuses outright.
    """
    for flag in flags:
        if flag.startswith("--"):
            name = flag[2:].split("=", 1)[0]
            matching = [option for option in _CHMOD_LONG_OPTIONS if option.startswith(name)]
            if matching == ["recursive"] or len(matching) != 1:
                return True
            continue
        if flag == "-r":
            return True
        if flag.startswith("-") and "R" in flag[1:]:
            return True
    return False


def _command_words(words: list[str]) -> list[str]:
    """One simple command's words, with the wrappers that only RUN it taken off.

    `RUN --mount=type=cache,target=/x chmod ...` puts the RUN options in front of the
    command, and `env install -d -m 0700 <dir>` / `env FOO=1 chmod ...` run the command
    that follows the `env`. The third non-author review measured
    `RUN /usr/bin/install -d -m 0700 /opt/aitelier-gate-imports/docker/godot` (the head
    word was compared with `== "install"`, exactly) and `env install ...` both leaving
    this file green while `install -d -m 0700` took a pinned directory to 0700 - a
    directory uid 1000 cannot enter, the original bug.
    """
    while words and words[0].startswith("--"):
        words = words[1:]
    while words and words[0].rsplit("/", 1)[-1] == "env":
        words = words[1:]
        while words and (words[0].startswith("-") or "=" in words[0]):
            words = words[1:]
    return words


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
    while one it waves through is a rebuild spent finding out. What is NOT tracked
    here is the RUN's own working directory (a relative operand is refused by
    `_reaches_pinned` rather than resolved) and the stage a RUN belongs to - `_copies`
    does track WORKDIR, but only for COPY destinations. A `chmod` in a later builder
    stage is still cheaper to re-read here than a deployed gate is to debug.
    """
    offenders: list[str] = []
    for command in _run_commands(text):
        if "<<" in command:             # a heredoc body is not in the collected text
            offenders.append(f"a RUN opens a heredoc, whose body this file never reads: {command}")
            continue
        for words in _simple_commands(command):
            words = _command_words(words)
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


def _mode_changes(text: str) -> tuple[list[_ModeChange], list[str]]:
    """Every chmod and `install` mode change in the Dockerfile, parsed rather than grepped.

    Returns the changes this file could PLACE, and one line per instruction whose mode
    change it could not place at all (an `install` flag it does not read, a `-m`/`-t`
    with no value). The second list is a refusal, not a warning: a mode change whose
    paths are unknown is exactly what must not be waved through.
    """
    changes: list[_ModeChange] = []
    unplaceable: list[str] = []
    for command in _run_commands(text):
        for words in _simple_commands(command):
            # `RUN --mount=...` and `env X ...` put the command behind their own words,
            # and the command is read by its LAST path component so `/usr/bin/install`
            # and `/bin/chmod` are read as the commands they are.
            words = _command_words(words)
            if not words:
                continue
            head = words[0].rsplit("/", 1)[-1]
            if head == "chmod":                     # `/bin/chmod` sets a mode too
                flags = [word for word in words[1:] if word.startswith("-")]
                operands = [word for word in words[1:] if not word.startswith("-")]
                if len(operands) < 2:
                    # ...unless it names the export tree and we cannot read its mode:
                    # `chmod -x DIR` (a leading-dash symbolic mode) makes a directory
                    # untraversable, and `chmod --reference=OTHER DIR` takes a mode we
                    # never see. Both reach the pinned paths, so both are refused - and
                    # so is a glob, a variable or a relative word that could become one
                    # of them (`chmod -R 0755 /opt/*` names no pinned path at all).
                    # Only the OPERANDS are read here: the FLAG word `-x` is not a path,
                    # and counting it as a relative one refused `chmod -x
                    # /usr/local/bin/tool`, which touches nothing pinned (F1 of the
                    # third non-author review).
                    named = tuple(_normalise_path(word) for word in operands
                                  if _touches_export(word))
                    if named:
                        changes.append(_ModeChange(
                            "chmod", None, named, _is_recursive(flags)))
                    continue
                changes.append(_ModeChange(
                    "chmod", _canonical_mode(operands[0]),
                    tuple(_normalise_path(word) for word in operands[1:]),
                    _is_recursive(flags)))
            elif head == "install":
                mode: str | None = None
                target: str | None = None
                directory = False
                operands: list[str] = []
                unreadable: str | None = None
                index = 1
                while index < len(words):
                    word = words[index]
                    if word in ("-m", "--mode"):
                        if index + 1 >= len(words):
                            unreadable = f"`{word}` names no mode"
                            break
                        mode = _canonical_mode(words[index + 1])
                        index += 2
                        continue
                    if word in ("-t", "--target-directory"):
                        if index + 1 >= len(words):
                            unreadable = f"`{word}` names no directory"
                            break
                        target = _normalise_path(words[index + 1])
                        index += 2
                        continue
                    if word.startswith("--mode="):
                        mode = _canonical_mode(word[len("--mode="):])
                    elif word.startswith("--target-directory="):
                        target = _normalise_path(word[len("--target-directory="):])
                    elif word in ("-d", "--directory"):
                        directory = True
                    elif word.startswith("-m") and len(word) > 2:
                        mode = _canonical_mode(word[2:])            # `-m0700`
                    elif word.startswith("-"):
                        unreadable = f"`{word}` is an install flag this file does not read"
                        break
                    else:
                        operands.append(_normalise_path(word))
                    index += 1
                if unreadable is not None:
                    unplaceable.append(
                        "an `install` invocation cannot be placed "
                        f"({unreadable}), so which paths it sets a mode on is unknown: "
                        f"{' '.join(words)}")
                    continue
                if directory:
                    # every operand of `install -d` is a directory created with `mode`
                    if target is not None:
                        unplaceable.append(
                            "`install -d -t DIR` names its directories with a flag this "
                            f"file does not model: {' '.join(words)}")
                        continue
                    changes.append(_ModeChange("install", mode, tuple(operands), False))
                elif target is not None:
                    # `install -t DIR src…` copies each SOURCE INTO `DIR` under its own
                    # name, so the path that takes the mode is `DIR/<name>` and not the
                    # source. Reading only the LAST operand as "the target" (true when
                    # there is no `-t`) let `install -m 0700 -t
                    # /opt/aitelier-gate-imports/docker /tmp/__init__.py` through: it
                    # re-modes the pinned 0644 marker, measured green on the fourth
                    # non-author review (E2).
                    landed = tuple(
                        posixpath.join(target, posixpath.basename(source.rstrip("/")))
                        for source in operands) or (target,)
                    changes.append(_ModeChange("install", mode, landed, False))
                else:
                    # Without `-t` only the LAST operand is a target: the earlier ones
                    # are SOURCES, read rather than set. Treating a source as a target
                    # refused `install -m 0755 scripts/tool /usr/local/bin/tool` - a
                    # relative source word, no pinned path named (F2 of the third
                    # review). With the destination named and the mode unreadable, the
                    # change is still refused below.
                    landed_target = tuple(operands[-1:])
                    if any(_touches_export(named) for named in landed_target):
                        changes.append(_ModeChange("install", mode, landed_target, False))
    return changes, unplaceable


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


def _has_parent_component(word: str) -> bool:
    """True when this word carries a `..` PATH COMPONENT (`a..b` is a name, not one)."""
    return ".." in word.split("/")


def _unplaceable(word: str) -> str | None:
    """Why this word cannot be placed as a path, or None when it can be read as one.

    Two spellings left this file green on the third non-author review while the build
    re-moded a pinned path, because both were COLLAPSED by text before anything
    compared them:

      * `ARG GODOT_DIR=/opt/aitelier-gate-imports/docker/godot` plus `COPY
        --chmod=0600 docker/godot/godot_harness.py ${GODOT_DIR}/../godot/`:
        `posixpath.normpath` dropped `${GODOT_DIR}` together with the `..` and left
        `/app/godot` (the stage's WORKDIR), so no destination named the export tree at
        all - while BuildKit expanded the variable and baked the client 0600, the
        original bug. The same two lines as `WORKDIR ${GODOT_DIR}/../godot` then
        `COPY ... ./` escaped the same way.
      * `chmod -R 0700 /var/run/../opt/aitelier-gate-imports`: the text collapsed to
        `/var/opt/...`, but `/var/run` is a symlink to `/run` and the KERNEL resolves
        `..` after following it, so the operand the build re-moded was `/opt/...`.

    The shell expands a variable and passes `..` through unchanged, so a word carrying
    either is refused instead of normalised. `RUN chmod -R 0755 /opt/aitelier-
    gate-imports/docker/../..` is refused by the same rule; that is a knowingly
    accepted false refusal, and it is the direction this file already refuses an
    unreadable chmod shape in.
    """
    if "$" in word or "`" in word:
        return f"`{word}` carries a variable or a command substitution"
    if _has_parent_component(word):
        return (f"`{word}` carries a `..` component, which the kernel resolves after "
                "following any symlink in the path")
    return None


def _normalise_path(word: str) -> str:
    """The path an operand names, with `//` and `/./` collapsed and nothing else.

    A raw-text comparison does not see those: measured on the second non-author review,
    `chmod -R 0755 /opt//aitelier-gate-imports` left this file green while the kernel
    re-moded the pinned tree.

    A `..` component is deliberately NOT collapsed. The shell passes `..` through
    unchanged and the kernel resolves it AFTER following every symlink in the prefix,
    so `/var/run/../opt/...` names `/opt/...` on this image (`/var/run` is a symlink to
    `/run`) and `/var/opt/...` only as text - the third non-author review measured
    `chmod -R 0700 /var/run/../opt/aitelier-gate-imports` green through exactly that
    textual collapse. Such a word keeps its spelling, and `_unplaceable` /
    `_reaches_pinned` refuse it. A word the shell would EXPAND (a variable, a command
    substitution, a glob) or resolve elsewhere (a relative path) keeps its spelling for
    the same reason: it is not a path this file can place.
    """
    if (word.startswith("/") and not any(mark in word for mark in "*?[$`")
            and not _has_parent_component(word)):
        return posixpath.normpath(word)
    return word


def _paths_above(path: str) -> tuple[str, ...]:
    """Every directory from the root down to `path`, `path` included.

    The root itself is included, because `RUN chmod 0700 /*/..` collapses to `/` and a
    chmod of the root re-modes every pinned path - while `/` is nobody's prefix, so a
    list that started at the first component matched nothing at all and that mutant
    (N7) stayed green.
    """
    parts = path.strip("/").split("/")
    return ("/",) + tuple("/" + "/".join(parts[:index])
                          for index in range(1, len(parts) + 1))


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
    r"""True when an operand cannot be shown to leave the pinned paths alone.

    Every unplaceable spelling is refused the way it could escape rather than skipped:
      * a variable or a command substitution (`$EXPORT_ROOT`, `` `pwd` ``) is refused
        whatever it expands to, because this file never runs the shell;
      * a `..` component is refused although `posixpath.normpath` can collapse it: the
        kernel resolves it after symlinks, so `/var/run/../opt/...` is `/opt/...`
        (N3, measured green through the textual collapse);
      * a BRACKET expression is refused outright instead of being handed to `fnmatch`,
        which reads `[[:alpha:]]` and `[^x]` as literals - `[\[:alpha:]\]` matches
        nothing - while dash/bash expand both to the export root's name, so
        `chmod -R 0700 /opt/aitelier-gate-import[[:alpha:]]` and `...import[^x]` re-moded
        the pinned tree while this file stayed green (N2, N2b);
      * the root itself is refused: `chmod 0700 /*/..` collapses to `/` (N7);
      * a glob (`/opt/*`, `/opt/aitelier-gate-imports*`, `godot?`) is matched with
        `fnmatch` against every pinned path and every directory above one - `/opt/*`
        names no pinned path at all and still re-modes all of them;
      * a relative word (`cd /opt && chmod -R 0755 aitelier-gate-imports`) is refused
        because the RUN's working directory is not tracked here;
      * a BRACE (`/opt/aitelier-gate-{imports,x}`) is refused because which name the
        shell expands it to is not text: under `/bin/sh` it is a literal path, under
        bash it is two, and `SHELL ["/bin/bash","-c"]` is refused for the same reason
        (E4 of the fourth non-author review).
    """
    if "$" in word or "`" in word:
        return True
    if _has_parent_component(word):
        return True
    if "{" in word or "}" in word:
        return True
    if not word.startswith("/"):
        return True
    normalised = posixpath.normpath(word)
    if normalised == "/":
        return True
    if "[" in word:
        return True
    if any(mark in word for mark in "*?"):
        # The OPERAND is the pattern and the pinned path is the name it may cover.
        # Getting those two the wrong way round is not a crash: it answers False for
        # every wildcard operand and leaves `/opt/*` green, which is why the mutants
        # below are run rather than reasoned about.
        return any(fnmatch.fnmatchcase(guarded, normalised)
                   for guarded in GUARDED_PATHS)
    return any(_covers(normalised, guarded) for guarded in GUARDED_PATHS)


def _touches_export(named: str) -> bool:
    """True when this operand names the export tree, something inside it, or a word
    that can reach it (an ancestor, a glob, a variable, a `..` component, a relative
    path)."""
    return _covers(EXPORT, named) or _reaches_pinned(named)


def _names_export_outright(word: str) -> bool:
    """True when this word names or reaches the export tree and is NOT a relative word.

    A relative word is left out of this reading on purpose. `_touches_export` counts
    "relative" as "could reach the tree", which is right when the word is an operand of
    a chmod - but `chmod -x /usr/local/bin/tool`'s FLAG word `-x` went through the same
    test and refused a build that touches nothing pinned (F1 of the third non-author
    review). A word carrying a variable stays IN, because
    `cp -p docker/godot/godot_harness.py $EXPORT/docker/godot/` does reach the tree and
    this file cannot read where `$EXPORT` lands.
    """
    if EXPORT in word:
        # Named INSIDE a token the shell hands to another program:
        # `python -c "os.chmod('/opt/aitelier-gate-imports', 0o700)"` names the tree
        # without ever being an operand of a chmod, so the word is not an absolute path
        # and the reading below would miss it.
        return True
    if not word.startswith("/"):
        return "$" in word or "`" in word
    return _touches_export(word)


#: Commands that cannot change the MODE of a path that already exists. `touch` and
#: `mkdir` only create one - and the two marker files the shipped build creates are
#: REQUIRED_MODES entries whose modes the same RUN pins with `chmod 0644`, so removing
#: that chmod fails the pinned-mode assertion above. `chown`/`chgrp` move ownership, not
#: the mode bits uid 1000 needs in order to read the tree. Everything else that names
#: the export tree - `ln`, `cp -p`, `mv`, `rsync`, `tar`, `dd` - can carry a mode in.
_MODE_PRESERVING_COMMANDS = ("touch", "mkdir", "chown", "chgrp")


#: Commands that create an ALIAS of a path, which is what makes a plain `chmod` of an
#: unrelated absolute path reach a pinned one: chmod follows a symlink named on its
#: command line, and a bind mount makes two paths one inode. The shipped build creates
#: neither, so refusing them costs nothing - and this file cannot resolve a link, so
#: modelling one would be guessing.
_ALIAS_COMMANDS = ("ln", "mount", "bindfs")


def _mode_carriers_this_file_does_not_read(text: str) -> list[str]:
    """Mode changes carried by an instruction or a command this file does not parse.

    Three shapes left the tree re-moded while every assertion above stayed green (third
    and fourth non-author reviews):

      * a `SHELL` instruction - `SHELL ["/bin/sh","-c","chmod -R 0700
        /opt/aitelier-gate-imports; eval \\"$1\\"","sh"]` - installs the shell every
        LATER RUN is executed through, so the chmod above it never has to appear on a
        RUN line at all. Only RUN lines were read, so nothing saw it.
      * a symlink alias - `RUN ln -s /opt/aitelier-gate-imports/docker/godot /tmp/g &&
        chmod 0700 /tmp/g` - is a plain `chmod` of `/tmp/g`, which names no pinned path;
        chmod follows a symlink named on its command line, so the pinned directories
        were re-moded under an alias this file never resolves. `cp -p` (which carries
        the source's mode onto a pinned path) and a `mv` out and back are the same hole.
      * a SHELL that is not a plain sh - `SHELL ["/bin/bash","-c"]` plus `RUN chmod -R
        0700 /opt/aitelier-gate-{imports,x}` - changes the LANGUAGE of every later RUN
        (brace expansion), which is measured green on the fourth review (E4).

    So: only a RUN may carry a mode change, only a plain `chmod`/`install` simple command
    may name the export tree, and `ln`/`mount` is refused because it makes an alias.
    What a command this file cannot read WOULD do to a pinned path is exactly what must
    not be guessed at.

    A `SHELL` instruction is refused WHATEVER it says, in two words: this file reads RUN
    as POSIX-sh-shaped words, so a different shell is a different language (brace
    expansion, `source`, and a `chmod` glued to another command with `;` - E5 - are all
    of it), and the shipped build sets no SHELL at all. The message names which of the
    two it saw, so the failing case still says what it broke.
    """
    offenders: list[str] = []
    for keyword, args in _directives(text):
        if keyword in ("RUN", "COPY", "ADD"):
            # RUN is parsed by _mode_changes; `COPY --chmod=` is the pin itself, and a
            # COPY destination is read by _copies.
            continue
        if keyword == "SHELL":
            if re.search(r"(?<![\w.-])(chmod|install)(?![\w.-])", args):
                offenders.append(
                    f"the SHELL instruction can carry a mode change this file does not "
                    f"read (only a RUN is parsed), so it cannot be shown to leave the "
                    f"pinned paths alone: SHELL {args}")
            else:
                offenders.append(
                    "a SHELL instruction replaces the shell every later RUN is read "
                    "through, and this file reads RUN as POSIX-sh-shaped words (`{a,b}` "
                    f"and other shell-specific expansion is not text): SHELL {args}")
        heads = {word.rsplit("/", 1)[-1].strip("\"',[]")
                 for word in re.split(r"[\s\"'\[\],]+", args)}
        if heads & {"chmod", "install"}:
            offenders.append(
                f"the {keyword} instruction can carry a mode change this file does not "
                f"read (only a RUN is parsed), so it cannot be shown to leave the pinned "
                f"paths alone: {keyword} {args}")
    for command in _run_commands(text):
        if "<<" in command:
            continue          # a heredoc body is refused by _unparsed_mode_instructions
        for words in _simple_commands(command):
            words = _command_words(words)
            if not words:
                continue
            head = words[0].rsplit("/", 1)[-1]
            if head in ("chmod", "install") or head in _MODE_PRESERVING_COMMANDS:
                continue
            if head in _ALIAS_COMMANDS:
                offenders.append(
                    f"`{' '.join(words)}` creates a link or a mount, and a plain `chmod` of "
                    "the alias then re-modes a pinned path under a name this file never "
                    "resolves (`ln -s /opt/aitelier-gate-imports/docker/godot /tmp/g && "
                    "chmod 0700 /tmp/g`, measured green on the fourth non-author review, "
                    "E6). The shipped build creates no link, so the refusal costs nothing.")
                continue
            naming = [word for word in words[1:] if _names_export_outright(word)]
            if naming:
                offenders.append(
                    f"`{' '.join(words)}` is not a plain chmod or install, but it names "
                    f"{naming[0]}: a command this file does not parse can put the export "
                    "tree's mode under an alias (`ln -s` then `chmod`), carry a source mode "
                    "onto it (`cp -p`), move it out and back (`mv`), or re-mode it another "
                    "way - so it is refused rather than waved through.")
    return offenders


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

    The third non-author review measured nine more spellings green, every one of them
    a shape whose mode-change this file never read: a `..` the kernel resolves after a
    symlink, a bracket expression `fnmatch` reads as a literal (`_reaches_pinned`), an
    `install` behind its full path or behind `env` (`_command_words`), a `chmod` carried
    by a `SHELL` instruction or hidden behind a symlink alias
    (`_mode_carriers_this_file_does_not_read`), and a chmod of `/` (`_paths_above`).

    The FOURTH review measured six more, and this round changed the rules rather than
    adding cases to a list: `chmod --recurs` (a getopt_long abbreviation `_is_recursive`
    now resolves), `install -t DIR` (the target is `DIR/<name>`, not the last operand),
    a SHELL that is not a plain sh (`SHELL` is refused outright, which also refuses a
    `chmod` glued to another command with `;` and a brace-expanded operand), an alias
    path (`/proc/self/root/opt/...`: a recursive chmod of anything that is not the tree
    is refused), `ln -s` + chmod of the link (`ln`/`mount` is refused outright), and
    brace expansion (`{`/`}` is refused as text). Where a shape is refused rather than
    read, the refusal says so - that is the boundary this file's module docstring states,
    and the modes baked into an image remain measurable only inside a built image.
    """
    text = (ROOT / "Dockerfile").read_text()
    changes, unplaceable = _mode_changes(text)
    problems: list[str] = list(unplaceable)

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
                    f"`{change.command} {' '.join(change.paths)}` names {named}, which is "
                    "not one of the pinned paths but can still reach one (a parent "
                    "directory, a glob, a variable, a `..` component, a bracket "
                    "expression, a brace, or a relative word): a chmod of anything under "
                    "the export root - `*.py`, `docker/*`, `/opt/*` - can change the "
                    "client's mode without naming it. "
                    f"Pin the paths themselves: {', '.join(REQUIRED_MODES)}.")
            elif change.recursive:
                # A recursive chmod re-modes everything below its operand, and this file
                # cannot show an absolute operand is not an ALIAS of the tree: the fourth
                # non-author review measured `chmod -R 0700 /proc/self/root/opt/aitelier-
                # gate-imports` green, which re-modes the tree through its own root (E3),
                # and a `..`-through-symlink or a bind mount is the same hole. So an
                # operand that is not the export tree itself is refused rather than
                # modelled. The shipped build has no recursive chmod at all.
                problems.append(
                    f"`{change.command} {' '.join(change.paths)}` is recursive and names "
                    f"{named}, which is not the export tree: this file cannot show an "
                    "operand is not an alias of it (`/proc/self/root/opt/...` re-modes it "
                    "through the tree's own root, measured green on the fourth non-author "
                    "review), so pin each path with its own mode instead.")

    problems.extend(_unparsed_mode_instructions(text))
    problems.extend(_mode_carriers_this_file_does_not_read(text))

    for command in _run_commands(text):
        if "godot_harness.py" in command:
            problems.append(
                "a RUN instruction names godot_harness.py, whose mode belongs to the "
                f"`COPY --chmod=0644` line: {command}")

    assert not problems, "\n\n".join(problems)


def _split_words(text: str) -> list[str]:
    """An instruction's words, quoted the way BuildKit reads them.

    `WORKDIR "/opt/aitelier-gate-imports/docker/godot"` is that path to BuildKit. Reading
    the raw token kept the quote characters, so the word no longer started with `/` and a
    following `COPY --chmod=0600 ... ./` was placed against the PREVIOUS WORKDIR - the
    fourth non-author review's E1. Appended to the shipped file (whose pin is still there)
    that COPY lands ON the pinned client at 0600 in the built image while this file stayed
    green: the shape it named was real, and it was the placement that hid it.
    """
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return text.split()


def _copy_destination(instruction: str) -> tuple[str, str | None]:
    """The destination word of one COPY/ADD instruction, and its `--chmod` value."""
    words = _split_words(instruction)
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

    A destination, or the WORKDIR a relative destination is resolved against, that
    carries a variable or a `..` component is refused BEFORE either is collapsed - the
    third non-author review measured both spellings green (N1, N1b): a variable and a
    `..` cancelled each other out under `posixpath.normpath`, leaving `/app/godot`,
    while BuildKit expanded the variable and baked the pinned client 0600.
    """
    copies: list[_Copy] = []
    workdir: str | None = None
    workdir_reason: str | None = None
    for keyword, args in _directives(text):
        if keyword == "FROM":
            workdir, workdir_reason = None, None
            continue
        if keyword == "WORKDIR":
            words = _split_words(args)
            reason = _unplaceable(words[0]) if words else "its stage declares an empty WORKDIR"
            if reason is not None:
                # `WORKDIR ${GODOT_DIR}/../godot` + `COPY --chmod=0600 ... ./` (N1b):
                # the stage's working directory is not a path this file can place, so
                # neither is a destination resolved against it.
                workdir, workdir_reason = None, (
                    f"its stage's WORKDIR cannot be placed ({reason})")
            elif words[0].startswith("/"):
                workdir, workdir_reason = posixpath.normpath(words[0]), None
            elif workdir is not None:
                workdir, workdir_reason = (
                    posixpath.normpath(posixpath.join(workdir, words[0])), None)
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
            continue
        reason = _unplaceable(destination)
        if reason is not None:
            # N1: `COPY --chmod=0600 docker/godot/godot_harness.py ${GODOT_DIR}/../godot/`
            # collapsed to the stage's WORKDIR and named no pinned path at all.
            copies.append(_Copy(instruction, destination, mode, reason))
        elif destination.startswith("/"):
            copies.append(_Copy(instruction, posixpath.normpath(destination), mode, None))
        elif workdir is None:
            copies.append(_Copy(instruction, destination, mode,
                                workdir_reason or (
                                    f"`{destination}` is relative and its stage declares no "
                                    "WORKDIR, so where it lands is unknown")))
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


def _refusals_for(dockerfile: str) -> str:
    """Every assertion the four tests above raise when ROOT holds this Dockerfile.

    The regression cases below need a Dockerfile they can mutate, so this writes one
    into a private directory, points the module-level `ROOT` at it (the same global the
    mutant matrix sets by hand) and puts the real one back afterwards. Empty text means
    all four stayed green.
    """
    global ROOT
    original = ROOT
    root = Path(tempfile.mkdtemp(prefix="gate-guard-case-"))
    try:
        (root / "Dockerfile").write_text(dockerfile)
        ROOT = root
        refusals: list[str] = []
        for test in (test_the_baked_gate_client_pins_its_mode,
                     test_no_other_opt_copy_of_repository_source_is_left_unpinned,
                     test_the_import_tree_is_searchable_and_not_only_the_file,
                     test_nothing_but_the_pinned_copy_writes_into_the_export_tree):
            try:
                test()
            except AssertionError as exc:
                refusals.append(str(exc))
        return "\n\n".join(refusals)
    finally:
        ROOT = original
        shutil.rmtree(root, ignore_errors=True)


#: The nine shapes the third non-author review measured GREEN against bb2e5a53, with
#: the words each is now refused with. A shape that is refused for an UNRELATED reason
#: fails this case rather than passing it, which is what keeps the listed rule the rule
#: that is being tested - so each expectation names the OPERAND as well as the rule after
#: the fourth review pointed out that four cases sharing "can still reach one" could not
#: tell their rules apart (`expected in refusals` is a substring over pooled output).
THIRD_REVIEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("N1  COPY destination built from a variable plus `..`",
     "ARG GODOT_DIR=/opt/aitelier-gate-imports/docker/godot\n"
     "COPY --chmod=0600 docker/godot/godot_harness.py ${GODOT_DIR}/../godot/",
     "cannot be shown to stay out of"),
    ("N1b WORKDIR built from a variable plus `..`, then `COPY ... ./`",
     "ARG GODOT_DIR=/opt/aitelier-gate-imports/docker/godot\n"
     "WORKDIR ${GODOT_DIR}/../godot\n"
     "COPY --chmod=0600 docker/godot/godot_harness.py ./",
     "cannot be shown to stay out of"),
    ("N2  shell-only glob `[[:alpha:]]`",
     "RUN chmod -R 0700 /opt/aitelier-gate-import[[:alpha:]]",
     "names /opt/aitelier-gate-import[[:alpha:]], which is not one of the pinned paths"),
    ("N2b shell negation glob `[^x]`",
     "RUN chmod -R 0700 /opt/aitelier-gate-import[^x]",
     "names /opt/aitelier-gate-import[^x], which is not one of the pinned paths"),
    ("N3  `..` resolved through a symlink",
     "RUN chmod -R 0700 /var/run/../opt/aitelier-gate-imports",
     "names /var/run/../opt/aitelier-gate-imports, which is not one of the pinned paths"),
    ("N4  `install` reached by its full path",
     "RUN /usr/bin/install -d -m 0700 /opt/aitelier-gate-imports/docker/godot",
     "instead of 0755"),
    ("N5  a mode change carried by a SHELL instruction",
     'SHELL ["/bin/sh","-c",'
     '"chmod -R 0700 /opt/aitelier-gate-imports; eval \\"$1\\"","sh"]',
     "can carry a mode change this file does not read"),
    ("N6  a symlink alias, then a chmod of the alias",
     "RUN ln -s /opt/aitelier-gate-imports/docker/godot /tmp/g && chmod 0700 /tmp/g",
     "creates a link or a mount"),
    ("N7  a chmod of `/` spelled `/*/..`",
     "RUN chmod 0700 /*/..",
     "names /*/.., which is not one of the pinned paths"),
)


def test_every_shape_the_third_review_measured_green_is_now_refused():
    """The nine spellings that re-moded a pinned path while this file stayed green.

    Measured (not theorised) on bb2e5a53 with the mutant matrix: "mutants: 27,
    escapes: 9, shipped green: True". Each line below is one of those nine appended to
    the shipped Dockerfile, and each must now be refused - by the rule named in
    `THIRD_REVIEW_SHAPES`'s expectation, not merely by falling over somewhere.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in THIRD_REVIEW_SHAPES:
        refusals = _refusals_for(f"{shipped}\n{extra}\n")
        assert refusals, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert expected in refusals, (
            f"{name} was refused, but not by the rule that closes it (expected "
            f"{expected!r}):\n{refusals}")


#: The six shapes the FOURTH non-author review measured GREEN against 0cbdcd30, plus the
#: one it named that was not reproducible as stated. Each is again a one-line mutation of
#: the SHIPPED Dockerfile (the control), and the expectation names the rule (and, where a
#: shape could be refused by two rules, the operand) so the case cannot pass by accident.
FOURTH_REVIEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("E2  `install -t DIR` puts the target behind a flag",
     "RUN install -m 0700 -t /opt/aitelier-gate-imports/docker /tmp/__init__.py",
     "instead of 0644"),
    ("E5  a `chmod` glued inside a SHELL string with `;`",
     'SHELL ["/bin/sh","-c",'
     '"true;chmod -R 0700 /opt/aitelier-gate-imports;eval \\"$1\\"","sh"]',
     "can carry a mode change this file does not read"),
    ("E7  `--recurs`, the getopt_long abbreviation of `--recursive`",
     "RUN chmod --recurs 0755 /opt/aitelier-gate-imports",
     "is recursive and reaches /opt/aitelier-gate-imports"),
    ("E3  the export tree reached through `/proc/self/root`",
     "RUN chmod -R 0700 /proc/self/root/opt/aitelier-gate-imports",
     "is recursive and names /proc/self/root/opt/aitelier-gate-imports"),
    ("E6  a symlink, then a chmod of the link",
     "RUN ln -s aitelier-gate-imports /opt/g && chmod 0700 /opt/g",
     "creates a link or a mount"),
    ("E4  bash brace expansion, armed by a SHELL override",
     'SHELL ["/bin/bash","-c"]\n'
     "RUN chmod -R 0700 /opt/aitelier-gate-{imports,x}",
     "replaces the shell every later RUN is read through"),
    ("E1  a quoted WORKDIR, then `COPY ... ./` (the placement, not the pin, was the hole)",
     'WORKDIR "/opt/aitelier-gate-imports/docker/godot"\n'
     "COPY --chmod=0600 docker/godot/godot_harness.py ./",
     "writes into /opt/aitelier-gate-imports/docker/godot with mode 0600"),
)


def test_every_shape_the_fourth_review_measured_green_is_now_refused():
    """Six more spellings, each re-measured against 0cbdcd30 before this round began.

    The director reproduced all six as one-line mutations of the shipped Dockerfile
    (`evidence/guard-r4-escapes/escapes-reproduced-v2.txt`) with the unmutated file green
    as the control, so these are measurements and not readings of the code.

    The seventh row is the reviewer's E1, and HOW it is mutated is the whole point:
    REPLACING the shipped `COPY --chmod=0644 ... godot_harness.py` with it is refused for
    an unrelated reason (no COPY bakes the client), so the first reproduction scored it
    "not an escape". APPENDING it - the pin still there, then a second copy at 0600
    through a quoted WORKDIR - is green on 0cbdcd30 and would bake 0600 into the image,
    because reading `"/opt/..."` with its quote characters made the destination resolve
    against the previous WORKDIR. That is what `_split_words` fixes; the row is kept
    appended, where it is a real hole.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in FOURTH_REVIEW_SHAPES:
        refusals = _refusals_for(f"{shipped}\n{extra}\n")
        assert refusals, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert expected in refusals, (
            f"{name} was refused, but not by the rule that closes it (expected "
            f"{expected!r}):\n{refusals}")


def test_the_guard_does_not_refuse_a_command_that_names_no_pinned_path():
    """The other direction: a correct line must not be refused.

    Both of these were false refusals the third non-author review named (F1, F2),
    because a FLAG word and a relative SOURCE word were each counted as "a relative
    path, therefore it can reach the export tree". A guard that refuses them is not
    stricter, it is wrong about a build that touches nothing pinned - and the cheapest
    way to silence a wrong guard is to weaken it.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line in ("RUN chmod -x /usr/local/bin/tool",
                 "RUN install -m 0755 scripts/tool /usr/local/bin/tool"):
        assert not _refusals_for(f"{shipped}\n{line}\n"), (
            f"this line names no pinned path and must not be refused: {line}")


def test_a_command_that_is_not_chmod_and_names_the_tree_is_refused():
    """The other three shapes the N6 rule exists for, beyond the reviewer's `ln -s`.

    `chmod` follows a symlink, `cp -p` carries the SOURCE's mode onto the destination,
    and a `python -c` one-liner names the tree inside a string - none of them is a
    `chmod` operand this file reads, and all three re-mode a pinned path. The `ln` row
    of `THIRD_REVIEW_SHAPES` is the measured one; these are the same hole.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line, expected in (
        ("RUN cp -p docker/godot/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
         "is not a plain chmod or install"),
        ("RUN mv /opt/aitelier-gate-imports/docker /tmp/d && mv /tmp/d "
         "/opt/aitelier-gate-imports/docker",
         "is not a plain chmod or install"),
        ("RUN python -c \"import os; os.chmod('/opt/aitelier-gate-imports', 0o700)\"",
         "changes a mode in a shape this file cannot read"),
    ):
        refusals = _refusals_for(f"{shipped}\n{line}\n")
        assert refusals, f"this line re-modes a pinned path and must be refused: {line}"
        assert expected in refusals, (
            f"{line} was refused, but not by the rule that closes it (expected "
            f"{expected!r}):\n{refusals}")
