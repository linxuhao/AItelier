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
     it is reachable (`/`, `/opt`), the client's own file name, or a WORD that builds
     such a path at parse time (a shell variable, a glob or a brace whose literal head
     already points there). What separates one word from the next is decided in one
     place, `_split_words`, and it is deliberately narrower than a shell: whitespace
     (so a TAB separates exactly as a space does), an UNQUOTED `;`, `|`, `&`, `(`, `)`,
     `<`, `>`, and an unquoted backslash escaping the character after it. A separator
     INSIDE quotes does NOT separate (`sed -i 's|/opt|/srv|'` is three words, not six),
     a BACKTICK stays inside its word, and quoting itself is removed later by `_word`
     (`"/opt"`, `/o'pt'`, `\\/opt` and `//opt` are all the same word to it). The exec form
     is read as the JSON array Docker reads it as, and a line continuation is DELETED
     rather than spaced - `/opt/aitelier-gate-im\\` + `ports/...` is one word, which is
     the hole the round-6 review measured green. An instruction that mentions the tree
     and is not one of the two expected ones is refused with the word it mentions,
     whatever command word it uses - so `chmod -R 0755 /opt/aitelier-gate-imports`,
     `install -m 0755 /tmp/x /opt/aitelier-gate-imports/docker`,
     `chmod --reference=/tmp/refmode 0755 /opt/aitelier-gate-imports` and a later
     `chmod 0666 .../godot_harness.py` are all the same refusal.

     Two false refusals are the stated price of judging text, and both are pinned by
     `test_the_documented_false_refusal_is_refused_on_purpose`:

       * a word that STARTS with an expansion (`${DIR}/x`, `${DIR}/..`) is refused when
         the expansion supplies a path: its literal head is empty, so the text says
         nothing about where it goes. A harmless `cp ${SRC}/x /tmp/` goes with it. A bare
         `$BUILD_TOOL` names no path and IS read;
       * a word with a `/`-headed literal head that also carries `$` or a backtick is
         refused, because the literal part already points somewhere and the rest is
         spliced on. That refuses `ENV PATH="/opt/venv/bin:$PATH"` and
         `curl https://host/v${VERSION}/tool.tgz -o /tmp/t`, which are legitimate lines.

     A third cost is stated and pinned in `test_the_seventh_review_false_refusals_are_read`:
     a word that IS `/` or `/opt` is refused outright (`WORKDIR /`, `RUN mkdir -p /opt`,
     `COPY --from=build / /`, `RUN find / -name '*.pyc' -delete`), because a mode on either
     decides whether the export tree can be reached and the text of a RUN does not say what
     it does with it. Those are real build lines this file will not read.

     `_directives` also drops blank and `#`-comment lines INSIDE a continuation, exactly
     as Docker does; before the round-7 fix a comment line ended the instruction and left
     two halves of the name as separate words.

That is deliberately NOT a model of `chmod`, `install` or `COPY`: five review rounds
each taught the parser the next shell spelling (a recursive `chmod`, an `install` into
an existing directory, a `--reference` whose mode slot is a filename, an alias path
through `/proc/self/root`, a TAB after `RUN`, a `chmod` glued inside a `SHELL` string)
and each fix was answered by the next spelling. The list of instruction SHAPES was the
bug. What is left is an allowlist of exact instructions plus one mention test, which
is why those rounds' findings now hold without a new rule per finding.

