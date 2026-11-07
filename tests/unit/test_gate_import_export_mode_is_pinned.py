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
     putting 0755 somewhere in a RUN. The mode in that COPY is pinned EXACTLY to 0644
     (leading zeros do not change an octal value, so `0644` and `644` are the same mode
     and `0755` is not): the client's mode comes from this line and from no `chmod`, so
     `--chmod=0755 ... godot_harness.py` used to satisfy the pin while baking an
     executable client - the round-9 review's finding, iss-363a4758f4974b31, refused now
     and pinned in `test_the_client_copy_refuses_every_other_mode`.
  2. no OTHER instruction names a pinned path, an ancestor whose mode decides whether
     it is reachable (`/`, `/opt`), the client's own file name, or a WORD that builds
     such a path at parse time (a shell variable, a glob or a brace whose literal head
     already points there) - and, when a word spells only the NAME of the tree or of the
     client, that it says so instead of claiming a path it never placed (see the ground
     paragraph below). Every RELATIVE operand WORD is first PLACED against the stage's
     WORKDIR, accumulated from the `WORKDIR` instructions before it and reset at every
     `FROM` to what the base brings (an earlier stage's WORKDIR when the `FROM` names one,
     nothing this file can read when it names an external image), because
     `WORKDIR /opt/aitelier-gate-imports` followed by `RUN chmod 0700
     docker/godot/godot_harness.py` re-modes the client with a word that spells no part
     of its path. What separates one word from the next is decided in one
     place, `_split_words`, and it is deliberately narrower than a shell AND blunter:
     whitespace (so a TAB separates exactly as a space does) and `;`, `|`, `&`, `(`, `)`,
     `<`, `>` - whether or not they sit inside quotes. Quoting is removed later, by
     `_word` (`"/opt"`, `/o'pt'`, `\\/opt` and `//opt` are all the same word to it), and
     a BACKTICK stays inside its word. The exec form
     is read as the JSON array Docker reads it as, and a line continuation is DELETED
     rather than spaced - `/opt/aitelier-gate-im\\` + `ports/...` is one word, which is
     the hole the round-6 review measured green. An instruction that mentions the tree
     and is not one of the two expected ones is refused with the word it mentions,
     whatever command word it uses - so `chmod -R 0755 /opt/aitelier-gate-imports`,
     `install -m 0755 /tmp/x /opt/aitelier-gate-imports/docker`,
     `chmod --reference=/tmp/refmode 0755 /opt/aitelier-gate-imports` and a later
     `chmod 0666 .../godot_harness.py` are all the same refusal.

     Quoting does not hide a word from that test, and that is why `RUN sh -c "chmod 0700
     /opt"` is refused: a shell would run that command, so the text is judged as the words
     it contains rather than as one opaque string.

     Four refusals are the stated price of judging text, and every one of them is pinned:

       * a word that STARTS with an expansion (`${DIR}/x`, `${DIR}/..`) is refused when
         the expansion supplies a path: its literal head is empty, so the text says
         nothing about where it goes. A harmless `cp ${SRC}/x /tmp/` goes with it. A bare
         `$BUILD_TOOL` names no path and IS read. Pinned by
         `test_the_documented_false_refusal_is_refused_on_purpose`;
       * a word whose literal head CONTAINS a `/` and which also carries `$` or a backtick
         is refused, because the literal part already points somewhere and the rest is
         spliced on. That refuses `ENV PATH="/opt/venv/bin:$PATH"` and
         `curl https://host/v${VERSION}/tool.tgz -o /tmp/t`, which are legitimate lines.
         Pinned in the same test;
       * a quoted expression that mentions a pinned path is refused, so
         `RUN sed -i 's|/opt|/srv|' /etc/app.conf` fails although a shell would only
         rewrite text there. Reading the quoted string as a command instead is what the
         round-8 candidate did, and its review measured four quoted commands ACCEPTED that
         its base refused; the mention is the rule, and the cost is this one line shape.
         Pinned in the same test;
       * a word that IS `/` or `/opt` is refused outright (`WORKDIR /`, `RUN mkdir -p
         /opt`, `COPY --from=build / /`, `RUN find / -name '*.pyc' -delete`), because a
         mode on either decides whether the export tree can be reached and the text of a
         RUN does not say what it does with it. Those are real build lines this file will
         not read. Pinned by `test_the_seventh_review_stated_costs_are_refused_on_purpose`.

     `_directives` also drops blank and `#`-comment lines INSIDE a continuation, exactly
     as Docker does; before that change a comment line ended the instruction and left
     two halves of the name as separate words.

     A refusal says WHICH ground it rests on, and the two are kept apart because a message
     that over-claims is how a guard stops being read. `NAMES` is for a word whose MEASURED,
     exact placement IS a pinned path: the tree, a path inside it (the client's own file
     among them), or an ancestor whose mode decides whether it is reachable (`/`, `/opt`) -
     an absolute word as itself, never joined to a WORKDIR, or a relative word the stage
     WORKDIR puts on one of those, and then the message prints the path it resolved to.
     `UNPLACED` ("could reach a pinned path, but the text does not place it on one") is for
     everything the text cannot settle, and its message says what WAS measured: a word that
     spells the NAME but resolves elsewhere (`/app/aitelier-gate-imports`, or
     `aitelier-gate-imports` under `WORKDIR /app` - the same place, the same ground, and the
     message prints it), a name in a stage with no WORKDIR this file can read, a `..` the
     kernel resolves after it follows a symlink, a glob the shell completes from a literal
     head (printed as the HEAD, with that sentence, never as the path), a path the shell
     supplies part of, or a `COPY`/`ADD` SOURCE, which Docker reads from the build context
     or another stage and never against this stage's WORKDIR. An instruction is refused
     once, on its strongest ground. The refusal stays a refusal either way; only the claim
     changes, and `test_a_refusal_prints_the_ground_it_measured` and
     `test_every_refusal_states_only_what_it_measured` measure both directions.

     Only a PATH-LIKE word is ever placed: one with a `/` in it and no leading `-`, the
     directory `.` itself, the `=value` of a `--flag=value` when that value is one of those,
     or a word that spells one of the two NAMES. A bare word is otherwise a command
     (`chmod`), a flag (`-m`) or a symbolic mode (`+x`, `u+s`), and counting one as a
     relative path is what refused `chmod -x /usr/local/bin/tool` - F1 of the third review.
     The stated costs of that, pinned as ACCEPTED in
     `test_the_declared_non_claims_are_still_accepted`: under `WORKDIR
     /opt/aitelier-gate-imports`, a bare FILE name (`chmod 0700 __init__.py`) and a bare
     glob (`chmod 0700 *`) name nothing this file can place, and a short flag with its
     value glued on (`install -t/opt/aitelier-gate-imports/docker` is refused for the name,
     `-t/opt` is not) is not split - only `--flag=value` is. `.` and `./` are NOT a cost:
     both are placed, and refused when they land on or above the tree.

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
middle of a name (a backtick pair, a comment line inside a continuation) - and it
measured this file refusing a `sed` expression whose `|` was quoted. The eighth tried to
answer that by treating a quoted separator as ordinary text, and its review measured the
cost: the same rule makes a whole quoted COMMAND one word, so `sh -c "chmod 0700 /opt"`
and a `python -c` chmod slipped back to green, and an escaped quote closed and reopened
the quoting. The split is quote-blind again, the `sed` refusal is a pinned cost, and the
assignment's value is judged too. The tenth found the two halves of one hole: a word FUSED by
a comma (`os.chmod('/opt',0o700)` was one word whose comma never reached an edge), and a
variable assigned a PREFIX of the tree and completed by the expansion (`D=/op`, then
`${D}t`). So a comma separates words, and a word is judged after the file's own assignments
are filled into the places they expand. What remains is not one function: `_split_keyword`
splits the keyword on any whitespace, `_directives` joins continuations and drops
comment/blank lines, `_words` reads the exec form and an assignment's value,
`_split_words` decides what separates a word, `_word` removes what a shell would remove,
and `_decides_reachability` normalises the leading slashes. The shapes below are rows of a
table rather than five more branches.

