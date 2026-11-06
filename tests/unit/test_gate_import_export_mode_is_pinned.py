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
at review time, before a rebuild is spent, and the built-image probe under
evidence/gate-import-dir-modes-20261106/ measures the modes it can only predict.

What this file decides
----------------------
Two things, and neither of them is "what a shell command does":

  1. the Dockerfile carries the instruction set it expects - the pinned
     `COPY --chmod=0644 ... godot_harness.py` line and the one `touch`/`chmod` block
     that creates and modes the export tree - compared as canonical text, so only
     whitespace and line continuations are normalised away. Every mode is therefore
     bound to the path token its own `chmod` names: nothing can satisfy this file by
     putting 0755 somewhere in a RUN.
  2. no OTHER instruction names a pinned path, an ancestor whose mode decides whether
     it is reachable (`/`, `/opt`), the client's own file name, or a token that builds
     such a path at parse time (a shell variable, a glob or a brace whose literal head
     already points there). An instruction that mentions the tree and is not one of the
     two expected ones is refused with the token it mentions, whatever command word it
     uses - so `chmod -R 0755 /opt/aitelier-gate-imports`, `install -m 0755 /tmp/x
     /opt/aitelier-gate-imports/docker`, `chmod --reference=/tmp/refmode 0755
     /opt/aitelier-gate-imports` and a later `chmod 0666 .../godot_harness.py` are all
     the same refusal.

That is deliberately NOT a model of `chmod`, `install` or `COPY`: five review rounds
each taught the parser the next shell spelling (a recursive `chmod`, an `install` into
an existing directory, a `--reference` whose mode slot is a filename, an alias path
through `/proc/self/root`, a TAB after `RUN`, a `chmod` glued inside a `SHELL` string)
and each fix was answered by the next spelling. The list of instruction SHAPES was the
bug. What is left is an allowlist of exact instructions plus one mention test, which
is why those rounds' findings now hold without a new rule per finding.

What it cannot decide, and does not claim to
--------------------------------------------
  * an alias that does not spell the name: a symlink, a bind mount, `COPY --from` of a
    tree built elsewhere, or a file in the build context that a RUN executes. Refusing
    every alias is not decidable from text - `chmod -x /usr/local/bin/tool` could be
    one - and refusing every instruction that names a path was measured as a FALSE
    REFUSAL the review called the fastest way to get a guard deleted. A `RUN` that names
    no pinned path is allowed to do whatever it does;
  * a token that spells NO literal character of the path (`*`, `**/godot/*.py`): a glob
    with a literal head that already points at or into the tree is refused, so `/opt/ai*`,
    `/opt/*`, `/opt/aitelier-gate-{imports,x}` and `/*/..` are all caught, but a token
    that begins with the wildcard gives the text nothing to judge;
  * the EFFECTIVE modes in an image, and therefore the modes this file can only predict
    for a tree it read as text. `evidence/gate-import-dir-modes-20261106/` is the probe
    that measures all six pinned paths inside a built image, and the deployment that
    builds the image owns that measurement.