The sixth round found what a spelling is made of rather than another command: a word is
not the bytes between two spaces (`"/opt"`, `/opt;`, `//opt`, the exec form's JSON
array, a continued line). The seventh found the other half - a word can be SPLIT in the
middle of a name (a backtick pair, a comment line inside a continuation) and a separator
can sit inside quotes - and it also measured this file refusing a `sed` expression whose
`|` was quoted. That fix is not one function: `_split_keyword` splits the keyword on any
whitespace, `_directives` joins continuations and drops comment/blank lines, `_words`
reads the exec form, `_split_words` decides what separates a word, `_word` removes what a
shell would remove, and `_decides_reachability` normalises the leading slashes. The shapes
below are rows of a table rather than five more branches.

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
  * a path whose VALUE this file cannot know: a bare `$VAR` or `${VAR}` carrying one
    (`ENV D=/opt` followed by `RUN chmod 0700 $D` is green here), a cwd-relative spelling
    (`..` is `/` because the last `WORKDIR` is `/app`, and `~/../..` is `/` because HOME
    is `/home/app`), the RESULT of a command substitution (`$(dirname /opt/x)` is three
    words, none of them the directory it prints), and an alias path such as
    `/proc/self/root`, which IS `/`. None of them is decidable from the text without
    running a shell, and running one is what five rounds proved is the wrong trade;
  * bash-only forms after an explicit `SHELL ["/bin/bash","-c"]`: `{/opt,/opt}` reduces
    to `/opt,/opt` once the braces come off, and `$'\\x2fopt'` to `$x2fopt`, so neither
    spells a path this file can judge;
  * `test_the_declared_non_claims_are_still_accepted` pins every shape above as ACCEPTED,
    with the measured Dockerfile line in each row, so the boundary is a measured
    decision rather than a silence. Closing one of them is a deliberate change to that
    table, not an accident;
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

import json
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


def _split_keyword(instruction: str) -> tuple[str, str]:
    """The KEYWORD of one instruction and the rest of its line.

    Split on ANY whitespace, because a TAB is whitespace to Docker. A parser that split
    on a space alone read `RUN\\tchmod 0700 /opt` as one unknown keyword, so the path it
    named was never judged; the round-6 review measured that shape green.
    """
    parts = instruction.split(None, 1)
    return (parts[0], parts[1].strip()) if len(parts) == 2 else (parts[0], "")


def _directives(text: str) -> list[tuple[str, str]]:
    """Every instruction in file order, as (KEYWORD, arguments).

    A `\\` at the end of a line is REMOVED, not replaced by a space - Docker deletes the
    backslash and the newline, so `/opt/aitelier-gate-im\\` followed by `ports/...` is one
    word and not two. The round-6 non-author review measured the space-inserting version
    green on exactly that instruction: it split the client's path into two operands that
    each spelled half of the name. Whitespace inside an instruction is then collapsed,
    which is the only difference in WHITESPACE Docker itself ignores.

    A blank line or a `#` comment is not an instruction - and Docker drops both INSIDE a
    continuation too, which is what the round-7 review measured green: `RUN chmod -R 0777
    /opt/aitelier-gate-im\\` + `# x` + `ports` runs the full path while the comment line
    ended the instruction here, leaving two words that each spelled half of the name. So
    they are skipped whether or not a continuation is pending.
    """
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1]
            continue
        joined = (pending + line).strip()
        pending = ""
        if not joined:
            continue
        instructions.append(_split_keyword(joined))
    if pending.strip():
        instructions.append(_split_keyword(pending.strip()))
    return instructions


def _canonical(keyword: str, arguments: str) -> str:
    """One instruction as the single line this file compares against EXPECTED."""
    return f"{keyword.upper()} {' '.join(arguments.split())}".strip()


def _instructions(text: str) -> list[str]:
    return [_canonical(keyword, arguments) for keyword, arguments in _directives(text)]


def _decides_reachability(path: str) -> bool:
    """True when re-moding `path` decides whether the export tree can be reached.

    `//opt` is `/opt` on Linux but POSIX lets `normpath` keep a leading double slash, so
    the leading slashes are collapsed before the comparison.
    """
    normalised = posixpath.normpath(path) if path else ""
    if not normalised:
        return False
    if normalised.startswith("//"):
        normalised = "/" + normalised.lstrip("/")
    if normalised in ANCESTORS or normalised == EXPORT:
        return True
    return EXPORT.startswith(normalised.rstrip("/") + "/")