What it cannot decide, and does not claim to
--------------------------------------------
  * an alias that does not spell the name: a symlink, a bind mount, `COPY --from` of a
    tree built elsewhere, or a file in the build context that a RUN executes. Refusing
    every alias is not decidable from text - `chmod -x /usr/local/bin/tool` could be
    one - and refusing every instruction that names a path was measured as a FALSE
    REFUSAL the review called the fastest way to get a guard deleted. A `RUN` that names
    no pinned path is allowed to do whatever it does. The round-11 brief
    (iss-a5023139fee246f2) restated this as a defect - it lists
    `RUN chmod -x /usr/local/bin/tool`, `RUN install -m 0755 scripts/tool
    /usr/local/bin/tool` and `RUN chmod +x scripts/entrypoint.sh` as "refused, with a false
    ground" - and the measurement says otherwise: at 18638499bcc0 all three are ACCEPTED,
    with no refusal at all, because they are F1 and F2 of the third review and the operand
    model that refused them was removed for it.
    `test_the_three_instructions_the_round11_brief_calls_false_grounds_are_read` pins that
    measurement, so the premise cannot be re-litigated by the next reading;
  * a token that spells NO literal character of the path (`*`, `**/godot/*.py`): a glob
    with a literal head that already points at or into the tree is refused, so `/opt/ai*`,
    `/opt/*`, `/opt/aitelier-gate-{imports,x}` and `/*/..` are all caught, but a token
    that begins with the wildcard gives the text nothing to judge;
  * the value of a bare `$VAR`/`${VAR}` the file never assigns: the text carries no path
    for it, so `RUN chmod 0700 $D` is green. (An assignment's VALUE is judged, so
    `ENV D=/opt` - and the legacy `ENV D /opt` - is refused; a word that expands one of the
    file's own assignments is judged as the text it would become, so `RUN D=/op; chmod 0700
    ${D}t` is refused, and a CHAIN resolves because a value is expanded at the point it is
    assigned: `RUN D=/op; E=${D}t; chmod 0700 $E` stores `/opt` in `E` and is refused too.
    The one shape left out is a value naming a variable defined LATER in the file - a shell
    stores the empty string there and this file has no order to read it in, so
    `RUN E=${D}t; D=/op` leaves `E` as written; a value that arrives as a build argument is
    text this file never reads.) A cwd-relative spelling
    (`..` is `/` because the last `WORKDIR` is `/app`, and `~/../..` is `/` because HOME
    is `/home/app`), the RESULT of a command substitution (`$(dirname /opt/x)` is three
    words, none of them the directory it prints), and an alias path such as
    `/proc/self/root`, which IS `/`. The FIRST of those stays out of reach even though
    operands are now placed against the stage `WORKDIR`: `_placed_path` refuses to place a
    word carrying a `..` component, because the kernel resolves one AFTER following a
    symlink (`/var/run/../opt` is `/run/../opt` under a symlinked `/var/run`), so `..` is
    the one relative spelling the text does not settle. (An ABSOLUTE `..` word whose text
    fold lands on or above the tree, `/opt/x/..`, or any `..` word that spells a name, is
    still refused - `UNPLACED`, saying the `..` is why.) None of them is decidable from the
    text without running a shell, and running one is what five rounds proved is the wrong
    trade;
  * the WORKDIR an EXTERNAL base image brings. Every `FROM` starts its stage at its base's
    working directory: when the base is an earlier stage of this file, that stage's WORKDIR
    is tracked and inherited; when it is an image, its WORKDIR is not in this file, so the
    stage starts UNKNOWN. What that leaves unplaced is a relative word in such a stage
    before its own `WORKDIR` - `FROM some/image` whose WORKDIR is the tree, then `RUN chmod
    0700 docker/godot/__init__.py`, is read. A relative word that spells a NAME there is
    still refused, saying the stage has no WORKDIR this file can read;
  * a bash ANSI-C quoted slash after an explicit `SHELL ["/bin/bash","-c"]`:
    `$'\\x2fopt'` reduces to `$x2fopt`, which spells no path this file can judge. The
    brace list that used to sit next to it (`{/opt,/opt}` reduced to a brace-free blob) is
    now REFUSED, because a comma separates words - see `_SHELL_BREAK`;
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
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
EXPORT = "/opt/aitelier-gate-imports"
TARGET = "/opt/aitelier-gate-imports/docker/godot/godot_harness.py"


#: One `--chmod=` flag and its value, as Docker's COPY carries it. The value stops at the
#: first whitespace, so `--chmod=0644` on the shipped line is the value `0644`.
_MODE_FLAG = re.compile(r"--chmod=(\S*)")


def _is_pinned_mode(mode: str) -> bool:
    """True when `mode` is octal 0644 - the only mode the client may be baked with.

    Leading zeros do not change an octal value, so `0644`, `644` and `00644` are the same
    mode and all three are read. `0755`, `0600` and `6440` are different modes, and so is a
    value this file cannot read as a mode at all. Nothing else is modelled: this is one value
    compared with one value, not an octal parser.
    """
    return bool(re.fullmatch(r"0*644", mode))


def _mode_pin_problems(text: str) -> list[str]:
    """Why the COPY that bakes the client does not pin its mode to 0644, or [] when it does.

    This comparison is the WHOLE pin, which is why it has to be exact. The image bakes the
    client's mode from this line and from nothing else - a `chmod` of the client in a RUN is
    refused outright, and `test_the_import_tree_is_searchable_and_not_only_the_file` rests
    on that, so a mode this file reads but the image does not use has no second line to fall
    back on. It read ANY four-digit octal until the round-9 review measured
    `--chmod=0755 ... godot_harness.py` green (iss-363a4758f4974b31), and 0755 bakes an
    executable client while 0777 bakes a writable one.

    The spelling read is the one the shipped COPY uses, `--chmod=0644`; Docker's flag parser
    also accepts `--chmod 0644` with a space, which this file does not read. That is a stated
    cost and not a new one - the assertion this replaces did not read it either - and it is
    pinned as a refusal by `test_the_client_copy_refuses_every_other_mode`, so a build that
    wants the space spelling changes this file deliberately.

    A line carrying MORE than one `--chmod=` must pin every one of them to 0644: two flags
    are a spelling this file cannot resolve (which one the image ends up with is a detail of
    the builder), so it refuses rather than picking the one that reads well.
    """
    copies = [line for line in text.splitlines()
              if line.strip().upper().startswith("COPY") and TARGET in line]
    if not copies:
        return [f"no COPY bakes {TARGET}; the gate client must come from the image"]
    problems: list[str] = []
    for line in copies:
        modes = _MODE_FLAG.findall(line)
        if not modes:
            spaced = re.search(r"(?:^|\s)(--chmod\s+\S+)", line)
            if spaced:
                problems.append(
                    f"this COPY spells the mode flag as {spaced.group(1)!r}, which this file "
                    "does not read - what it reads is one token, --chmod=<octal>:\n  " + line
                    + "\nPin it with --chmod=0644 - that is the spelling the image uses, and "
                    "the backend runs as uid 1000 and cannot read 0600.")
            else:
                problems.append(
                    "this COPY carries no --chmod=, so it bakes the build host's mode "
                    "(0600 root:root on this host):\n  " + line + "\n"
                    "Pin it with --chmod=0644: the backend runs as uid 1000 and cannot read "
                    "0600.")
            continue
        for mode in modes:
            if _is_pinned_mode(mode):
                continue
            problems.append(
                f"this COPY bakes the client with --chmod={mode}, not --chmod=0644:\n  "
                + line + "\n"
                "The client's mode is 0644 and nothing else: 0755 makes it executable, 0777 "
                "makes it writable, and no `chmod` of it elsewhere is allowed to correct this "
                "- that line IS the pin. Change it deliberately and say why in the commit "
                "message.")
    return problems


def test_the_baked_gate_client_pins_its_mode():
    problems = _mode_pin_problems((ROOT / "Dockerfile").read_text())
    assert not problems, "\n".join(problems)


#: The spellings of octal 0644 this file must READ, measured on a mutated COPY of the
#: Dockerfile text. This column is the CONTROL: an assertion that refused every mode would
#: pass the refusing column below and be worthless. `00644` is here because leading zeros do
#: not change an octal value - it is the same mode as `0644`, not a neighbour of it.
CLIENT_MODES_READ: tuple[str, ...] = ("0644", "644", "00644")

#: The modes the shipped COPY must be REFUSED with, and the reason each one is a real bug
#: rather than a spelling this file dislikes. 0755 is the round-9 review's measured escape
#: (iss-363a4758f4974b31); 0777 is the same hole one step wider; 0600 is the mode the image
#: actually baked before this COPY pinned one, which the module docstring measures.
CLIENT_MODES_REFUSED: tuple[tuple[str, str], ...] = (
    ("0755 an executable gate client, baked by a line the old assertion read", "0755"),
    ("0777 a client any user in the image can write", "0777"),
    ("0600 the mode this image used to bake, unreadable to uid 1000", "0600"),
)

#: The other three ways the SAME line stops pinning 0644, each measured as a text mutation
#: `(name, shipped fragment, mutated fragment, token the refusal must name)`. Every branch
#: `_mode_pin_problems` has appears here: the absent flag is the bug this file exists for, the
#: space-spelled flag is the cost its docstring states, and two flags on one line is a mode
#: the builder picks and this file will not. Leaving one of them prose-only would be a
#: boundary the suite never measures, which is the failure mode the docstring calls a silence.
CLIENT_COPY_SHAPES_REFUSED: tuple[tuple[str, str, str, str], ...] = (
    ("the flag is absent, so the build host's 0600 is baked",
     "COPY --chmod=0644 docker", "COPY docker", "no --chmod="),
    ("the flag is spelled with a space, the form Docker's parser also accepts",
     "--chmod=0644", "--chmod 0644", "'--chmod 0644'"),
    ("a second flag decides the mode by builder detail",
     "COPY --chmod=0644 docker", "COPY --chmod=0644 --chmod=0755 docker", "--chmod=0755"),
)


def _client_copy_with_mode(text: str, mode: str) -> str:
    """`text` with the client's COPY re-moded to `mode` - a MUTATED COPY of the Dockerfile.

    The line is found by the same test the guard uses (a COPY naming the client) and only the
    digits after its `--chmod=` change. Nothing on disk is edited and nothing is built: like
    `_added_problems`, which mutates the same text by APPENDING an instruction, this mutates
    it by REPLACING one line's mode, because a mode is a property of the pinned line rather
    than a line of its own. That is what lets the suite measure the refusal of 0755 without a
    rebuild, and it is the same in-process mechanism - not a second runner.
    """
    lines = text.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines)
            if line.strip().upper().startswith("COPY") and TARGET in line]
    assert len(hits) == 1, (
        f"expected exactly one COPY of the client to re-mode, found {len(hits)}")
    index = hits[0]
    assert _MODE_FLAG.search(lines[index]), (
        f"the client's COPY carries no --chmod= to re-mode, so this case would measure "
        f"nothing:\n  {lines[index]}")
    lines[index] = _MODE_FLAG.sub(f"--chmod={mode}", lines[index], count=1)
    return "".join(lines)


def test_the_client_copy_refuses_every_other_mode():
    """The mode pin is EXACT, measured in both directions on a mutated copy of the text.

    The round-9 review of this guard (iss-363a4758f4974b31) measured the hole: the assertion
    accepted `--chmod=0?6?4?4|--chmod=0[0-7]{3}`, so `--chmod=0755 ... godot_harness.py`
    satisfied the file pin. That matters beyond the line itself, because the rest of this file
    rests on the claim that the client's mode comes SOLELY from this COPY: no RUN may `chmod`
    the client, so a mode accepted here is the mode the image bakes. 0755 bakes an executable
    gate client; 0777 bakes one any user in the image can overwrite, which is a gate whose
    measurements another process can rewrite.

    Every row runs `_mode_pin_problems`, the function the real assertion runs, on the shipped
    text with that one line re-moded - never the file, never the image. The READ column is the
    control in the other direction: an assertion tightened to "no --chmod satisfies it" would
    pass every refused row here, and the three accepted spellings are what rules that out. The
    last loop is the same check's other three branches - an absent flag, the space-spelled flag
    Docker also accepts, and a second flag - each of which used to be prose in the helper.
    """
    shipped = (ROOT / "Dockerfile").read_text()
    assert not _mode_pin_problems(shipped), (
        "the CONTROL is red - the shipped COPY does not pin the mode - so every case below "
        "would measure a refusal that is already there for another reason:\n"
        + "\n".join(_mode_pin_problems(shipped)))

    for mode in CLIENT_MODES_READ:
        problems = _mode_pin_problems(_client_copy_with_mode(shipped, mode))
        assert not problems, (
            f"--chmod={mode} IS 0644 and the pin must read it, but it refuses:\n"
            + "\n".join(problems))

    for name, mode in CLIENT_MODES_REFUSED:
        problems = _mode_pin_problems(_client_copy_with_mode(shipped, mode))
        assert problems, (
            f"this COPY re-modes the client to {mode} and the pin is still green - that is "
            f"the round-9 hole (iss-363a4758f4974b31): {name}")
        assert _names(problems, f"--chmod={mode}"), (
            f"the refusal does not name the mode it refused (expected '--chmod={mode}'): {name}\n"
            + "\n".join(problems))

    # The three branches besides a wrong mode: no flag at all (the bug this file exists for),
    # the space-spelled flag (the cost `_mode_pin_problems` states) and a second flag. Each is
    # a one-fragment mutation of the shipped line, so none of them touches the file.
    for name, shipped_part, mutated_part, expected in CLIENT_COPY_SHAPES_REFUSED:
        assert shipped_part in shipped, (
            f"the shipped Dockerfile no longer carries {shipped_part!r}, so this case would "
            f"measure nothing: {name}")
        mutated = shipped.replace(shipped_part, mutated_part)
        assert mutated != shipped, f"this case does not change the text: {name}"
        problems = _mode_pin_problems(mutated)
        assert problems, (
            f"this COPY is not pinned to 0644 and the check reads it anyway - {name}:\n"
            + "\n".join(line for line in mutated.splitlines() if TARGET in line))
        assert _names(problems, expected), (
            f"the refusal does not name what closes this case (expected {expected!r}): {name}\n"
            + "\n".join(problems))


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


def _normalised(path: str) -> str:
    """One path with its `.`/`..` folded and its leading slashes collapsed to one.

    `//opt` is `/opt` on Linux but POSIX lets `normpath` keep a leading double slash, so
    the collapse is explicit rather than implied.
    """
    collapsed = posixpath.normpath(path) if path else ""
    if collapsed.startswith("//"):
        collapsed = "/" + collapsed.lstrip("/")
    return collapsed


def _decides_reachability(path: str) -> bool:
    """True when re-moding `path` decides whether the export tree can be reached.

    `//opt` is `/opt` on Linux but POSIX lets `normpath` keep a leading double slash, so
    the leading slashes are collapsed before the comparison.
    """
    normalised = _normalised(path)
    if not normalised:
        return False
    if normalised in ANCESTORS or normalised == EXPORT:
        return True
    return EXPORT.startswith(normalised.rstrip("/") + "/")


def _inside_export(path: str) -> bool:
    """True when `path` IS the export tree or sits anywhere under it."""
    return path == EXPORT or path.startswith(EXPORT + "/")


def _has_parent_component(word: str) -> bool:
    """True when this word carries a `..` PATH COMPONENT (`a..b` is a name, not one).

    The kernel resolves `..` AFTER it follows a symlink, so `/var/run/../opt/...` is not
    `/var/opt/...` - where such a word lands is a fact about the filesystem, not about the
    text. That is why it is never PLACED here (see `_placed_path`), and why the cwd-relative
    spellings stay the declared non-claim they have always been.
    """
    return ".." in word.split("/")


def _is_path_like(word: str) -> bool:
    """True when a bare operand is read as a PATH: a `/` in it, or the directory `.` itself.

    Anything else without a `/` is a command (`chmod`), a symbolic mode (`+x`, `u+s`) or a
    file name, and a word with a leading `-` is a flag (its `=value`, if any, is judged on
    its own - see `_operand`).
    """
    return not word.startswith("-") and ("/" in word or word == ".")


def _placed_path(word: str, workdir: str | None, bare: bool = False) -> str | None:
    """The EXACT absolute path `word` denotes in a stage whose WORKDIR is `workdir`, or None.

    None is the honest answer whenever the text does not settle where the word lands, and
    every None below is a word this file either refuses on what it CAN measure (the name it
    spells, a literal head, a `..` fold) or reads because it names nothing pinned:

      * the word carries a `..` component, which the kernel resolves after following a
        symlink (`_has_parent_component`);
      * the shell supplies part of it - a `$`, a backtick, a glob or a leading `~` - so the
        path is not in the text at all; a glob's literal HEAD is judged by `_refusal`, which
        says the shell completes it and never prints the head as the path;
      * it is RELATIVE and this stage has no `WORKDIR` it can read: the base image's is not
        in this file, so the text does not say what the word is resolved against;
      * it is not PATH-LIKE (`_is_path_like`) unless the caller says the word is a path by
        construction (`bare`: a `WORKDIR` operand, which has nowhere else to be, and a word
        that spells one of the two NAMES, which no command and no flag does). A bare word is
        a command (`chmod`), a flag (`-m`) or a symbolic mode (`+x`, `u+s`), and counting
        one as a relative path is exactly what refused `chmod -x /usr/local/bin/tool` and
        `install -m 0755 scripts/tool /usr/local/bin/tool` - F1 and F2 of the third
        non-author review. A bare FILE name is therefore a stated cost of this rule, not an
        oversight: `WORKDIR /opt/aitelier-gate-imports` plus `RUN chmod 0700 __init__.py`
        names nothing this file can place, while the same WORKDIR plus
        `docker/godot/__init__.py`, `./` or `.` is refused.

    An ABSOLUTE word is never joined to the WORKDIR: it is its own placement. Where a
    relative word CAN be placed is the case the round-11 brief names: `WORKDIR
    /opt/aitelier-gate-imports` followed by `RUN chmod 0700
    docker/godot/godot_harness.py` re-modes the pinned client with a word that spells no
    part of its path, and `docker/godot/__init__.py` - which spells neither the tree's name
    nor the client's - is green without this join.
    """
    if not word or _build_time_path(word) is not None or word.startswith("~"):
        return None
    if _has_parent_component(word):
        return None
    if word.startswith("/"):
        return _normalised(word)
    if not bare and not _is_path_like(word):
        return None
    if workdir is None:
        return None
    return _normalised(posixpath.join(workdir, word))


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


#: What separates one WORD from the next in shell text. Quoting is deliberately NOT honoured
#: here: a `;` or a `|` inside quotes still separates, because what this file decides is
#: what the TEXT mentions, not what a shell would run (see `_split_words`, and the `sed`
#: row in `test_the_documented_false_refusal_is_refused_on_purpose`). The backtick is
#: deliberately absent: it stays inside its word. A COMMA separates for the same reason: it
#: is no part of a path, and without it `os.chmod('/opt',0o700)` is one word whose comma is
#: INTERIOR, so nothing on its edge is stripped and `'/opt',0o700` named no path at all -
#: the round-9 review measured that escape (`RUN python -c "import os;
#: os.chmod('/opt',0o700)"` was accepted, while the same command with a space after the
#: comma was refused). A comma is also what a bash brace list separates its members with,
#: which is why `{/opt,/opt}` is now refused rather than reduced to a brace-free blob.
_SHELL_BREAK = re.compile(r"[\s,;|&()<>]+")


def _split_words(text: str) -> list[str]:
    """The words of one instruction's shell text, each still as WRITTEN (quoting attached).

    One thing separates one word from the next: `\\s`, `,`, `;`, `|`, `&`, `(`, `)`, `<` or
    `>`, so a TAB after `RUN` separates exactly as a space does (an earlier space-only split
    made a whole instruction vanish), and a comma separates because it belongs to no path:
    without it, `os.chmod('/opt',0o700)` is one word whose only comma is interior, so the
    edge trimming of `_word` never reaches it and the word names nothing.

    Quoting does NOT protect a separator, and a BACKTICK is not a separator:

      * a quoted separator still separates, so `RUN sh -c "chmod 0700 /opt"` is five words
        and its `chmod` argument is judged like any other. Treating a quoted string as one
        word is what the round-8 candidate did, and its review measured four quoted
        commands ACCEPTED that the candidate's own base refused - a shell runs the string,
        so the words it would run are the words to judge. The price is one measured line
        shape, pinned: `sed -i 's|/opt|/srv|'` is refused, because its text names `/opt`
        and this file does not read a sed expression;
      * a BACKTICK stays inside its word, so `/opt/aitelier-gate-imp``orts` remains one
        word whose literal head stops at the backtick and `_refusal`'s expansion rule
        refuses it. Splitting on the backtick is what let the round-7 regression through:
        the two halves spelled no name and no ancestor;
      * quoting itself: `"/opt"` is one word, and what it names is decided after the
        quotes come off, in `_word`. No escape handling is modelled at all - `\\"` is a
        quote and a backslash, and `_word` removes both.

    Nothing more of quoting is modelled than that: no nested quoting, no `$'...'`, no
    arithmetic. Every word keeps whatever it spells and is judged as written.
    """
    return [word for word in _SHELL_BREAK.split(text) if word]


#: `NAME=value` as a shell assignment: an identifier on the left, so `--chmod=0644` and
#: `--from=build` (a leading `-`) are not one.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.+)$")

#: A `$NAME` or `${NAME}` expansion, so a word carrying one can be judged after the value
#: this file has already read is put where the shell would put it.
_EXPANSION = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

#: The NAME half of the legacy `ENV NAME VALUE` form. Docker exports it exactly like
#: `ENV NAME=VALUE`, so a path carried that way is no further from the shell than one
#: carried by `=`; a first word that is not an identifier is not that form.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _expand_with(text: str, values: dict[str, str]) -> str:
    """`text` with every `$NAME`/`${NAME}` this file has already read put in its place.

    ONE pass over ONE text, never a fixed point: `values` holds values that are already
    expanded (an assignment's value is expanded at the point it is assigned, below), so a
    replacement is never re-scanned and `A=$A` cannot loop. A name this file has not read is
    left as written - a shell would put the empty string there, and emptying it here would
    decide a word on evidence this file does not have.
    """
    if "$" not in text:
        return text
    return _EXPANSION.sub(
        lambda found: values.get(found.group(1) or found.group(2), found.group(0)), text)


def _with_assigned_values(
    words: list[str], known: dict[str, str], keyword: str = "",
) -> tuple[list[str], dict[str, str]]:
    """Each word, plus the value of its assignment and its expansion filled in.

    Three kinds of word are produced for one instruction, because all three name what a
    later line would use:

      * every word as written;
      * the VALUE of an assignment the word carries, judged EXPANDED: `ENV D=/opt` puts the
        export tree's ancestor into a variable in one word no shell would split, so the value
        is its own word;
      * every word with a `$NAME`/`${NAME}` that this file has ALREADY read replaced by that
        value, so `RUN D=/op; chmod 0700 ${D}t` is judged as the `/opt` a shell would build.
        The round-9 review measured that escape: the assignment's own value `/op` names
        nothing and `${D}t` carries no `/`, so neither word was refused while the shell
        re-moded the ancestor.

    A value is expanded at the point it is assigned, which is when a shell expands it, and
    that is what makes a chain resolve: `RUN D=/op; E=${D}t; chmod 0700 $E` stores `/opt` in
    `E`, so `$E` is judged as `/opt`. The tenth round stored the value as written and
    substituted once, and the round-10 review measured that line green
    (`evidence/guard-r11-repro-20261107.txt`). What this file does NOT do is expand a value
    that names a variable defined LATER in the file: a shell would store the empty string
    there, and this file has no order to read it in, so `RUN E=${D}t; D=/op` leaves `E` as
    written. `A=B=/opt` still yields `B=/opt`, because that text is what a shell puts in `A`.

    An `ENV` also has a LEGACY two-word form - `ENV D /opt`, `ENV LANG C.UTF-8` - which
    Docker still accepts and which a shell exports exactly like the `=` form: the first word
    is the name, the rest of the line is the value. `keyword` is what says an instruction is
    an `ENV`; the value is expanded the same way, because an instruction that carries a path
    is not less reachable for being spelled the old way.

    `known` carries the assignments of the instructions BEFORE this one, so an `ENV D=/op`
    is visible to the `RUN` that expands it, and the returned mapping adds this one's.
    """
    values = dict(known)
    produced: list[str] = []
    # The legacy `ENV NAME VALUE...` form first, so a later RUN sees the value: the name is
    # the first word and the value is everything after it, as a shell would export it.
    if keyword.upper() == "ENV" and len(words) > 1 and not _ASSIGNMENT.match(words[0]):
        name = _word(words[0])
        if _ENV_NAME.match(name):
            value = _expand_with(" ".join(words[1:]), values)
            values[name] = value
            produced.append(value)
    assignments: list[str | None] = [None] * len(words)
    for index, word in enumerate(words):
        match = _ASSIGNMENT.match(word)
        if match is None:
            continue
        # Expanded HERE, in word order, so `D=/op; E=${D}t` stores `/opt` in `E`. The word
        # BEFORE an assignment still sees it below, because this pass finishes first - that
        # is the round-9 shape (`chmod 0700 ${D}t` with no assignment in its own word) and
        # it must keep being refused.
        value = _expand_with(match.group(1), values)
        assignments[index] = value
        values[word.split("=", 1)[0]] = value
    for index, word in enumerate(words):
        produced.append(word)
        if assignments[index] is not None:
            produced.append(assignments[index])
            continue
        substituted = _expand_with(word, values)
        if substituted != word:
            produced.append(substituted)
    return produced, values


def _words(
    arguments: str, known: dict[str, str] | None = None, keyword: str = "",
) -> tuple[list[str], dict[str, str]]:
    """The words of one instruction, each still as WRITTEN (its quoting attached).

    Docker has two forms and they split differently. The exec form IS a JSON array -
    Docker reads it with a JSON reader, so this file does too, and
    `RUN ["chmod","0700","/opt"]` is three words rather than one bracket-wrapped blob
    that named nothing; the round-6 review measured that shape green. Everything else is
    shell text, split by `_split_words` on whitespace, a comma and on `;`, `|`, `&`, `(`,
    `)`, `<`, `>` whether or not they are quoted, with a backtick left inside its word. An
    exec-form element may itself be shell text (`SHELL ["/bin/sh","-c","true;chmod -R 0700
    /opt"]`), so each element is split again.

    The VALUE of an assignment is judged as its own word too, and so is a word with one of
    the file's own assignments expanded into it, so neither `ENV D=/opt` nor a later
    `chmod 0700 ${D}t` is carried out of this file's reach; see `_with_assigned_values`,
    which also returns the mapping so the next instruction sees it. A value is expanded where
    it is ASSIGNED, so a chain (`D=/op` then `E=${D}t`) is judged as the path it becomes, and
    the legacy two-word `ENV NAME VALUE` form is read as the same assignment.
    """
    return _with_assigned_values(_written_words(arguments), known or {}, keyword)


def _written_words(arguments: str) -> list[str]:
    """The words of one instruction as written, before any assignment is read into them."""
    text = arguments.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return [word for item in parsed for word in _split_words(item)]
    return _split_words(text)


def _copy_sources(arguments: str, known: dict[str, str]) -> set[str]:
    """The SOURCE words of a `COPY`/`ADD`, as written and as `_words` expands them.

    Every operand but the last is a source, and Docker reads a source from the build
    context (or from the stage or image its `--from` names) - never against this stage's
    WORKDIR. Joining one to the WORKDIR invents a path in this image that the instruction
    never touches (`WORKDIR /opt/aitelier-gate-imports` + `COPY docker/godot/__init__.py
    /tmp/x` reads the CONTEXT's file and writes `/tmp/x`). The destination is excluded
    even when a source is spelled the same way, because the destination is the word that
    decides where a mode lands.
    """
    operands = [word for word in _written_words(arguments)
                if _word(word) and not _word(word).startswith("-")]
    if len(operands) < 2:
        return set()
    destination = {operands[-1], _expand_with(operands[-1], known)}
    sources = {form for word in operands[:-1] for form in (word, _expand_with(word, known))}
    return sources - destination


def _stage_of(arguments: str) -> tuple[str, str | None]:
    """`FROM [--platform=...] <image> [AS <name>]` as (image, name), both lowercased.

    Docker matches a stage name case-insensitively, and a `FROM` that names an EARLIER
    stage starts from that stage's filesystem and its WORKDIR.
    """
    tokens = [token for token in arguments.split() if not token.startswith("--")]
    image = tokens[0].lower() if tokens else ""
    name = tokens[2].lower() if len(tokens) >= 3 and tokens[1].upper() == "AS" else None
    return image, name



def _word(raw: str) -> str:
    """The word a shell would pass on for this one: quoting and edge punctuation gone.

    `/"opt"`, `/o'pt'`, `\\/opt`, `"/opt"` and `/opt;` are all `/opt` to a shell, and the
    round-6 review measured three of those spellings GREEN while a chmod of them re-moded
    the ancestor. A word this reduces to nothing is not judged at all.
    """
    return raw.translate(_UNQUOTED).strip(_EDGE)


#: The two grounds a refusal may rest on. They are kept apart because a message that
#: claims the stronger one without measuring it teaches the next author to disbelieve the
#: guard: `NAMES` says the word's MEASURED placement IS a pinned path - the export tree, a
#: path inside it (the client among them) or an ancestor whose mode decides whether it can
#: be reached; `UNPLACED` says only that the word could reach one, which is all a name, a
#: literal head, a `..`, a `COPY` source or a shell-supplied path supports.
NAMES = "names a pinned path"
UNPLACED = "not placed on a pinned path"

#: The sentence every `UNPLACED` reason opens with, followed by what WAS measured.
COULD_REACH = "could reach a pinned path, but the text does not place it on one"

#: What a name-spelling word is refused for when its placement is not the tree.
_SPELLS_NAME = "it spells the name of the export tree or of the client"


class _Refusal(NamedTuple):
    """One refusal: the ground it rests on, and the reason printed for it."""

    ground: str
    reason: str


def _operand(word: str) -> tuple[str, str | None]:
    """(the part of `word` that can be a path, the flag it is attached to or None).

    An ASSIGNMENT is judged by its VALUE: `ENV D=/opt` puts an absolute path in a word
    whose own spelling starts with the variable's name, and joining `D=/opt` to a WORKDIR
    would invent a path (`<workdir>/D=/opt`) that is not in the text. A FLAG is judged by
    the value glued to it with `=` - `--target-directory=/opt/aitelier-gate-imports/docker`
    and `--reference=/opt/aitelier-gate-imports` carry a path, `--chmod=0644` and
    `--from=build` carry a word that is no path - and a flag with no `=` carries nothing
    this file places. A short flag with its value glued on (`-t/opt`) is not split: only
    the `--flag=value` spelling is read, which is a stated cost.
    """
    assignment = _ASSIGNMENT.match(word)
    if assignment:
        return assignment.group(1), None
    if word.startswith("-"):
        flag, equals, value = word.partition("=")
        return (value if equals else ""), flag
    return word, None


def _pinned(path: str) -> str | None:
    """What a MEASURED absolute placement is to the pinned tree, or None when it is nothing.

    This is the only place the `NAMES` ground is earned: the export tree, a path inside it
    (the pinned client is one), or an ancestor whose mode decides whether it is reachable.
    """
    if path == TARGET:
        return f"the pinned client {TARGET}"
    if path == EXPORT:
        return f"the export tree {EXPORT}"
    if _inside_export(path):
        return f"{path}, a path inside the export tree this image pins"
    if _decides_reachability(path):
        return (f"{path}, an ancestor of the export tree, whose mode decides whether it can "
                "be reached")
    return None


def _head_relation(head: str) -> str | None:
    """How a glob's placed literal HEAD points at the pinned tree, or None when it does not.

    The head may be the tree's own beginning rather than one of its directories:
    `/opt/aitelier-gate-import[[:alpha:]]` and `/opt/aitelier-gate-{imports,x}` both spell
    everything up to the first character the shell supplies.
    """
    if head == EXPORT:
        return "is the export tree itself"
    if _inside_export(head):
        return "lies inside the export tree"
    if _decides_reachability(head):
        return "is an ancestor of the export tree"
    if EXPORT.startswith(head):
        return f"is the start of the export tree's own path {EXPORT}"
    return None


def _refusal(word: str, workdir: str | None = None, source: bool = False) -> _Refusal | None:
    """Why this one WORD makes its instruction one this file cannot allow, or None.

    Every question is about the text of the word (`_word` has already taken the shell's
    quoting and word separators off it), and every answer says what was MEASURED. The word
    is first reduced to the part that can be a path (`_operand`: an assignment's value, a
    `--flag=`'s value), then sorted by KIND:

      * a `COPY`/`ADD` SOURCE (`source`) is read from the build context or another stage,
        never against this stage's WORKDIR, so it is never placed. It is refused - as
        `UNPLACED` - only when, read as a path of this image, the word would be refused
        anyway (it spells a name, `/`, `/opt`, a literal head into the tree);
      * a word the SHELL supplies part of - a `$`, a backtick, a leading `~` - has no path
        in the text: refused `UNPLACED` when it spells a name, or when it builds a PATH out
        of the expansion (`${DIR}/x`, `/opt/venv/bin:$PATH` - the stated costs); a bare
        `$TOOL` is read;
      * a word the shell COMPLETES - a glob or a brace - has only its literal HEAD in the
        text. The head is placed (an absolute head as written, a relative one against the
        WORKDIR) and the word is refused `UNPLACED` when the head points at, into or above
        the tree, with a sentence that says the shell completes it: `/opt/aitelier-gate-
        imports/*` is never reported as the path `/opt/aitelier-gate-imports`;
      * a word with a `..` component is not placed, because the kernel resolves `..` after
        following a symlink. It is refused `UNPLACED` when it spells a name, or when it is
        absolute and its TEXT fold lands on or above the tree (`/opt/x/..`); a relative
        `..` that spells no name is the declared cwd-relative non-claim;
      * anything else is placed EXACTLY (`_placed_path`): an absolute word as itself, a
        path-like relative word (`docker/x`, `./`, `.`) against this stage's WORKDIR. Only
        here is `NAMES` claimed, and only when the placement IS the tree, a path inside it
        or an ancestor (`_pinned`); the message then prints the path it resolved to.
        A word that spells the tree's or the client's NAME but places elsewhere
        (`/app/aitelier-gate-imports`, or `aitelier-gate-imports` under `WORKDIR /app`) is
        refused `UNPLACED`, saying where it resolved and that the place is not the tree:
        the name is refused wherever it appears, because the text cannot say whether that
        place is an alias of the tree (`/proc/self/root/opt/...` IS one; this file does not
        resolve aliases, so it does not claim it).

    Costs are stated rather than hidden, and every one is pinned by
    `test_the_documented_false_refusal_is_refused_on_purpose`: a word that STARTS with an
    expansion (`${SRC}/tool`) is refused because its whole path comes from the shell; a
    word whose literal head CONTAINS a `/` and which ALSO carries `$` or a backtick is
    refused because the literal part already points somewhere and the rest is spliced on
    (`ENV PATH="/opt/venv/bin:$PATH"`, `RUN curl https://host/v${VERSION}/tool.tgz -o
    /tmp/t`); and a QUOTED expression that mentions a pinned path is refused, so
    `RUN sed -i 's|/opt|/srv|' /etc/app.conf` fails although a shell would only rewrite
    text there. That is the price of judging text instead of running a shell, and the
    module docstring names it.
    """
    operand, flag = _operand(word)
    named = NAME in operand or CLIENT in operand
    if flag is not None and (NAME in flag or CLIENT in flag):
        return _Refusal(
            UNPLACED,
            f"{COULD_REACH}: it spells the name of the export tree or of the client in the "
            f"flag {flag!r} itself, which is not a path")
    if not operand:
        return None
    carried = f"passes {operand!r} as the value of its flag {flag!r}, and that value "
    if source:
        as_path = _refusal(operand)
        if as_path is None:
            return None
        if named:
            measured = _SPELLS_NAME
        elif as_path.ground == NAMES:
            measured = f"read as a path of this image, it {as_path.reason}"
        else:
            measured = ("read as a path of this image, "
                        + as_path.reason.removeprefix(f"{COULD_REACH}: "))
        return _Refusal(
            UNPLACED,
            f"{COULD_REACH}: it is a COPY/ADD source, which Docker reads from the build "
            "context (or from the stage its --from names), never against this stage's "
            f"WORKDIR, so its place in this image cannot be settled from the text - and "
            f"{measured}")
    head = _build_time_path(operand)
    if (head is not None and ("$" in operand or "`" in operand)) or operand.startswith("~"):
        if named:
            return _Refusal(
                UNPLACED,
                f"{COULD_REACH}: {_SPELLS_NAME}, and the shell supplies part of it (a `$`, a "
                "backtick or a leading `~`), so the path it builds is not in the text")
        # The literal head is read on the WHOLE word, as written, exactly as before flags
        # and assignments were split: an assignment's value is judged again EXPANDED, as its
        # own word, and any word the file's own assignments fill is judged again filled
        # (`_with_assigned_values`). So `E=${D}/data` and `--prefix=${P}/lib` are not refused
        # for a `/` the expansion may supply, while `PATH=/opt/venv/bin:$PATH` is - its
        # literal head already carries a `/`.
        written = _build_time_path(word) or ""
        if "/" in written or (not written and "/" in word and not word.startswith("~")):
            return _Refusal(
                UNPLACED,
                f"{COULD_REACH}: it builds a path out of a shell variable, so the path is not "
                "in the text")
        return None
    if head is not None:
        placed_head = None
        if head.startswith("/") and not _has_parent_component(head):
            placed_head = _normalised(head)
        elif head and _is_path_like(head) and not _has_parent_component(head) and workdir:
            placed_head = _normalised(posixpath.join(workdir, head))
        relation = _head_relation(placed_head) if placed_head else None
        where = ("" if placed_head is None or head.startswith("/")
                 else f" ({placed_head} against the stage WORKDIR {workdir})")
        if relation:
            return _Refusal(
                UNPLACED,
                f"{COULD_REACH}: the shell completes it from its literal head {head!r}{where}, "
                f"which {relation}, so the exact path is not in the text")
        if named:
            return _Refusal(
                UNPLACED,
                f"{COULD_REACH}: {_SPELLS_NAME}, and the shell completes it from its literal "
                f"head {head!r}{where}, so the exact path is not in the text")
        return None
    if _has_parent_component(operand):
        fold = _normalised(operand) if operand.startswith("/") else None
        folded = _pinned(fold) if fold else None
        if not named and not folded:
            return None
        return _Refusal(
            UNPLACED,
            f"{COULD_REACH}: " + (f"{_SPELLS_NAME}, and " if named else "")
            + "it carries a `..` component, which the kernel resolves after it follows a "
            "symlink, so where it lands is not decidable from the text"
            + (f" (folded as text, ignoring symlinks, it would read {fold})" if folded else ""))
    placed = _placed_path(operand, workdir, bare=named)
    pinned = _pinned(placed) if placed is not None else None
    if pinned:
        if operand.startswith("/"):
            # An absolute word is its own placement; it is never joined to the WORKDIR.
            what = (f"names {pinned}" if _inside_export(placed) else
                    "names an ancestor of the export tree, whose mode decides whether it can "
                    "be reached" + (f" - {operand!r} is {placed}" if placed != operand else ""))
            return _Refusal(NAMES, (carried + what) if flag else what)
        landed = (f"is {operand!r} as written and {placed} in this stage: placed against the "
                  f"stage WORKDIR {workdir}, it lands on {pinned}")
        return _Refusal(NAMES, (carried + landed) if flag else landed)
    if not named:
        return None
    if placed is not None:
        where = (f"it is the absolute path {placed}" if operand.startswith("/")
                 else f"against the stage WORKDIR {workdir} it becomes {placed}")
        return _Refusal(
            UNPLACED,
            f"{COULD_REACH}: {_SPELLS_NAME}, but {where}, which is not {EXPORT}, a path under "
            "it or one of its ancestors - the name is refused wherever it appears, because "
            f"the text cannot say whether {placed} is an alias of the tree")
    return _Refusal(
        UNPLACED,
        f"{COULD_REACH}: {_SPELLS_NAME}, and it is relative while this stage has no WORKDIR "
        "this file can read (the base image's is not in this file), so the text does not say "
        "what it resolves against")


def _next_workdir(words: list[str], workdir: str | None) -> str | None:
    """The stage WORKDIR after one `WORKDIR` instruction, or None when it cannot be placed.

    A relative `WORKDIR` is resolved against the one before it, as Docker does, and a
    `WORKDIR` this file cannot place leaves the stage's working directory UNKNOWN rather
    than guessing: a later operand that spells a name is then refused as `UNPLACED`, saying
    the stage has no WORKDIR this file can read, which is the honest ground.
    """
    operands = [word for word in words if _word(word)]
    if not operands:
        return None
    # `bare=True`: a `WORKDIR` operand is a directory by construction, so `WORKDIR app`
    # places at `<previous>/app` even though a bare word is never treated as a path operand.
    return _placed_path(_word(operands[0]), workdir, bare=True)


def _problems(text: str) -> list[str]:
    """Every way this Dockerfile departs from the instruction set this file expects."""
    problems: list[str] = []
    found: list[str] = []
    known: dict[str, str] = {}
    workdir: str | None = None
    #: The WORKDIR each NAMED stage has reached, so a later `FROM <that stage>` starts there.
    stage_workdirs: dict[str, str | None] = {}
    stage: str | None = None
    for keyword, arguments in _directives(text):
        canonical = _canonical(keyword, arguments)
        upper = keyword.upper()
        sources = _copy_sources(arguments, known) if upper in ("COPY", "ADD") else set()
        words, known = _words(arguments, known, keyword)
        # The instruction's own words are judged against the WORKDIR BEFORE it - a relative
        # `WORKDIR` is resolved against the previous one - and the stage keeps what the
        # author wrote whether or not this file refuses the instruction: what the next RUN
        # resolves against is his text, not this file's opinion of it.
        previous = workdir
        if upper == "WORKDIR":
            workdir = _next_workdir(words, workdir)
        elif upper == "FROM":
            # Every stage starts at its BASE's working directory. A base that is an EARLIER
            # stage of this file brings the WORKDIR that stage reached, which is tracked; an
            # external image brings one this file's text does not carry, so the stage starts
            # where nothing relative can be placed (None) and no operand is ever resolved
            # against the PREVIOUS stage. What that leaves unplaced is stated in the module
            # docstring: a relative word in a stage built on an external image that sets a
            # WORKDIR of its own.
            image, stage = _stage_of(arguments)
            previous = None
            workdir = stage_workdirs.get(image)
        if stage is not None:
            stage_workdirs[stage] = workdir
        if canonical in EXPECTED:
            found.append(canonical)
            continue
        # The KEYWORD is not an operand: placing it against the stage's WORKDIR would say
        # `RUN` resolves to `<workdir>/RUN` and, when the WORKDIR is the tree, refuse an
        # instruction for a ground about its own name rather than about what it does.
        # One refusal per instruction, on its STRONGEST ground: the first word whose
        # placement IS a pinned path, else the first word that could reach one - so
        # `COPY --from=build / /` is reported on the destination `/` it writes, not on the
        # source `/` it reads from another stage.
        chosen: tuple[str, str, _Refusal] | None = None
        for index, raw in enumerate([keyword, *words]):
            word = _word(raw)
            refusal = _refusal(word, None if index == 0 else previous,
                               source=index > 0 and raw in sources)
            if refusal and (chosen is None or refusal.ground == NAMES):
                chosen = (raw, word, refusal)
                if refusal.ground == NAMES:
                    break
        if chosen:
            raw, word, refusal = chosen
            written = "" if word == raw else f" (a shell word: {word!r})"
            shown = canonical if len(canonical) <= 160 else canonical[:157] + "..."
            problems.append(
                f"{upper} carries {raw!r}{written}, which {refusal.reason}. "
                "This file reads exactly two instructions:\n  " + "\n  ".join(EXPECTED)
                + "\nand refuses every other instruction whose operands name a pinned "
                "path, or could reach one that the text cannot place. Got:\n  "
                f"{shown}\n"
                "If one of the two really changed, change EXPECTED here deliberately and "
                "say why in the commit message - do not teach this file to read a shell: "
                "six review rounds each found the next spelling.")
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
    # Refused on the FIRST brace-list member: a comma separates words (round 10), so the
    # token this row is closed on is `/opt/aitelier-gate-{imports` rather than the whole
    # list. The list was already refused before that change, on its literal head.
    ("E4  bash brace expansion, armed by a SHELL override",
     'SHELL ["/bin/bash","-c"]\n'
     "RUN chmod -R 0700 /opt/aitelier-gate-{imports,x}",
     "/opt/aitelier-gate-{imports"),
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

#: The escapes the EIGHTH review measured against the round-8 candidate (6237d9a6). X1, X1b,
#: X1c and X2 were REFUSED by the branch's base (ee02377d2b) and accepted by the candidate:
#: its quote-aware splitter kept a whole quoted COMMAND as one word, so the argument that
#: re-modes an ancestor was never a word of its own, and an escaped quote closed and reopened
#: the quoting, so the `;` after it disappeared into the same word. X3 is the control that was
#: refused throughout. The expected token is what the refusal has to NAME on its first line.
EIGHTH_REVIEW_ESCAPES: tuple[tuple[str, str, str], ...] = (
    ("X1  a quoted shell command that re-modes an ancestor",
     'RUN sh -c "chmod 0700 /opt"', "/opt"),
    ("X1b a quoted recursive chmod of the root",
     "RUN sh -c 'chmod -R 0700 /'", "names an ancestor"),
    ("X1c a python one-liner that re-modes an ancestor from inside quotes",
     "RUN python -c \"import os; os.chmod('/opt', 0o700)\"", "/opt"),
    ("X2  an escaped quote that closes and reopens the quoting",
     "RUN echo \"a\\\"\" ; chmod 0700 /opt", "/opt"),
    ("X3  the same chmod with no quoting at all (the control that never went green)",
     "RUN chmod 0700 /opt", "/opt"),
)

#: The `sed` spellings the seventh review measured as false refusals, re-measured after the
#: round-9 split. The `|` one is REFUSED: a shell operator separates words whether or not it
#: is quoted, so the expression's `/opt` IS a word and the line's text names a pinned path.
#: The `#` and escaped-slash ones are ACCEPTED, because neither character separates words and
#: the word they form spells no path. Both directions are pinned so neither can drift quietly.
SED_SPELLINGS_REFUSED: tuple[str, ...] = (
    "RUN sed -i 's|/opt|/srv|' /etc/app.conf",
)
SED_SPELLINGS_READ: tuple[str, ...] = (
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


def test_the_eighth_review_escapes_are_refused():
    """The quoted-command spellings the round-8 candidate let back in.

    The round-8 splitter treated a separator inside quotes as ordinary text, which reads a
    quoted COMMAND as one opaque word: `sh -c "chmod 0700 /opt"` then spelled no ancestor,
    and a `python -c` chmod did the same. It also had no escape handling inside quotes, so
    `"a\\""` closed at the escaped quote and reopened on the next one, swallowing the `;` and
    the chmod after it. All four were refused by the branch's base and accepted by the
    candidate; the base's reading is the one this file keeps, because a shell runs the
    string and the words it would run are the words to judge.

    X3 is the control: the same chmod with no quoting, refused before the round-8 change and
    after it, so the table can tell "the quoted form is refused" from "this chmod is refused".
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, expected in EIGHTH_REVIEW_ESCAPES:
        added = _added_problems(shipped, extra)
        assert added, f"this shape is green again, and it re-modes a pinned path:\n{extra}"
        assert _names(added, expected), (
            f"{name} was refused, but not for the token that closes it (expected "
            f"{expected!r}):\n" + "\n".join(added))


def test_the_seventh_review_stated_costs_are_refused_on_purpose():
    """The cost the seventh review named as BY DESIGN BUT UNSTATED.

    `WORKDIR /`, `RUN mkdir -p /opt`, `COPY --from=build / /` and `RUN find / -name '*.pyc'
    -delete` are real builds this file will not read, because the word IS an ancestor and no
    text tells a harmless mention from a mode change. Pinning them makes that a stated
    decision with a test instead of a silence, and the reason is asserted too: a refusal
    that names the ancestor is the contract, not a stray match.

    This test used to hold the `sed` spellings in the OTHER direction, on the round-8
    reading that a quoted expression is one word. Round 8's review measured what that
    reading costs, so the `sed` rows moved to
    `test_the_documented_false_refusal_is_refused_on_purpose`, where one spelling is a
    stated refusal and the other two are stated reads.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
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
        # Refused by the round-8 candidate and accepted by its base: a quoted command whose
        # `$` is not part of a path. The round-8 `$` rule read the head of the whole quoted
        # word, so `cd /app && echo ` looked like the literal part of a path.
        'RUN sh -c "cd /app && echo $HOME"',
    ):
        assert not _added_problems(shipped, line), (
            f"this line names no pinned path and must not be refused: {line}")


def test_the_documented_false_refusal_is_refused_on_purpose():
    """Every cost the module docstring states, pinned in both directions.

    Four refusals are the price of judging TEXT instead of running a shell, and each is paid
    on a line a build could legitimately want:

      * a word that STARTS with an expansion (`${SRC}/tool`) has an empty literal head, so
        the text says nothing about where the shell will point it;
      * a word whose literal head contains a `/` and which also carries `$` or a backtick is
        refused, because the literal part already points somewhere and the rest is spliced
        on. `ENV PATH="/opt/venv/bin:$PATH"` and `curl https://host/v${VERSION}/tool.tgz`
        are legitimate lines this file will not read;
      * a quoted expression whose text names a pinned path is refused, so
        `sed -i 's|/opt|/srv|'` fails although a shell would only rewrite text there. The
        two spellings that carry no shell operator, `s#/opt#/srv#` and `s/\\/opt/\\/srv/`,
        are READ - not because they are safer, but because this file splits words on the
        shell's operators and `#` is not one of them. That asymmetry is the measured
        boundary, not a preference;
      * a word that IS `/` or `/opt` is refused outright, pinned in
        `test_the_seventh_review_stated_costs_are_refused_on_purpose`.

    The last rows are the other side of the same rules: a bare expansion with no path in it
    names a command or an operand and IS read, and so does a `sed` expression this file's
    word rule does not split, so the rule is about paths and not about `$` or `sed`.
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
    for line in ('ENV PATH="/opt/venv/bin:$PATH"',
                 "RUN curl https://host/v${VERSION}/tool.tgz -o /tmp/t"):
        added = _added_problems(shipped, line)
        assert added, (
            f"the second documented cost is gone, and the docstring still states it: {line}")
        assert _names(added, "builds a path out of a shell variable"), (
            f"{line} was refused for another reason than the docstring states:\n"
            + "\n".join(added))
    for line in SED_SPELLINGS_REFUSED:
        added = _added_problems(shipped, line)
        assert added, f"the sed cost is documented as a refusal and is now read: {line}"
        assert _names(added, "names an ancestor"), (
            f"{line} was refused for another reason than the docstring states:\n"
            + "\n".join(added))
    for line in ("RUN ${BUILD_TOOL} --version", "RUN chmod 0755 ${MODE}", *SED_SPELLINGS_READ):
        assert not _added_problems(shipped, line), (
            f"this line spells no path and must not be refused: {line}")


def test_a_command_that_is_not_chmod_and_names_the_tree_is_refused():
    """The shapes the mention rule exists for, beyond the reviewer's `ln -s`.

    `chmod` follows a symlink, `cp -p` carries the SOURCE's mode onto the destination,
    a `python -c` one-liner names the tree inside a string, a write into the tree from a
    source outside it, and `${GODOT_DIR}/..` is a path the text does not contain at all -
    none of them is a `chmod` operand this file reads, and every one re-modes or replaces
    a pinned path. The old rule needed a command word for each of them; the mention rule
    needs none, which is why it holds for a command this file has never seen.

    The first row is reported on its DESTINATION, the pinned client itself: an instruction
    is refused once, on its strongest ground, and a word whose placement IS the client
    outranks a relative SOURCE word that only spells its name. The second row keeps the
    other half of the contract measured - naming the client is refused wherever the name
    appears, so the same source with a destination outside the tree is still refused on
    `godot_harness.py`. Both rows expect the file name in the FIRST line rather than the
    path printed under it - before `_names` read only the first line, this row was green
    for the pinned path quoted in the message tail, which is exactly the vacuity the
    round-6 review measured.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line, expected in (
        ("RUN cp -p docker/godot/godot_harness.py "
         "/opt/aitelier-gate-imports/docker/godot/godot_harness.py",
         "godot_harness.py"),
        ("RUN cp -p docker/godot/godot_harness.py /tmp/client.py", "godot_harness.py"),
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
    ("E1  an alias that does not spell the name (a symlink, a bind mount, a COPY --from)",
     "RUN chmod -x /usr/local/bin/tool"),
    ("E2  a token that spells no literal character of the path",
     "RUN chmod 0700 *"),
    ("E3  a path carried in a bare variable the text never assigns",
     "RUN chmod 0700 $D"),
    ("E4  a cwd-relative spelling of an ancestor",
     "RUN chmod 0700 ..\nRUN chmod 0700 ../opt\nRUN chmod 0700 ~/../.."),
    ("E5  the RESULT of a command substitution",
     "RUN chmod 0700 $(dirname /opt/x)"),
    ("E7  an alias path that IS `/` without spelling it",
     "RUN chmod 0700 /proc/self/root"),
    ("E6  a bash ANSI-C quoted slash after an explicit SHELL",
     'SHELL ["/bin/bash","-c"]\nRUN chmod 0700 $\'\\x2fopt\''),
)


#: The placement costs the module docstring states, each measured in the stage CONTEXT that
#: makes it a cost: `(name, context appended to the shipped text first, the line)`. The
#: context is part of the control, so a `WORKDIR` on the tree - refused on its own line - does
#: not make every row red for a reason that is not the row's.
PLACEMENT_COSTS: tuple[tuple[str, str, str], ...] = (
    ("a bare FILE name under a WORKDIR on the tree",
     f"WORKDIR {EXPORT}", "RUN chmod 0700 __init__.py"),
    ("a bare glob under a WORKDIR on the tree",
     f"WORKDIR {EXPORT}", "RUN chmod 0700 *"),
    ("a short flag with its value glued on",
     "", "RUN install -d -t/opt /tmp/x"),
    ("a relative word in a stage built on an EXTERNAL image, whose WORKDIR is not in the text",
     "FROM python:3.12-slim", "RUN chmod 0700 docker/godot/__init__.py"),
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
    # The ASSIGNMENT half of E3 was closed by the round-8 review's own finding: an
    # instruction that puts a pinned path into a variable is refused on the VALUE, so what
    # is left of the non-claim is a variable whose value the text never carries at all.
    # The placement costs, each in the context that makes it one.
    for name, context, line in PLACEMENT_COSTS:
        text = f"{shipped}\n{context}" if context else shipped
        added = _added_problems(text, line)
        assert not added, (
            f"{name} is a declared COST of placing operands and must still be accepted - if "
            f"closing it is intended, move the row and say why in the commit message:\n"
            f"{context}\n{line}\n" + "\n".join(added))
    for line in ("ENV D=/opt", "ARG D=/opt", "RUN D=/opt; chmod 0700 $D"):
        added = _added_problems(shipped, line)
        assert added, (
            f"this line does put a pinned path into a variable and must be refused: {line}")
        assert _names(added, "/opt"), (
            f"{line} was refused, but not for the path it assigns:\n" + "\n".join(added))


def test_the_tenth_review_escapes_are_refused():
    """The two shapes the round-9 review measured green, and the non-claim it closed.

    The review found that the quoted-command class this file claims to have closed still had
    a hole, twice over:

      * a quoted `os.chmod('/opt',0o700)` with NO space after the comma. The comma stayed
        inside one word (`'/opt',0o700`), the edge trimming in `_word` only reaches the ENDS
        of a word, and so the word named nothing at all - the same command WITH a space was
        already refused. `_SHELL_BREAK` separates on a comma now, because no path contains
        one and a comma is not a character a shell keeps in a path;
      * an assignment of a PREFIX of the tree followed by an expansion: `D=/op` names
        nothing and `${D}t` carries no `/`, so neither word was refused while the shell
        built `/opt`. `_with_assigned_values` fills the file's own assignments into a word
        before judging it, so the second word is judged as `/opt`.

    The comma rule also closed a declared non-claim the review did not have to discover: a
    bash brace list `{/opt,/opt}` used to reduce to a brace-free blob and be ACCEPTED, and
    is now refused on `/opt`, which is one operand a shell would hand to `chmod`.
    `$'\\x2fopt'` stays a non-claim - no comma, and no literal character of the path.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for line, expected in (
        ('RUN python -c "import os; os.chmod(\'/opt\',0o700)"', "/opt"),
        ('RUN python -c "import os; os.chmod(\'/\',0o700)"', "/"),
        ('RUN node -e "require(\'fs\').chmodSync(\'/opt\',0o700)"', "/opt"),
        ("RUN D=/op; chmod 0700 ${D}t", "/opt"),
        ("ENV D=/op\nRUN chmod 0700 ${D}t", "/opt"),
        ('SHELL ["/bin/bash","-c"]\nRUN chmod 0700 {/opt,/opt}', "/opt"),
    ):
        added = _added_problems(shipped, line)
        assert added, f"this line re-modes a pinned path and must be refused: {line}"
        assert _names(added, expected), (
            f"{line} was refused, but not for the path it names (expected {expected!r}):\n"
            + "\n".join(added))
    # The control: the SAME command with a space after the comma was refused before this
    # round and stays refused, so the comma rule cannot be the only thing carrying the
    # class - and a prefix assignment that composes to nothing pinned stays accepted.
    assert _names(_added_problems(
        shipped, 'RUN python -c "import os; os.chmod(\'/opt\', 0o700)"'), "/opt")
    for innocent in ("RUN D=/srv; echo ${D}",
                     "ENV D=/srv\nRUN echo ${D}"):
        assert not _added_problems(shipped, innocent), (
            f"this line assigns and expands a path that names nothing pinned: {innocent}")


def test_a_variable_defined_from_another_variable_is_resolved_first():
    """The chained-assignment escape the round-10 review measured, refused.

    The tenth round expanded a word with the assignments the file had already read, ONE
    level only: a value was stored as written, so `E=${D}t` put the literal text `${D}t`
    into `E`, and `$E` was substituted into that text and never looked at again. The
    round-10 non-author review measured the consequence, and the round-11 probe reproduced
    it on the candidate itself (`evidence/guard-r11-repro-20261107.txt`): `RUN D=/op;
    E=${D}t; chmod 0700 $E` was ACCEPTED, and so were the braced form, a chain of three,
    the same chain over two `ENV` lines, and the legacy two-word `ENV D /op` form - each of
    which re-modes `/opt`, the ancestor whose mode decides whether the export tree can be
    reached at all.

    The fix is substitution, not suspicion: a value is expanded at the point it is assigned
    (as a shell does) and the RESULT is stored, so an arbitrary chain resolves in
    instruction order. The controls at the bottom are what says so - a chain that composes
    `/srvt` or `/srv/data` stays accepted, and so does a legacy `ENV` whose value names
    nothing pinned, because "refuse every instruction that carries a `$`" would pass every
    refused row above and be worthless as a guard.

    The one shape still unresolved is a value naming a variable defined LATER in the file: a
    shell stores the empty string there, and this file has no order to read it in.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")

    for line, expected in (
        ("RUN D=/op; E=${D}t; chmod 0700 $E", "/opt"),
        ("RUN D=/op; E=${D}t; chmod 0700 ${E}", "/opt"),
        ("RUN A=/o; B=${A}p; C=${B}t; chmod 0700 ${C}", "/opt"),
        ("ENV D=/op\nENV E=${D}t\nRUN chmod 0700 ${E}", "/opt"),
        ("ENV D /op\nRUN chmod 0700 ${D}t", "/opt"),
    ):
        added = _added_problems(shipped, line)
        assert added, (
            "this line builds a pinned path out of a variable defined from another and must "
            f"be refused: {line}")
        assert _names(added, expected), (
            f"{line} was refused, but not for the path it builds (expected {expected!r}):\n"
            + "\n".join(added))

    # The round-10 control: the SINGLE-level shape stays refused, so the chain fix cannot
    # have moved the earlier case onto a different ground.
    assert _names(_added_problems(shipped, "RUN D=/op; chmod 0700 ${D}t"), "/opt")

    # And the other direction - the substitution is precise, so a chain that lands on a
    # path naming nothing pinned is accepted. A guard that refused every expansion would
    # pass every row above and fail exactly here.
    for innocent in ("RUN D=/srv; E=${D}t; echo $E",
                     "RUN D=/srv; E=${D}/data; echo ${E}",
                     "ENV LANG C.UTF-8\nRUN echo hi",
                     "ENV D=/srv\nRUN echo ${D}"):
        assert not _added_problems(shipped, innocent), (
            "this line composes a path that names nothing pinned and must be accepted: "
            f"{innocent}")
#: The three instructions the round-11 brief (iss-a5023139fee246f2) lists as "refused, and
#: reported as naming the export tree, when the operand only shares a prefix with a pinned
#: path". MEASURED against the base of that round, 18638499bcc0, with `_added_problems`: all
#: three are ACCEPTED - there is no refusal, and so no ground to correct. They are F1 and F2
#: of the third non-author review, whose OPERAND MODEL refused them (`-x` counted as a
#: relative path, and the SOURCE word of an `install` counted as a mode target); the round-6
#: rewrite deleted that model, and this table keeps the measurement next to the claim so the
#: premise cannot be re-litigated by the next reading of it.
ROUND11_BRIEF_CASES_READ: tuple[tuple[str, str], ...] = (
    ("F1  a flag word and an absolute operand outside the tree",
     "RUN chmod -x /usr/local/bin/tool"),
    ("F2  a relative SOURCE word and a destination outside the tree",
     "RUN install -m 0755 scripts/tool /usr/local/bin/tool"),
    ("F3  a symbolic mode and a relative word",
     "RUN chmod +x scripts/entrypoint.sh"),
)


def test_the_three_instructions_the_round11_brief_calls_false_grounds_are_read():
    """The brief's defect 1, measured instead of implemented: these three are NOT refused.

    A refusal whose ground is wrong is a defect only if the refusal exists. At this base
    these three lines are green, and the docstring's own account says why: refusing every
    word that could belong to a path was measured as a FALSE REFUSAL, "the fastest way to
    get a guard deleted", and the four assertions that pin them as accepted
    (`test_the_guard_does_not_refuse_a_command_that_names_no_pinned_path`, `NON_CLAIMS`
    row E1) are the record of that measurement.

    What is pinned here is therefore the measurement, in the direction it was measured,
    together with the reason each leading word carries no ground at all: `-x` and `+x` are
    a flag and a symbolic mode, not paths, and `scripts/tool` and `scripts/entrypoint.sh`
    are relative words that place, against the shipped `WORKDIR /app`, at
    `/app/scripts/...` - a path that is not the tree and not under it. Making this file
    refuse them would not be stricter; it would redden the shipped-Dockerfile control, which
    carries `RUN pip install --no-cache-dir -e .` and `COPY web/... ./`, i.e. relative words
    of the same kind.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, line in ROUND11_BRIEF_CASES_READ:
        added = _added_problems(shipped, line)
        assert not added, (
            f"{name} names no pinned path and must stay READ - if refusing it is intended, "
            f"say so in the commit message and move the four assertions that pin it:\n"
            f"{line}\n" + "\n".join(added))
    for word in ("-x", "+x", "-m", "scripts/tool", "scripts/entrypoint.sh",
                 "/usr/local/bin/tool"):
        assert _refusal(word, "/app") is None, (
            f"{word!r} is a flag, a symbolic mode or a path outside the tree, and this file "
            f"must not report a ground for it: {_refusal(word, '/app')}")


#: The two grounds a refusal may print, and the one sentence each must carry:
#: `NAMES_GROUND` claims the word, PLACED, IS a pinned path; `UNPLACED_GROUND` claims only
#: that it could reach one (it is `COULD_REACH`, written out so a reworded constant cannot
#: carry the test with it). The split is the round-11 brief's real subject, and every row
#: below is a word this file ALREADY refused at 18638499bcc0 - the ground changes, never
#: the refusal.
NAMES_GROUND = "names the export tree"
UNPLACED_GROUND = "could reach a pinned path, but the text does not place it on one"


def test_a_refusal_prints_the_ground_it_measured():
    """One ground per word, measured on both sides, with the refusal kept in both.

    `aitelier-gate-imports` is the word that carries the brief's real defect. It is a
    DIRECTORY NAME in a build context, not `/opt/aitelier-gate-imports`, and the shipped
    `WORKDIR /app` places it at `/app/aitelier-gate-imports` - so saying it "names the
    export tree" asserts something the text does not say. It must still be refused (this
    file reads the NAME wherever it appears, and the tree could be anywhere a WORKDIR puts
    it), and the refusal must say the operand could reach a pinned path but the text does
    not place it on one, naming the path its stage's WORKDIR gives it.

    The other rows are the shapes the same sentence has to cover - a RELATIVE word carrying
    a `..` the kernel resolves after a symlink, a stage with no WORKDIR of its own, and the
    two ABSOLUTE words the round-3 review of this change measured claiming `NAMES` for a
    placement the classifier never computed: `/proc/self/root/opt/...` (an alias this file
    does not resolve - its measured placement is `/proc/self/root/...`, not the tree) and
    `/var/run/../opt/...` (a `..` is never placed). Both stay REFUSED, on the name they
    spell. The direction that must NOT soften: a word whose measured placement IS the
    client's path, an ancestor, or a path a relative word lands on under the stage WORKDIR,
    keeps the pinned-path ground.
    """
    unplaced = _refusal("aitelier-gate-imports", "/app")
    assert unplaced is not None, (
        "a word that spells the tree's own directory name is refused by contract; a ground "
        "change must not turn into an acceptance")
    assert unplaced.ground == UNPLACED, unplaced
    assert UNPLACED_GROUND in unplaced.reason, unplaced.reason
    assert "/app/aitelier-gate-imports" in unplaced.reason, (
        "the refusal must say where the stage's WORKDIR puts the word, and that the place "
        f"is not the tree: {unplaced.reason}")
    assert NAMES_GROUND not in unplaced.reason, (
        f"this word does not name {EXPORT} and must not be reported as if it did: "
        f"{unplaced.reason}")

    unknown = _refusal("aitelier-gate-imports", None)
    assert unknown is not None and unknown.ground == UNPLACED, unknown
    assert "no WORKDIR" in unknown.reason, (
        f"the reason must say the stage has no WORKDIR to place it against: {unknown.reason}")

    relative_parent = _refusal("docker/../aitelier-gate-imports", "/app")
    assert relative_parent is not None, (
        "a relative word that spells the tree's name stays refused, `..` or no `..`")
    assert relative_parent.ground == UNPLACED and "`..`" in relative_parent.reason, (
        relative_parent.reason)
    assert UNPLACED_GROUND in relative_parent.reason, relative_parent.reason

    for word, measured in (("/proc/self/root/opt/aitelier-gate-imports",
                            "it is the absolute path /proc/self/root/opt/aitelier-gate-imports"),
                           ("/var/run/../opt/aitelier-gate-imports", "`..`")):
        refusal = _refusal(word, None)
        assert refusal is not None and refusal.ground == UNPLACED, (
            f"{word!r} spells the tree's name and must stay refused, on the ground the "
            f"classifier measured rather than a placement it never computed: {refusal}")
        assert UNPLACED_GROUND in refusal.reason and measured in refusal.reason, (
            refusal.reason)
        assert NAMES_GROUND not in refusal.reason, refusal.reason

    for word, workdir in ((TARGET, None), ("/opt", None), ("/", None),
                          ("//opt/aitelier-gate-imports", None),
                          ("docker/godot/godot_harness.py", EXPORT),
                          ("docker/godot/__init__.py", EXPORT)):
        refusal = _refusal(word, workdir)
        assert refusal is not None, f"{word!r} names a pinned path and must be refused"
        assert refusal.ground == NAMES, (
            f"{word!r} against WORKDIR {workdir!r} IS a pinned path (or an ancestor of the "
            f"tree), so the ground is the pinned one, not {UNPLACED!r}: {refusal}")
        assert UNPLACED_GROUND not in refusal.reason, refusal.reason


#: The round-11 brief's two pinned-path rows, as Dockerfile text: the first names the client
#: outright, the second only AFTER the stage's WORKDIR is applied - which is the resolution
#: the brief asks for, and the hole it closes is the MARKER file, whose name this file does
#: not read at all (`REQUIRED_MODES` pins its mode to 0644).
WORKDIR_PLACED_CASES: tuple[tuple[str, str, str], ...] = (
    ("the client named outright",
     "RUN chmod 0700 " + TARGET, TARGET),
    ("the client named only after the stage WORKDIR",
     "WORKDIR " + EXPORT + "\nRUN chmod 0700 docker/godot/godot_harness.py", TARGET),
    ("a pinned MARKER named only after the stage WORKDIR",
     "WORKDIR " + EXPORT + "\nRUN chmod 0700 docker/godot/__init__.py",
     EXPORT + "/docker/godot/__init__.py"),
)


def test_a_workdir_places_a_relative_operand_on_the_pinned_tree():
    """Operands are resolved against the stage WORKDIR, so a relative word cannot slip past.

    `WORKDIR /opt/aitelier-gate-imports` followed by `RUN chmod 0700
    docker/godot/godot_harness.py` re-modes the client, and the RUN's word spells no part of
    a pinned path: it was refused at 18638499bcc0 only because it happens to spell
    `godot_harness.py`, and a word with neither the tree's name nor the client's -
    `docker/godot/__init__.py`, the marker `REQUIRED_MODES` pins to 0644 - was GREEN. That
    is the case the WORKDIR join closes, and the message prints the path it resolved to
    rather than the name it read.

    Both directions are pinned: the same RUN line with the shipped `WORKDIR /app` (or no
    WORKDIR of its own) is READ, so the new refusal is the resolution and not a widened
    word rule - and a word carrying `..` is never placed at all, which is what keeps the
    declared cwd-relative non-claim in `test_the_declared_non_claims_are_still_accepted`
    accepted.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for name, extra, placed in WORKDIR_PLACED_CASES:
        added = _added_problems(shipped, extra)
        assert added, f"{name} re-modes a pinned path and is green:\n{extra}"
        run_problems = [problem for problem in added
                        if problem.splitlines()[0].startswith("RUN carries ")]
        assert run_problems, (
            f"{name} was refused, but not on its RUN line, so the operand itself is still "
            f"unjudged:\n{extra}\n" + "\n".join(added))
        assert any(placed in problem.splitlines()[0] for problem in run_problems), (
            f"{name}: the refusal must name the path the stage WORKDIR places the word on "
            f"({placed!r}):\n" + "\n".join(run_problems))
    # The control: without a WORKDIR on the tree, the same relative MARKER places under the
    # shipped `/app` and names nothing pinned, so the row above measures the resolution and
    # not a widened word rule. The marker is the control and the client is not, because the
    # client's word spells `godot_harness.py` and is refused under ANY WORKDIR - on its
    # NAME, which is the branch the ground test measures.
    assert _refusal("docker/godot/__init__.py", "/app") is None, (
        "this marker word names nothing pinned under the shipped WORKDIR /app and must be "
        f"read: {_refusal('docker/godot/__init__.py', '/app')}")
    harness = _refusal("docker/godot/godot_harness.py", "/app")
    assert harness is not None and harness.ground == UNPLACED, (
        "the client's word is refused on its NAME wherever the WORKDIR puts it, and the "
        f"ground for it says so: {harness}")
    # A new STAGE begins at the base image's WORKDIR, which is not in this file's text, so a
    # WORKDIR of the PREVIOUS stage cannot place an operand: the same relative marker word,
    # reached after a `FROM`, is READ. That is Docker's own reset and not a relaxation - the
    # other direction is pinned above, in the stage that did declare the WORKDIR.
    staged = _problems(f"{shipped}\nWORKDIR {EXPORT}\nFROM python:3.12-slim\n"
                       "RUN chmod 0700 docker/godot/__init__.py\n")
    assert not [problem for problem in staged
                if problem.splitlines()[0].startswith("RUN carries "
                                                      "'docker/godot/__init__.py'")], (
        "a WORKDIR of the PREVIOUS stage must not place an operand in this one:\n"
        + "\n".join(staged))
    # And a `..` word is never placed, in either direction: the kernel resolves one after
    # it follows a symlink, so it stays the declared non-claim it has always been.
    for relative in ("..", "../opt", "~/../.."):
        assert _refusal(relative, "/app") is None, (
            f"{relative!r} is a cwd-relative spelling the docstring declares out of reach: "
            f"{_refusal(relative, '/app')}")


#: The round-3 review of the WORKDIR change, as rows: every shape it measured printing a
#: ground or a path the classifier never computed, with what the FIRST line of the refusal
#: must and must not say. `(rule, context appended to the shipped text first, line, ground,
#: must contain, must not contain)`; ground `None` means the line must be READ. The context is
#: part of the control: a `WORKDIR` on the tree is refused on its own line, so it sits in the
#: text `_added_problems` measures against and only the row's own line is judged.
MEASURED_GROUND_CASES: tuple[tuple[str, str, str, str | None, tuple[str, ...],
                                   tuple[str, ...]], ...] = (
    # 1. A COPY/ADD source is read from the build context, never against the WORKDIR.
    ("1 a context source under a WORKDIR on the tree writes /tmp, not the tree",
     f"WORKDIR {EXPORT}", "COPY docker/godot/__init__.py /tmp/x", None, (), ()),
    ("1 a source that spells the client's name is not joined to the shipped /app",
     "", "COPY docker/godot/godot_harness.py /tmp/x", UNPLACED,
     ("COPY/ADD source", "build context", UNPLACED_GROUND), ("/app/", NAMES_GROUND)),
    ("1 the destination outranks a source, and is placed against the WORKDIR",
     "WORKDIR /opt/aitelier-gate-imports/docker/godot",
     "COPY --chmod=0600 docker/godot/godot_harness.py ./", NAMES,
     ("'./'", "/opt/aitelier-gate-imports/docker/godot"), (UNPLACED_GROUND,)),
    # 2. An absolute word is its own placement.
    ("2 an absolute word under a WORKDIR on the tree is not joined to it",
     f"WORKDIR {EXPORT}", "RUN chmod 0700 /app/aitelier-gate-imports", UNPLACED,
     ("the absolute path /app/aitelier-gate-imports",),
     ("WORKDIR", f"{EXPORT}/app", NAMES_GROUND)),
    ("2 an absolute ancestor names itself, with no WORKDIR in the message",
     f"WORKDIR {EXPORT}", "RUN chmod 0700 /opt", NAMES, ("names an ancestor",), ("WORKDIR",)),
    # 3. A flag's `=value` is placed when it is a path, and the message says which value.
    ("3 --target-directory= carries the tree's directory",
     "", "RUN install -m 0700 --target-directory=/opt/aitelier-gate-imports/docker /tmp/x",
     NAMES, ("'--target-directory'", "'/opt/aitelier-gate-imports/docker' as the value",
             "inside the export tree"), (UNPLACED_GROUND, "/app/")),
    ("3 --reference= carries the tree itself",
     "", "RUN chmod --reference=/opt/aitelier-gate-imports 0700 /tmp/x", NAMES,
     ("'--reference'", f"the export tree {EXPORT}"), (UNPLACED_GROUND, "/app/")),
    ("3 a flag value outside the tree is read",
     "", "RUN install -d --target-directory=/srv/data /tmp/x", None, (), ()),
    # 4. A glob is completed by the shell: its literal head is printed as a HEAD.
    ("4 an absolute glob is not reported as the tree it starts in",
     "", "RUN chmod 0700 /opt/aitelier-gate-imports/*", UNPLACED,
     ("the shell completes it from its literal head '/opt/aitelier-gate-imports/'",),
     (NAMES_GROUND, "WORKDIR")),
    ("4 a relative glob under a WORKDIR on the tree prints its placed head as a head",
     f"WORKDIR {EXPORT}", "RUN chmod 0700 docker/godot/godot_h*", UNPLACED,
     ("the shell completes it from its literal head 'docker/godot/godot_h'",
      f"{EXPORT}/docker/godot/godot_h against the stage WORKDIR {EXPORT}"), ("lands on",)),
    # 5. NAMES only for a measured placement; a name elsewhere is one ground, both spellings.
    ("5 the absolute spelling of a name outside the tree",
     "", "RUN chmod 0700 /app/aitelier-gate-imports", UNPLACED,
     ("/app/aitelier-gate-imports", f"which is not {EXPORT}"), (NAMES_GROUND,)),
    ("5 its relative twin under the shipped WORKDIR /app",
     "", "RUN chmod 0700 aitelier-gate-imports", UNPLACED,
     ("/app/aitelier-gate-imports", f"which is not {EXPORT}"), (NAMES_GROUND,)),
    ("5 an alias this file does not resolve",
     "", "RUN chmod 0700 /proc/self/root/opt/aitelier-gate-imports", UNPLACED,
     ("the absolute path /proc/self/root/opt/aitelier-gate-imports",), (NAMES_GROUND,)),
    # 6. `.` and `./` are placed words.
    ("6 `.` under a WORKDIR inside the tree",
     "WORKDIR /opt/aitelier-gate-imports/docker/godot", "RUN chmod -R 0700 .", NAMES,
     ("/opt/aitelier-gate-imports/docker/godot", "inside the export tree"), ()),
    ("6 `./` under a WORKDIR on an ancestor",
     "WORKDIR /opt", "RUN chmod 0700 ./", NAMES, ("/opt", "an ancestor of the export tree"),
     ()),
    ("6 `.` under the shipped WORKDIR /app is read",
     "", "RUN chmod -R 0755 .", None, (), ()),
    # 7. `FROM <earlier stage>` inherits that stage's WORKDIR - matched case-insensitively.
    ("7 a stage built on an earlier stage starts at its WORKDIR",
     f"FROM python:3.12-slim AS tree\nWORKDIR {EXPORT}\nFROM TREE AS next",
     "RUN chmod 0700 docker/godot/__init__.py", NAMES,
     (f"{EXPORT}/docker/godot/__init__.py", f"against the stage WORKDIR {EXPORT}"), ()),
    ("7 the inherited WORKDIR is carried down a chain of stages",
     f"FROM python:3.12-slim AS tree\nWORKDIR {EXPORT}\nFROM tree AS mid\nFROM mid",
     "RUN chmod 0700 docker/godot/__init__.py", NAMES,
     (f"{EXPORT}/docker/godot/__init__.py",), ()),
    # 8. A `..` word: the name (or the text fold) is measured, the placement is not.
    ("8 an absolute `..` detour that spells the name",
     "", "RUN chmod 0700 /var/run/../opt/aitelier-gate-imports", UNPLACED,
     ("`..`", "not decidable from the text"), (NAMES_GROUND,)),
    ("8 an absolute `..` whose text fold is an ancestor says it is a fold",
     "", "RUN chmod 0700 /opt/x/..", UNPLACED,
     ("`..`", "folded as text, ignoring symlinks, it would read /opt"), ("names an ancestor",)),
)


def test_every_refusal_states_only_what_it_measured():
    """Eight rules, one per kind of word, each pinned by the round-3 review's own shapes.

    The WORKDIR change made the guard print grounds it did not compute: a `COPY` source
    joined to the WORKDIR (Docker reads it from the build context), a glob's literal head
    printed as the path, `--target-directory=/opt/...` read as a RELATIVE name and joined to
    `/app`, `/app/aitelier-gate-imports` claimed as NAMES while its relative twin at the same
    place was UNPLACED, `/var/run/../opt/...` claimed as NAMES although a `..` is never
    placed, and `.`/`./` never placed at all. Each row is measured through `_added_problems`
    on the shipped text, with the stage context a row needs placed BEFORE the measured line.

    The refusal set is not what changed: every line refused before this change is still
    refused (the rows that are READ are the context source, which the old join refused for a
    path the instruction never touches, and two controls). What changed is that the FIRST
    line - the only one `_names` reads - says what was measured and nothing more.
    """
    shipped = (ROOT / "Dockerfile").read_text().rstrip("\n")
    for rule, context, line, ground, must, must_not in MEASURED_GROUND_CASES:
        text = f"{shipped}\n{context}" if context else shipped
        added = _added_problems(text, line)
        if ground is None:
            assert not added, f"rule {rule}: this line must be READ:\n{line}\n" + "\n".join(added)
            continue
        assert len(added) == 1, f"rule {rule}: expected one refusal of {line!r}:\n" + "\n".join(added)
        first = added[0].splitlines()[0]
        if ground == UNPLACED:
            assert UNPLACED_GROUND in first, f"rule {rule}: not reported as UNPLACED:\n{first}"
        else:
            assert UNPLACED_GROUND not in first, f"rule {rule}: not reported as NAMES:\n{first}"
        for text_part in must:
            assert text_part in first, f"rule {rule}: missing {text_part!r}:\n{first}"
        for text_part in must_not:
            assert text_part not in first, f"rule {rule}: must not say {text_part!r}:\n{first}"

    # Rule 5 at the classifier: the two spellings of one place get ONE ground.
    absolute = _refusal("/app/aitelier-gate-imports", "/app")
    relative = _refusal("aitelier-gate-imports", "/app")
    assert absolute is not None and relative is not None
    assert absolute.ground == relative.ground == UNPLACED, (absolute, relative)
    # Rule 3 at the classifier: a flag with no path in its value carries no ground.
    for flag in ("-m", "-x", "--chmod=0644", "--from=build", "--no-cache-dir", "--omit=dev"):
        assert _refusal(flag, EXPORT) is None, (flag, _refusal(flag, EXPORT))
    # Rule 6 at the classifier: `.` and `./` are the WORKDIR itself.
    for dot in (".", "./"):
        refusal = _refusal(dot, EXPORT)
        assert refusal is not None and refusal.ground == NAMES, (dot, refusal)
        assert _refusal(dot, "/app") is None, (dot, _refusal(dot, "/app"))
        assert _refusal(dot, None) is None, (dot, _refusal(dot, None))