This is a review-time alarm against a MISTAKE, not a defence against an author who
wants to get past it: whoever can edit the Dockerfile can also edit this file. Its job
is that no accidental edit can change the pinned modes without a red test that names the
instruction it did not expect.
"""
from __future__ import annotations

import posixpath
import re
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


#: The tree's own name, and the client's file name. An instruction that spells either one
#: is one this file reads or refuses - there is no third case.
NAME = "aitelier-gate-imports"
CLIENT = "godot_harness.py"

#: Every path the image must bake with a mode, and the mode it must get. The client is not
#: here: it is pinned by the COPY below, and a `chmod` of it is refused outright.
REQUIRED_MODES = {
    "/opt/aitelier-gate-imports": "0755",
    "/opt/aitelier-gate-imports/docker": "0755",
    "/opt/aitelier-gate-imports/docker/godot": "0755",
    "/opt/aitelier-gate-imports/docker/__init__.py": "0644",
    "/opt/aitelier-gate-imports/docker/godot/__init__.py": "0644",
}

#: The two ancestors whose mode decides whether the client can be reached at all. A
#: recursive chmod is not needed to break the pin: `chmod 0700 /opt` does it.
ANCESTORS = ("/", "/opt")

#: The instruction set the shipped Dockerfile is expected to carry, as canonical text
#: (single spaces, keyword uppercased, continuations joined). Written as tokens so that
#: the mode always sits next to the paths its own `chmod` names.
MODE_BLOCK = "RUN " + " ".join((
    "touch",
    f"{EXPORT}/docker/__init__.py",
    f"{EXPORT}/docker/godot/__init__.py",
    "&& chmod 0755",
    EXPORT,
    f"{EXPORT}/docker",
    f"{EXPORT}/docker/godot",
    "&& chmod 0644",
    f"{EXPORT}/docker/__init__.py",
    f"{EXPORT}/docker/godot/__init__.py",
))
PIN = f"COPY --chmod=0644 docker/godot/godot_harness.py {TARGET}"
EXPECTED = (PIN, MODE_BLOCK)


def _directives(text: str) -> list[tuple[str, str]]:
    """Every instruction in file order, as (KEYWORD, arguments).

    Continuations are joined with one space and whitespace inside an instruction is
    collapsed, which is exactly the difference Docker itself ignores - and the one thing
    this file does normalise. A blank line or a `#` comment is not an instruction.
    """
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined = (pending + line).strip()
        pending = ""
        if not joined:
            continue
        keyword, _, arguments = joined.partition(" ")
        instructions.append((keyword, arguments.strip()))
    if pending.strip():
        keyword, _, arguments = pending.strip().partition(" ")
        instructions.append((keyword, arguments.strip()))
    return instructions


def _canonical(keyword: str, arguments: str) -> str:
    """One instruction as the single line this file compares against EXPECTED."""
    return f"{keyword.upper()} {' '.join(arguments.split())}".strip()


def _instructions(text: str) -> list[str]:
    return [_canonical(keyword, arguments) for keyword, arguments in _directives(text)]


def _decides_reachability(path: str) -> bool:
    """True when re-moding `path` decides whether the export tree can be reached."""
    normalised = posixpath.normpath(path) if path else ""
    if normalised in ANCESTORS or normalised == EXPORT:
        return True
    return EXPORT.startswith(normalised.rstrip("/") + "/")


def _build_time_path(token: str) -> str | None:
    """The literal head of a token the shell will expand, or None for a plain token.

    A glob, a brace or a shell variable in a token means the text is not the path: the
    shell decides. Only the literal head can be judged, so the head is what is judged:
    `/opt/*` has the head `/opt/`, and that already points at the tree's ancestor, while
    `/var/lib/apt/lists/*` has the head `/var/lib/apt/lists/` and names nothing pinned.
    """
    first = re.search(r"[*?\[{$`]", token)
    return token[:first.start()] if first else None


def _refusal(token: str) -> str | None:
    """Why this one token makes its instruction one this file cannot allow, or None.

    Four questions, all about the TEXT of the token:
      * does it spell the export tree or the client's file name?
      * does it build a path out of a shell variable (`$DIR/x`, `${D}/..`)?
      * does its literal head, before the expansion, already point at the tree or at an
        ancestor of it (`/opt/*`, `/opt/aitelier-gate-{imports,x}`, `/*/..`)?
      * is it, plainly, one of the two ancestors (`/opt`, `/`)?
    """
    if NAME in token or CLIENT in token:
        return "names the export tree or the client this image pins"
    head = _build_time_path(token)
    if head is None:
        return ("names an ancestor of the export tree, whose mode decides whether it can "
                "be reached") if _decides_reachability(token) else None
    if not head:
        # A token that STARTS with the expansion: the whole path is supplied by the shell.
        # A bare `$CMD` with no slash is a command name, which is not this file's subject.
        return ("builds a path out of a shell variable, so the path is not in the text"
                ) if ("$" in token or "`" in token) and "/" in token else None
    if "$" in head or "`" in head or "/" in head:
        if "$" in token or "`" in token:
            return "builds a path out of a shell variable"
        # The head may be the tree's own beginning rather than one of its directories:
        # `/opt/aitelier-gate-import[[:alpha:]]` and `/opt/aitelier-gate-{imports,x}` both
        # spell everything up to the last character the shell supplies.
        if _decides_reachability(head) or EXPORT.startswith(posixpath.normpath(head)):
            return f"can expand to {EXPORT} or to a path above it"
    return None


def _problems(text: str) -> list[str]:
    """Every way this Dockerfile departs from the instruction set this file expects."""
    problems: list[str] = []
    found: list[str] = []
    for keyword, arguments in _directives(text):
        canonical = _canonical(keyword, arguments)
        if canonical in EXPECTED:
            found.append(canonical)
            continue
        for token in [keyword, *arguments.split()]:
            why = _refusal(token)
            if why:
                shown = canonical if len(canonical) <= 160 else canonical[:157] + "..."
                problems.append(
                    f"{keyword.upper()} carries {token!r}, which {why}. This file reads "
                    "exactly two instructions:\n  " + "\n  ".join(EXPECTED) + "\n"
                    f"and refuses every other instruction that names a pinned path. Got:\n  {shown}\n"
                    "If one of the two really changed, change EXPECTED here deliberately and "
                    "say why in the commit message - do not teach this file to read a shell: "
                    "five review rounds each found the next spelling.")
                break
    for expected in EXPECTED:
        if expected not in found:
            problems.append(
                "the Dockerfile no longer carries this instruction, which IS the pin:\n  "
                f"{expected}\n"
                "The modes the gate client needs are that text; if it changed shape, change "
                "EXPECTED here deliberately and say why in the commit message.")
    return problems


def _added_problems(shipped: str, extra: str) -> list[str]:
    """The problems ONE appended instruction creates, and only those.

    The regression cases below need a Dockerfile they can mutate, so they append a line
    to the shipped text and measure the DIFFERENCE. Measuring the difference rather than
    "the whole file is red" is what keeps a case about its own instruction: a control
    that is already red for another reason (a mutant, or a broken expected block) cannot
    answer for a line that changes nothing, and a line that changes nothing cannot hide
    behind a control that was red anyway.
    """
    before = _problems(shipped)
    return [problem for problem in _problems(f"{shipped}\n{extra}\n")
            if problem not in before]


def _mode_groups(instruction: str) -> dict[str, str]:
    """path -> the mode its own `chmod` group in `instruction` gives it."""
    words = instruction.split()
    groups: dict[str, str] = {}
    index = 0
    while index < len(words):
        if words[index] != "chmod":
            index += 1
            continue
        mode = words[index + 1]
        index += 2
        while index < len(words) and words[index] != "&&":
            assert words[index] not in groups, (
                f"{words[index]} is given a mode twice in the expected block")
            groups[words[index]] = mode
            index += 1
    return groups


def test_the_import_tree_is_searchable_and_not_only_the_file():
    """The directories and markers are created and moded by one block, not chmod-ed
    afterwards.

    A plain `COPY` of the client left /opt/aitelier-gate-imports and its two
    subdirectories at 0644 root:root, so uid 1000 could read the file and not open the
    directories above it. The fix is a block that makes the directories traversable and
    the markers readable, and this case binds each of those modes to the path token its
    own `chmod` names - a `chmod -R 0755` over the tree would take the client's pinned
    file mode with it, and a mode matched anywhere in the RUN could not tell that apart
    from the intended one.
    """
    text = (ROOT / "Dockerfile").read_text()
    problems = _problems(text)
    assert not problems, "\n\n".join(problems)

    groups = _mode_groups(MODE_BLOCK)
    assert groups == REQUIRED_MODES, (
        "the expected block must bind each of these paths to its own mode:\n  "
        + "\n  ".join(f"{mode} {path}" for path, mode in sorted(REQUIRED_MODES.items()))
        + "\nbut it says:\n  "
        + "\n  ".join(f"{mode} {path}" for path, mode in sorted(groups.items())))
    assert TARGET not in groups, (
        f"{TARGET} is pinned by its COPY; a chmod of it is the re-mode this file refuses")

    naming = [instruction for instruction in _instructions(text)
              if NAME in instruction or CLIENT in instruction]
    assert naming == list(EXPECTED), (
        "the shipped Dockerfile must carry exactly the two instructions this file reads, "
        f"in this order:\n  " + "\n  ".join(EXPECTED) + "\nand it carries:\n  "
        + "\n  ".join(naming))


def test_nothing_but_the_pinned_copy_writes_into_the_export_tree():
    """Only one instruction may put a file into the export tree, and it carries the mode.

    A COPY whose destination is the tree, or a directory inside it, adopts whatever mode
    that COPY carries - `COPY --chmod=0600 ... /opt/aitelier-gate-imports/docker/` would
    re-mode the directory and everything under it, and `ADD` and `COPY --from` reach the
    same place from another stage.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    writes = [instruction for instruction in _instructions(shipped)
              if instruction.split(" ")[0] in ("COPY", "ADD")
              and (NAME in instruction or CLIENT in instruction)]
    assert writes == [PIN], (
        f"the only instruction allowed to write into {EXPORT} is `{PIN}`; found:\n  "
        + "\n  ".join(writes))

    for shape, named in (
        ("COPY --chmod=0600 docker/godot/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/", "/opt/aitelier-gate-imports/docker/"),
        ("ADD docker/godot /opt/aitelier-gate-imports/docker/godot",
         "/opt/aitelier-gate-imports/docker/godot"),
        ("COPY --from=builder /out/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"),
    ):
        added = _added_problems(shipped, shape)
        assert added, f"this instruction writes into the export tree and is green:\n{shape}"
        assert any(named in problem for problem in added), (
            f"{shape} was refused, but not for the path it names ({named!r}):\n"
            + "\n".join(added))