def _build_time_path(token: str) -> str | None:
    """The literal head of a token the shell will expand, or None for a plain token.

    A glob, a brace or a shell variable in a token means the text is not the path: the
    shell decides. Only the literal head can be judged, so the head is what is judged:
    `/opt/*` has the head `/opt/`, and that already points at the tree's ancestor, while
    `/var/lib/apt/lists/*` has the head `/var/lib/apt/lists/` and names nothing pinned.

    A BACKTICK counts here for the same reason `$` does, and `_split_words` deliberately
    keeps it inside its word, so `/opt/aitelier-gate-imp``orts` has the head
    `/opt/aitelier-gate-imp`: a path that already points into the tree's parent, spliced
    with a substitution this file cannot read. Splitting on the backtick instead left two
    halves that spelled neither a name nor an ancestor, which is the round-7 regression.
    `$(...)`, by contrast, IS split (on the `(`), so the result of a command substitution
    is a declared non-claim rather than a refusal.
    """
    first = re.search(r"[*?\[{$`]", token)
    return token[:first.start()] if first else None


#: What is taken off the EDGES of a word, where a shell's own syntax reaches into it:
#: the quotes and backslashes it removes, and the brackets, comma and `=` of an exec-form
#: array (`["chmod","0700","/opt"]` is three words, and `[` is no part of `chmod`).
#: Braces ARE here, and that is a stated cost rather than an oversight: a word `{/opt}`
#: reduces to `/opt` and is refused, while a word `{/opt,/opt}` reduces to `/opt,/opt`,
#: which names nothing this file knows. Neither brace form survives as a brace by the time
#: a word is judged, so bash brace expansion is a declared non-claim (see the docstring).
_EDGE = "\"'\\[]{},="
_UNQUOTED = {ord(character): None for character in "\"'\\"}


def _split_words(text: str) -> list[str]:
    """The words of one instruction's shell text, each still as WRITTEN (quoting attached).

    Three things separate one word from the next, and only three:

      * whitespace - `\\s`, so a TAB after `RUN` separates exactly as a space does (an
        earlier space-only split made a whole instruction vanish);
      * an UNQUOTED shell operator: `;`, `|`, `&`, `(`, `)`, `<`, `>`;
      * an UNQUOTED backslash, which escapes the character after it and so keeps both.

    Three things deliberately do NOT separate words:

      * a separator INSIDE quoting: `sed -i 's|/opt|/srv|'` is three words to a shell,
        not six, and the round-7 review measured the six-word reading refusing that
        harmless line - a false refusal, which is the fastest way to get a guard deleted;
      * a BACKTICK: it stays inside its word, so `/opt/aitelier-gate-imp``orts` remains
        one word whose literal head stops at the backtick, and `_refusal`'s expansion rule
        refuses it. Splitting on the backtick is what let round 7's regression through:
        the two halves spelled no name and no ancestor;
      * quoting itself: `"/opt"` is one word, and what it names is decided after the
        quotes come off, in `_word`.

    Nothing more of quoting is modelled than that: no nested quoting, no `$'...'`, no
    arithmetic. A word this cannot read is not silently dropped - it keeps whatever it
    spells, and `_refusal` judges the literal text.
    """
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            current.append(character)
            if character == quote:
                quote = None
            index += 1
            continue
        if character in "\"'":
            quote = character
            current.append(character)
        elif character == "\\" and index + 1 < len(text):
            current.append(character)
            index += 1
            current.append(text[index])
        elif character.isspace() or character in ";|&()<>":
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(character)
        index += 1
    if current:
        words.append("".join(current))
    return words


def _words(arguments: str) -> list[str]:
    """The words of one instruction, each still as WRITTEN (its quoting attached).

    Docker has two forms and they split differently. The exec form IS a JSON array -
    Docker reads it with a JSON reader, so this file does too, and
    `RUN ["chmod","0700","/opt"]` is three words rather than one bracket-wrapped blob
    that named nothing; the round-6 review measured that shape green. Everything else is
    shell text, split by `_split_words` on whitespace and on an UNQUOTED operator, with a
    backtick and a separator inside quoting left inside their word. An exec-form element
    may itself be shell text (`SHELL ["/bin/sh","-c","true;chmod -R 0700 /opt"]`), so each
    element is split again.
    """
    text = arguments.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return [word for item in parsed for word in _split_words(item)]
    return _split_words(text)


def _word(raw: str) -> str:
    """The word a shell would pass on for this one: quoting and edge punctuation gone.

    `/"opt"`, `/o'pt'`, `\\/opt`, `"/opt"` and `/opt;` are all `/opt` to a shell, and the
    round-6 review measured three of those spellings GREEN while a chmod of them re-moded
    the ancestor. A word this reduces to nothing is not judged at all.
    """
    return raw.translate(_UNQUOTED).strip(_EDGE)