#: The nine shapes the third non-author review measured GREEN against bb2e5a53, each with
#: the TOKEN this file must name when it refuses them. The expectation is the token rather
#: than the rule, because the rule is now one rule: a shape refused for an unrelated
#: reason (say, the expected block missing) does not name its token and fails the case,
#: which is what keeps "refused" from meaning "fell over somewhere".
THIRD_REVIEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("N1  COPY destination built from a variable plus `..`",
     "ARG GODOT_DIR=/opt/aitelier-gate-imports/docker/godot\n"
     "COPY --chmod=0600 docker/godot/godot_harness.py ${GODOT_DIR}/../godot/",
     "/opt/aitelier-gate-imports/docker/godot"),
    ("N1b WORKDIR built from a variable plus `..`, then `COPY ... ./`",
     "ARG GODOT_DIR=/opt/aitelier-gate-imports/docker/godot\n"
     "WORKDIR ${GODOT_DIR}/../godot\n"
     "COPY --chmod=0600 docker/godot/godot_harness.py ./",
     "/opt/aitelier-gate-imports/docker/godot"),
    ("N2  shell-only glob `[[:alpha:]]`",
     "RUN chmod -R 0700 /opt/aitelier-gate-import[[:alpha:]]",
     "/opt/aitelier-gate-import[[:alpha:]]"),
    ("N2b shell negation glob `[^x]`",
     "RUN chmod -R 0700 /opt/aitelier-gate-import[^x]",
     "/opt/aitelier-gate-import[^x]"),
    ("N3  `..` resolved through a symlink",
     "RUN chmod -R 0700 /var/run/../opt/aitelier-gate-imports",
     "/var/run/../opt/aitelier-gate-imports"),
    ("N4  `install` reached by its full path",
     "RUN /usr/bin/install -d -m 0700 /opt/aitelier-gate-imports/docker/godot",
     "/opt/aitelier-gate-imports/docker/godot"),
    ("N5  a mode change carried by a SHELL instruction",
     'SHELL ["/bin/sh","-c",'
     '"chmod -R 0700 /opt/aitelier-gate-imports; eval \\"$1\\"","sh"]',
     "/opt/aitelier-gate-imports;"),
    ("N6  a symlink alias, then a chmod of the alias",
     "RUN ln -s /opt/aitelier-gate-imports/docker/godot /tmp/g && chmod 0700 /tmp/g",
     "/opt/aitelier-gate-imports/docker/godot"),
    ("N7  a chmod of `/` spelled `/*/..`",
     "RUN chmod 0700 /*/..",
     "/*/.."),
)


def test_every_shape_the_third_review_measured_green_is_now_refused():
    """The nine spellings that re-moded a pinned path while the guard stayed green.

    Measured (not theorised) on bb2e5a53 with the mutant matrix: "mutants: 27,
    escapes: 9, shipped green: True". Each line below is one of those nine appended to
    the shipped Dockerfile, and each must now be refused with the token it names - for
    `/*/..` that is the literal head `/`, which is the parent whose mode decides whether
    the tree can be reached at all.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in THIRD_REVIEW_SHAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert any(expected in problem for problem in added), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))


#: The six shapes the FOURTH non-author review measured GREEN against 0cbdcd30, plus the
#: one it named that was not reproducible as stated. Each is a one-line mutation of the
#: SHIPPED Dockerfile (the control), and the expectation again names the offending token.
FOURTH_REVIEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("E2  `install -t DIR` puts the target behind a flag",
     "RUN install -m 0700 -t /opt/aitelier-gate-imports/docker /tmp/__init__.py",
     "/opt/aitelier-gate-imports/docker"),
    ("E5  a `chmod` glued inside a SHELL string with `;`",
     'SHELL ["/bin/sh","-c",'
     '"true;chmod -R 0700 /opt/aitelier-gate-imports;eval \\"$1\\"","sh"]',
     "/opt/aitelier-gate-imports;"),
    ("E7  `--recurs`, the getopt_long abbreviation of `--recursive`",
     "RUN chmod --recurs 0755 /opt/aitelier-gate-imports",
     "/opt/aitelier-gate-imports"),
    ("E3  the export tree reached through `/proc/self/root`",
     "RUN chmod -R 0700 /proc/self/root/opt/aitelier-gate-imports",
     "/proc/self/root/opt/aitelier-gate-imports"),
    ("E6  a symlink, then a chmod of the link",
     "RUN ln -s aitelier-gate-imports /opt/g && chmod 0700 /opt/g",
     "aitelier-gate-imports"),
    ("E4  bash brace expansion, armed by a SHELL override",
     'SHELL ["/bin/bash","-c"]\n'
     "RUN chmod -R 0700 /opt/aitelier-gate-{imports,x}",
     "/opt/aitelier-gate-{imports,x}"),
    ("E1  a quoted WORKDIR, then `COPY ... ./`",
     'WORKDIR "/opt/aitelier-gate-imports/docker/godot"\n'
     "COPY --chmod=0600 docker/godot/godot_harness.py ./",
     '"/opt/aitelier-gate-imports/docker/godot"'),
)


def test_every_shape_the_fourth_review_measured_green_is_now_refused():
    """Six more spellings, each re-measured against 0cbdcd30 before that round began.

    The director reproduced all six as one-line mutations of the shipped Dockerfile
    (`evidence/guard-r4-escapes/escapes-reproduced-v2.txt`) with the unmutated file green
    as the control, so these are measurements and not readings of the code.

    The seventh row is the reviewer's E1, and HOW it is mutated is the whole point:
    REPLACING the shipped `COPY --chmod=0644 ... godot_harness.py` with it is refused for
    an unrelated reason (no COPY bakes the client), so the first reproduction scored it
    "not an escape". APPENDING it - the pin still there, then a second copy at 0600
    through a quoted WORKDIR - was green then and would bake 0600 into the image, because
    reading `"/opt/..."` with its quote characters made the destination resolve against
    the previous WORKDIR. Under this file it is refused for the token it carries, quote
    characters and all: what counts is the text, not how it parses.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in FOURTH_REVIEW_SHAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert any(expected in problem for problem in added), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))