def _refusal(word: str) -> str | None:
    """Why this one WORD makes its instruction one this file cannot allow, or None.

    Four questions, all about the text of the word (`_word` has already taken the shell's
    quoting and word separators off it):
      * does it spell the export tree or the client's file name?
      * does it build a path out of a shell variable (`$DIR/x`, `${D}/..`)?
      * does its literal head, before the expansion, already point at the tree or at an
        ancestor of it (`/opt/*`, `/opt/aitelier-gate-{imports,x}`, `/*/..`)?
      * is it, plainly, one of the two ancestors (`/opt`, `/`)?

    Two costs are stated rather than hidden, and both are pinned as false refusals:
    a word that STARTS with an expansion (`${SRC}/tool`) is refused because its whole path
    comes from the shell, and a word with a `/`-headed literal head that ALSO carries `$`
    or a backtick is refused because the literal part already points somewhere and the
    rest is spliced on. The second one refuses legitimate lines: `ENV PATH="/opt/venv/
    bin:$PATH"` and `RUN curl https://host/v${VERSION}/tool.tgz -o /tmp/t`. That is the
    price of judging text, and the module docstring names it.
    """
    if NAME in word or CLIENT in word:
        return "names the export tree or the client this image pins"
    head = _build_time_path(word)
    if head is None:
        return ("names an ancestor of the export tree, whose mode decides whether it can "
                "be reached") if _decides_reachability(word) else None
    if not head:
        # A word that STARTS with the expansion: the whole path is supplied by the shell.
        # A bare `$CMD` with no slash is a command name, which is not this file's subject.
        return ("builds a path out of a shell variable, so the path is not in the text"
                ) if ("$" in word or "`" in word) and "/" in word else None
    if "/" in head:
        if "$" in word or "`" in word:
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
        for raw in [keyword, *_words(arguments)]:
            word = _word(raw)
            why = _refusal(word)
            if why:
                written = "" if word == raw else f" (a shell word: {word!r})"
                shown = canonical if len(canonical) <= 160 else canonical[:157] + "..."
                problems.append(
                    f"{keyword.upper()} carries {raw!r}{written}, which {why}. This file reads "
                    "exactly two instructions:\n  " + "\n  ".join(EXPECTED) + "\n"
                    f"and refuses every other instruction that names a pinned path. Got:\n  {shown}\n"
                    "If one of the two really changed, change EXPECTED here deliberately and "
                    "say why in the commit message - do not teach this file to read a shell: "
                    "six review rounds each found the next spelling.")
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


def _names(problems: list[str], word: str) -> bool:
    """True when some refusal names `word` on its FIRST line.

    Only the first line: every message then quotes the whole EXPECTED block, which
    contains the pinned paths, so `word in problem` is true even for a refusal that named
    a different word entirely. The round-6 review measured that vacuity in these very
    assertions, and a case that cannot tell "refused for its own token" from "refused for
    the pinned path printed underneath" is not a case about the shape it lists.
    """
    return any(word in problem.splitlines()[0] for problem in problems)


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

    Each row copies from a source that names nothing pinned, so the refusal has to name
    the DESTINATION - the word that decides where the mode lands. A source that spells
    `godot_harness.py` is refused on that word first, which is the same contract and a
    different row (`test_a_command_that_is_not_chmod_and_names_the_tree_is_refused`).
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    writes = [instruction for instruction in _instructions(shipped)
              if instruction.split(" ")[0] in ("COPY", "ADD")
              and (NAME in instruction or CLIENT in instruction)]
    assert writes == [PIN], (
        f"the only instruction allowed to write into {EXPORT} is `{PIN}`; found:\n  "
        + "\n  ".join(writes))

    for shape, named in (
        ("COPY --chmod=0600 /tmp/tool /opt/aitelier-gate-imports/docker/",
         "/opt/aitelier-gate-imports/docker/"),
        ("ADD docker/godot /opt/aitelier-gate-imports/docker/godot",
         "/opt/aitelier-gate-imports/docker/godot"),
        ("COPY --from=builder /out/tool /opt/aitelier-gate-imports/docker/godot/tool",
         "/opt/aitelier-gate-imports/docker/godot/tool"),
    ):
        added = _added_problems(shipped, shape)
        assert added, f"this instruction writes into the export tree and is green:\n{shape}"
        assert _names(added, named), (
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
     "/opt/aitelier-gate-imports"),
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
        assert _names(added, expected), (
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
     "/opt/aitelier-gate-imports"),
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
        assert _names(added, expected), (
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
        assert _names(added, expected), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))
    for line in FIFTH_REVIEW_FALSE_REFUSALS:
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")