#: The five shapes the FIFTH non-author review measured on f0665c5d (round 5). Four were
#: green escapes and one was a false refusal; the two lists are kept apart because they
#: are the two directions this file can be wrong in.
FIFTH_REVIEW_ESCAPES: tuple[tuple[str, str, str], ...] = (
    ("A  `install` copies INTO the existing directory",
     "RUN touch /tmp/__init__.py\n"
     "RUN install -m 0755 /tmp/__init__.py /opt/aitelier-gate-imports/docker",
     "/opt/aitelier-gate-imports/docker"),
    ("B  `--reference` makes the mode slot a file name",
     "RUN install -m 0700 /dev/null /tmp/refmode\n"
     "RUN touch 0755\n"
     "RUN chmod --reference=/tmp/refmode 0755 /opt/aitelier-gate-imports",
     "/opt/aitelier-gate-imports"),
    ("C  a non-recursive chmod of a pinned directory through an alias",
     "RUN chmod 0700 /proc/self/root/opt/aitelier-gate-imports/docker/godot",
     "/proc/self/root/opt/aitelier-gate-imports/docker/godot"),
    ("D  a TAB after RUN made the instruction vanish from the parser",
     "RUN\tchmod 0666 "
     "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
     "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"),
)

FIFTH_REVIEW_FALSE_REFUSALS: tuple[str, ...] = (
    'LABEL description="pip install"',
)