#: The escapes the SIXTH non-author reviews (round 6, two model families) measured GREEN
#: against 3437a37b, with the word each must be refused by. G1-G5 were measured by those
#: reviews; G9 (`//opt`) is one of the spellings their brief lists as measured green, and
#: G6-G8 are not findings - they are the same word rule exercised on the next spellings
#: its own examples imply, so the rule cannot be tightened for one of them and left open
#: for its neighbour.
SIXTH_REVIEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("G1  the ancestor quoted, as a shell word",
     'RUN chmod 0700 "/opt"',
     '"/opt"'),
    ("G1b the ancestor with a `;` attached",
     "RUN chmod 0700 /opt; true",
     "/opt"),
    ("G1c the literal head quoted, then a glob",
     'RUN chmod -R 0700 "/opt/"*',
     '"/opt/"*'),
    ("G2  the client's path split by a line continuation",
     "RUN chmod 0666 /opt/aitelier-gate-im\\\n"
     "ports/docker/godot/godot_har\\\n"
     "ness.py",
     "aitelier-gate-im"),
    ("G3  the ancestor in EXEC form, which Docker reads as a JSON array",
     'RUN ["chmod","0700","/opt"]',
     "/opt"),
    ("G4  the ancestor as an earlier word of a `;`-joined list",
     "RUN cd /opt;chmod -R 0700 a*",
     "/opt"),
    ("G5  a TAB after RUN, which a space-only keyword split read as one word",
     "RUN\tchmod 0700 /opt",
     "/opt"),
    ("G6  the ancestor with its separator escaped",
     "RUN chmod 0700 \\/opt",
     "\\/opt"),
    ("G7  the ancestor with a quote inside the word",
     'RUN chmod 0700 /"opt"',
     '/"opt"'),
    ("G8  the ancestor with its second letter quoted",
     "RUN chmod 0700 /o'pt'",
     "/o'pt'"),
    ("G9  the ancestor spelled with two leading slashes",
     "RUN chmod 0700 //opt",
     "//opt"),
)


def test_every_shape_the_sixth_review_measured_green_is_now_refused():
    """The round-6 reviews' holes, and the neighbours of the rule that closes them.

    G1 is quote removal: `"/opt"`, `/opt;` and `/o'pt'` are the same word to a shell as
    `/opt`, and the guard judged the bytes between the spaces. G2 is the one that is not
    about quotes at all: this file joined a continued line with a SPACE where Docker
    deletes the backslash and the newline, so `.../aitelier-gate-im\\` + `ports/...`
    became two words that each spelled half of the name and neither of the two names this
    file refuses - both reviews found that one, independently. G3, G4 and G5 are the three
    word BOUNDARIES the second review named: the exec form is a JSON array and not one
    bracket-wrapped word, `;` ends a word, and a TAB separates the keyword.

    G9 is a finding of that review - `//opt` is one of the spellings its brief lists as
    measured green against 3437a37b, and it needed a code change rather than a sentence:
    `posixpath.normpath` keeps the leading double slash, so the ancestor comparison has to
    collapse it first. G6-G8 are not findings: an escaped separator, a quote inside the
    word and a quoted second letter are all the SAME word as `/opt`, and the row exists so
    the rule cannot be taught one of them and left open on its neighbour. Each is refused
    with the word in the first line of the message, which is the only line `_names` reads.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in SIXTH_REVIEW_SHAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert _names(added, expected), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))


#: The escapes the SEVENTH non-author review measured against the round-6 candidate
#: (ee02377d2b), with the word each must be refused by. E1a and E1b are a REGRESSION from
#: 3437a37b rather than a hole that was always there: the round-6 tokenizer split a word on
#: a backtick pair, so `/opt/aitelier-gate-imp``orts` became two halves that spelled
#: neither the tree's name nor one of its ancestors. E2 and E2b reopen the G2 class:
#: Docker drops a comment line or an empty line INSIDE a continuation, and `_directives`
#: ended the instruction there, leaving two halves again.
SEVENTH_REVIEW_ESCAPES: tuple[tuple[str, str, str], ...] = (
    ("E1a a recursive chmod of the tree whose name is split by a backtick pair",
     "RUN chmod -R 0777 /opt/aitelier-gate-imp``orts",
     "/opt/aitelier-gate-imp``orts"),
    ("E1b the client re-moded, with both halves of its name split",
     "RUN chmod 0666 /opt/aitelier-gate-imp``orts/docker/godot/godot_har``ness.py",
     "/opt/aitelier-gate-imp``orts/docker/godot/godot_har``ness.py"),
    ("E2  a comment line inside a continuation",
     "RUN chmod -R 0777 /opt/aitelier-gate-im\\\n# x\nports",
     "aitelier-gate-imports"),
    ("E2b an empty line inside a continuation",
     "RUN chmod -R 0777 /opt/aitelier-gate-im\\\n\nports",
     "aitelier-gate-imports"),
)

#: The FALSE refusals the seventh review measured, the first of them new that round: a
#: separator INSIDE quoting was taken as a word boundary, so a sed EXPRESSION became a word
#: `/opt` that no shell ever produces. 3437a37b accepted both spellings.
SEVENTH_REVIEW_FALSE_REFUSALS: tuple[str, ...] = (
    "RUN sed -i 's|/opt|/srv|' /etc/app.conf",
    "RUN sed -i 's#/opt#/srv#' /etc/app.conf",
    "RUN sed -i 's/\\/opt/\\/srv/' /etc/app.conf",
)

#: Legitimate build lines this file refuses ON PURPOSE, with the reason the refusal has to
#: state. A word that IS `/` or `/opt` decides whether the export tree can be reached, and
#: no text tells `mkdir -p /opt` from `chmod 0700 /opt` - so the mention is the refusal.
SEVENTH_REVIEW_STATED_COSTS: tuple[tuple[str, str], ...] = (
    ("WORKDIR /", "names an ancestor"),
    ("RUN mkdir -p /opt", "names an ancestor"),
    ("COPY --from=build / /", "names an ancestor"),
    ("RUN find / -name '*.pyc' -delete", "names an ancestor"),
    ("RUN tar -C / -xzf /tmp/build.tgz", "names an ancestor"),
)


def test_the_seventh_review_escapes_are_refused():
    """The round-7 review's FAIL: the two that were a regression and the two it reopened.

    Only these four are closed here, and that is deliberate. The same review listed E3-E7
    as ACCEPTED shapes this file has never judged: E3 a path carried in a bare `$VAR`, E4
    a cwd-relative spelling, E5 the RESULT of a command substitution, E6 the bash-only
    brace and `$'\\x2fopt'` forms, E7 an alias path through `/proc/self/root`. Every one of
    them needs a shell to decide, and five rounds of teaching this file one more spelling
    is the history that produced the mention rule - so they are pinned as ACCEPTED in
    `test_the_declared_non_claims_are_still_accepted` and stated as the boundary in the
    module docstring. What could not wait is what the FAIL rests on: a word split by a
    backtick pair was refused by 3437a37b and accepted by its successor, and a comment or
    an empty line inside a continuation ended an instruction Docker continues.

    The expected token is the one the refusal has to NAME, in the first line of its
    message: the word as WRITTEN for E1a and E1b, and the path the join reconstructs for
    E2 and E2b - the whole point of deleting the continuation instead of spacing it is
    that the text then does contain the name.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in SEVENTH_REVIEW_ESCAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is still green, and it re-modes a pinned path:\n{extra}"
        assert _names(added, expected), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))