def test_the_fifth_review_shapes_are_refused_and_its_false_refusal_is_read():
    """The round-5 review, answered in the direction that closes it - and only there.

    A and B are the reason this file stops reading `chmod`/`install` operands at all: the
    destination of an `install` is a directory it copies INTO, and with `--reference` the
    mode slot is a file name, so a parser that binds "mode, then paths" was wrong about
    both. C is an alias path the old rule only caught when the chmod was recursive. D is
    a TAB, which Docker treats as a separator and the old parser did not, so the
    instruction disappeared. None of them needs a rule now: each names the tree.

    The fifth shape is the other direction. `LABEL description="pip install"` was refused
    as "a mode change this file cannot read", which is the fastest way to get a guard
    deleted, and it is here so that closing the escapes cannot be done by widening the
    refusal again.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in FIFTH_REVIEW_ESCAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert any(expected in problem for problem in added), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))
    for line in FIFTH_REVIEW_FALSE_REFUSALS:
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")


def test_the_guard_does_not_refuse_a_command_that_names_no_pinned_path():
    """The other direction: a correct line must not be refused.

    Every one of these was refused by an earlier round - a flag word and a relative
    source word counted as "a relative path, therefore it can reach the export tree", a
    `+x` operand counted as a mode, and a `LABEL` string containing "install" counted as
    an install. A guard that refuses them is not stricter, it is wrong about a build that
    touches nothing pinned, and the cheapest way to silence a wrong guard is to weaken it.

    The client's own file name is NOT in this list. Naming `godot_harness.py` in a RUN is
    refused by contract even when the line only copies it somewhere harmless, because this
    file does not read what a RUN does with it: the refusal names the token, and a build
    that really needs it changes the expected set deliberately.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line in (
        "RUN chmod -x /usr/local/bin/tool",
        "RUN chmod +x scripts/entrypoint.sh",
        "RUN chmod u+x scripts/entrypoint.sh",
        "RUN install -m 0755 scripts/tool /usr/local/bin/tool",
        "RUN rm -rf /var/lib/apt/lists/*",
        "RUN chmod -R 0755 /opt/aitelier-dsh",
        "RUN cp -p /usr/local/bin/tool /usr/local/bin/tool.bak",
        "RUN $BUILD_TOOL --version",
        "RUN rm -rf /tmp/build/*.log",
        "RUN chmod 0755 /srv/*",
        'LABEL description="pip install"',
    ):
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")


def test_a_command_that_is_not_chmod_and_names_the_tree_is_refused():
    """The three shapes the mention rule exists for, beyond the reviewer's `ln -s`.

    `chmod` follows a symlink, `cp -p` carries the SOURCE's mode onto the destination,
    a `python -c` one-liner names the tree inside a string, and `${GODOT_DIR}/..` is a
    path the text does not contain at all - none of them is a `chmod` operand this file
    reads, and every one re-modes or replaces a pinned path. The old rule needed a command
    word for each of them; the mention rule needs none, which is why it holds for a command
    this file has never seen.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line, expected in (
        ("RUN cp -p docker/godot/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"),
        ("RUN mv /opt/aitelier-gate-imports/docker /tmp/d && mv /tmp/d "
         "/opt/aitelier-gate-imports/docker",
         "/opt/aitelier-gate-imports/docker"),
        ("RUN python -c \"import os; os.chmod('/opt/aitelier-gate-imports', 0o700)\"",
         "/opt/aitelier-gate-imports',"),
        ("RUN chmod 0700 ${GODOT_DIR}/..", "${GODOT_DIR}/.."),
    ):
        added = _added_problems(shipped, line)
        assert added, f"this line re-modes a pinned path and must be refused: {line}"
        assert any(expected in problem for problem in added), (
            f"{line} was refused, but not for the path it names (expected "
            f"{expected!r}):\n" + "\n".join(added))