def test_the_seventh_review_false_refusals_are_read():
    """The other direction: the line the round-7 review measured as a FALSE REFUSAL.

    `sed -i 's|/opt|/srv|' /etc/app.conf` names no pinned path: the `/opt` is a string
    inside a sed expression, and a shell hands the whole quoted word to sed. The round-6
    tokenizer split on `|` wherever it appeared, so this harmless line was refused on a
    word the shell never produces, and 3437a37b accepted it - a false refusal is the
    fastest way to get a guard deleted, which is why the quote-aware split belongs to the
    same fix as the E1 regression rather than to a later loosening.

    The second table is the cost the review named as BY DESIGN BUT UNSTATED. `WORKDIR /`,
    `RUN mkdir -p /opt`, `COPY --from=build / /` and `RUN find / -name '*.pyc' -delete` are
    real builds this file will not read, because the word IS an ancestor and no text tells
    a harmless mention from a mode change. Pinning them makes that a stated decision with a
    test instead of a silence, and the reason is asserted too: a refusal that names the
    ancestor is the contract, not a stray match.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line in SEVENTH_REVIEW_FALSE_REFUSALS:
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")
    for line, reason in SEVENTH_REVIEW_STATED_COSTS:
        added = _added_problems(shipped, line)
        assert added, (
            f"this cost is documented as a refusal and is now read; the docstring says "
            f"it is refused on purpose: {line}")
        assert _names(added, reason), (
            f"{line} was refused, but not because a word of it IS an ancestor:\n"
            + "\n".join(added))


def test_the_guard_does_not_refuse_a_command_that_names_no_pinned_path():
    """The other direction: a correct line must not be refused.

    Every one of these was refused by an earlier round - a flag word and a relative
    source word counted as "a relative path, therefore it can reach the export tree", a
    `+x` operand counted as a mode, and a `LABEL` string containing "install" counted as
    an install. A guard that refuses them is not stricter, it is wrong about a build that
    touches nothing pinned, and the cheapest way to silence a wrong guard is to weaken it.

    The last three rows are the round-7 word rule measured in the direction it is NEW in:
    the exec form is now read as a JSON array, so it must not become a refusal of every
    exec-form instruction, and a `SHELL` that names no path must stay readable. A rule
    that refuses those would be a widening of the refusal, not a tightening of the guard.

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
        'RUN chmod -R 0755 "/opt/aitelier-dsh"',
        "RUN chmod 0755 /usr/local/bin:/opt/aitelier-dsh/bin",
        "RUN chmod 0755 /srv//data",
        "RUN cp -p /usr/local/bin/tool /usr/local/bin/tool.bak",
        "RUN $BUILD_TOOL --version",
        "RUN rm -rf /tmp/build/*.log",
        "RUN chmod 0755 /srv/*",
        'LABEL description="pip install"',
        'RUN ["chmod","+x","scripts/entrypoint.sh"]',
        'SHELL ["/bin/bash","-c"]',
        'RUN ["/usr/local/bin/tool","--version"]',
    ):
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")


def test_the_documented_false_refusal_is_refused_on_purpose():
    """The one cost the docstring states, pinned so that it stays a decision.

    A word that STARTS with an expansion has an empty literal head: the text says
    nothing about where the shell will point it, so this file refuses it. `cp ${SRC}/tool
    /tmp/tool` is harmless in a real build and is still refused - that is the price of not
    modelling the shell, and the module docstring says so. Pinning it here means the cost
    is a stated decision with a test rather than an accident, and the last two rows are
    the other side of the same line: a bare expansion with no path in it names a command
    or an operand and IS read, so the rule is about paths and not about `$`.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line in ("RUN cp ${SRC}/tool /tmp/tool",
                 "RUN chmod 0755 ${DIR}/sub"):
        added = _added_problems(shipped, line)
        assert added, (
            f"the documented false refusal is gone, and the docstring still states it: {line}")
        assert _names(added, "builds a path out of a shell variable"), (
            f"{line} was refused for another reason than the docstring states:\n"
            + "\n".join(added))
    for line in ("RUN ${BUILD_TOOL} --version", "RUN chmod 0755 ${MODE}"):
        assert not _added_problems(shipped, line), (
            f"a bare expansion names no path and must not be refused: {line}")


def test_a_command_that_is_not_chmod_and_names_the_tree_is_refused():
    """The shapes the mention rule exists for, beyond the reviewer's `ln -s`.

    `chmod` follows a symlink, `cp -p` carries the SOURCE's mode onto the destination,
    a `python -c` one-liner names the tree inside a string, a write into the tree from a
    source outside it, and `${GODOT_DIR}/..` is a path the text does not contain at all -
    none of them is a `chmod` operand this file reads, and every one re-modes or replaces
    a pinned path. The old rule needed a command word for each of them; the mention rule
    needs none, which is why it holds for a command this file has never seen.

    The first row is refused on `godot_harness.py`, its SOURCE word, and not on its
    destination: naming the client is refused wherever the name appears, so the source is
    the first word that closes it. That is the contract, and it is why this row expects
    the file name rather than the path printed under it - before `_names` read only the
    first line, this row was green for the pinned path quoted in the message tail, which
    is exactly the vacuity the round-6 review measured.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line, expected in (
        ("RUN cp -p docker/godot/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
         "godot_harness.py"),
        ("RUN cp -p /usr/local/bin/tool /opt/aitelier-gate-imports/docker/godot/tool",
         "/opt/aitelier-gate-imports/docker/godot/tool"),
        ("RUN mv /opt/aitelier-gate-imports/docker /tmp/d && mv /tmp/d "
         "/opt/aitelier-gate-imports/docker",
         "/opt/aitelier-gate-imports/docker"),
        ("RUN python -c \"import os; os.chmod('/opt/aitelier-gate-imports', 0o700)\"",
         "/opt/aitelier-gate-imports"),
        ("RUN chmod 0700 ${GODOT_DIR}/..", "${GODOT_DIR}/.."),
    ):
        added = _added_problems(shipped, line)
        assert added, f"this line re-modes a pinned path and must be refused: {line}"
        assert _names(added, expected), (
            f"{line} was refused, but not for the path it names (expected "
            f"{expected!r}):\n" + "\n".join(added))


#: Every shape the module docstring declares OUT of this file's reach, with the Dockerfile
#: text that exhibits it. Each row is measured against the CURRENT parser, so the boundary
#: is a decision with a test: closing one of them is a visible change to this table, never
#: an accident of the next fix.
NON_CLAIMS: tuple[tuple[str, str], ...] = (
    ("E3  a path carried in a bare variable",
     "ENV D=/opt\nRUN chmod 0700 $D"),
    ("E4  a cwd-relative spelling of an ancestor",
     "RUN chmod 0700 ..\nRUN chmod 0700 ../opt\nRUN chmod 0700 ~/../.."),
    ("E5  the RESULT of a command substitution",
     "RUN chmod 0700 $(dirname /opt/x)"),
    ("E7  an alias path that IS `/` without spelling it",
     "RUN chmod 0700 /proc/self/root"),
    ("E6  a bash brace list after an explicit SHELL",
     'SHELL ["/bin/bash","-c"]\nRUN chmod 0700 {/opt,/opt}'),
    ("E6  a bash ANSI-C quoted slash after an explicit SHELL",
     'SHELL ["/bin/bash","-c"]\nRUN chmod 0700 $\'\\x2fopt\''),
)


def test_the_declared_non_claims_are_still_accepted():
    """The boundary the docstring declares, measured rather than assumed.

    Each row is a way to re-mode a pinned path that this file cannot see, and every one of
    them is ACCEPTED by the parser as it stands. That is not an oversight to be fixed by
    the next reading rule: a bare `$D`, a `..`, a `$(...)`, an alias and a bash-only word
    all need a shell to say what path they name, and the five rounds that taught this file
    one more shell spelling produced the next spelling as the next review's finding. The
    boundary is therefore declared in the module docstring and pinned here, so a later
    commit that closes one of these has to say so by editing this table - and a review that
    finds a NEW escape knows which side of the line it lands on.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra in NON_CLAIMS:
        added = _added_problems(shipped, extra)
        assert not added, (
            f"{name} is declared a NON-CLAIM and must still be accepted, but the parser "
            f"now refuses it - if that is intended, move the row into a refusing table and "
            f"say why in the commit message:\n{extra}\n" + "\n".join(added))
