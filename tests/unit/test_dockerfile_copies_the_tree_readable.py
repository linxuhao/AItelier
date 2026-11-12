"""The backend image must not inherit the builder's umask into /app.

`COPY . /app` keeps the mode each file has in the BUILD CONTEXT, so a checkout
written by umask-077 processes bakes 0600 root:root into an image whose backend
runs as uid 1000 and whose disposable test containers run with `-u 1000:1000`.
Measured on the image built from bef46569db: 102 regular files under /app were
mode 600 (build-created bytecode, the two deployment config JSONs, and the SPA
bundle the frontend stage handed over) and 4 directories were not
other-traversable - a non-root consumer could not read that part of the source
while the baked sha256 still matched.

This file reads the repository's real Dockerfile, not a copy of it, and refuses:

  * a tree-wide `COPY . /app` that no later RUN widens: regular files to
    other-readable (`a+r`/`o+r`) and directories to other-traversable
    (`a+rx`/`o+rx`). Comment lines and `\\` continuations are handled, so a RUN
    spread over several lines is seen and a RUN turned into a comment is not;
  * a widening RUN that carries no deny-list, or carries it outside an exclusion
    match. Widening exists to make the source readable, not to publish what
    .dockerignore keeps out of the build context while the image still ships it:
    build-created bytecode (`*.pyc`) and the two deployment config JSON files
    (`model_routes.json`, `llm_providers.json`). They are excluded FROM THE MATCH
    on purpose - a `chmod` after the widening would SET a mode, while the
    contract is that the mode `COPY` left survives untouched;
  * a widening RUN that comes BEFORE the tree-wide COPY: the copy then overwrites
    the widened tree with the builder's modes again.

What this test cannot see, stated on purpose: it reads the Dockerfile, not an
image. It does not prove the widen ran, that a file a later build step creates
keeps a readable mode, or that any path has the bit. Those are properties of a
built image and are measured there (`find /app ! -perm -o=r`,
`docker run -u 1000:1000 <image> ...`); a green here only means the recipe still
describes the widening.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"

COPY_DEST = "/app"
# What the widen must NOT touch: the gitignored build-context files .dockerignore
# does not exclude, plus the bytecode the build itself creates.
DENY_LIST = ("*.pyc", "model_routes.json", "llm_providers.json")

SEGMENT_SEPARATOR = re.compile(r"&&|\|\||;")
NAME_MATCH = r"(?:!|-not)\s+-name\s+['\"]?{}['\"]?(?=\s)"


@dataclass(frozen=True)
class Instruction:
    """One logical Dockerfile instruction; `start`/`end` are 1-based source lines."""

    index: int
    start: int
    end: int
    keyword: str
    body: str


def instructions(text: str) -> list[Instruction]:
    """Parse logical instructions, joining `\\` continuations and dropping comments.

    A comment inside a continuation is dropped rather than joined (Docker's rule),
    which is what makes "turn the RUN into a comment" a different recipe, not a
    longer RUN.
    """
    out: list[Instruction] = []
    buf: list[str] = []
    start: int | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if start is not None and stripped.startswith("#"):
            continue
        if start is None:
            if not stripped or stripped.startswith("#"):
                continue
            start = lineno
        continued = line.endswith("\\")
        buf.append(line[:-1] if continued else line)
        if continued:
            continue
        joined = " ".join(part.strip() for part in buf if part.strip())
        buf = []
        head, _, rest = joined.partition(" ")
        out.append(Instruction(len(out), start, lineno, head.upper(), rest.strip()))
        start = None
    assert start is None, f"{DOCKERFILE.name}:{start}: a continuation never ends"
    return out


def _tokens(body: str) -> list[str]:
    return body.split()


def _tree_wide_copy(inss: list[Instruction]) -> Instruction | None:
    for ins in inss:
        if ins.keyword not in {"COPY", "ADD"}:
            continue
        if any(tok.startswith("--from") for tok in _tokens(ins.body)):
            continue
        operands = [tok for tok in _tokens(ins.body) if not tok.startswith("--")]
        if len(operands) == 2 and operands[0] in {".", "./"} and operands[1].rstrip("/") == COPY_DEST:
            return ins
    return None


def _segments(body: str) -> list[str]:
    return [seg for seg in SEGMENT_SEPARATOR.split(body) if seg.strip()]


def _path_types(segment: str) -> set[str]:
    toks = _tokens(segment)
    kinds: set[str] = set()
    for pos, tok in enumerate(toks):
        if tok == "-type" and pos + 1 < len(toks):
            kinds.add(toks[pos + 1])
        elif tok.startswith("-type") and len(tok) > len("-type"):
            kinds.add(tok[len("-type"):])
    return kinds


def _chmod_modes(segment: str) -> list[str]:
    """The mode argument of every chmod in this shell segment."""
    toks = _tokens(segment)
    modes: list[str] = []
    for pos, tok in enumerate(toks):
        if tok == "chmod" or tok.endswith("/chmod"):
            for cand in toks[pos + 1:]:
                if cand.startswith("-"):
                    continue        # a chmod flag such as -R, or find's own flags
                modes.append(cand)
                break
    return modes


def _grants(mode: str, bit: str) -> bool:
    """True when a symbolic chmod mode gives other-users `bit`.

    Only symbolic modes are read: the contract names `a+r`/`o+r` and `a+rx`/`o+rx`,
    and an octal mode hides which class it widens.
    """
    if "=" in mode:
        who, _, perms = mode.partition("=")
    elif "+" in mode:
        who, _, perms = mode.partition("+")
    else:
        return False
    if not ({"a", "o"} & set(who or "a")):
        return False
    return bit in perms


def _widens_regular_files(body: str) -> bool:
    for seg in _segments(body):
        kinds = _path_types(seg)
        if "d" in kinds and "f" not in kinds:
            continue
        if any(_grants(mode, "r") for mode in _chmod_modes(seg)):
            return True
    return False


def _widens_directories(body: str) -> bool:
    for seg in _segments(body):
        kinds = _path_types(seg)
        if "f" in kinds and "d" not in kinds:
            continue
        if any(_grants(mode, "r") and _grants(mode, "x") for mode in _chmod_modes(seg)):
            return True
    return False


def _widens(body: str) -> bool:
    return _widens_regular_files(body) or _widens_directories(body)


def _excludes(body: str, name: str) -> bool:
    """True when `name` is kept out of the widening by a `! -name` predicate."""
    return re.search(NAME_MATCH.format(re.escape(name)), body) is not None


def widening_recipe_refusal(text: str) -> str | None:
    """Why this Dockerfile would ship an unreadable tree, or None when it would not."""
    inss = instructions(text)
    copy = _tree_wide_copy(inss)
    if copy is None:
        return (f"{DOCKERFILE.name}: no tree-wide COPY of the repository into {COPY_DEST} "
                "- a renamed or deleted copy must not pass by finding nothing to normalize")
    widenings = [ins for ins in inss if ins.keyword == "RUN" and _widens(ins.body)]
    early = [ins for ins in widenings if ins.index < copy.index]
    if early:
        return (f"{DOCKERFILE.name}:{early[0].start}: the mode-widening RUN comes BEFORE the "
                f"tree-wide COPY at line {copy.start}, which overwrites it with the builder's modes")
    later = [ins for ins in widenings if ins.index > copy.index]
    if not later:
        return (f"{DOCKERFILE.name}:{copy.start}: the tree-wide COPY into {COPY_DEST} is never "
                "widened after it: regular files stay as unreadable and directories as "
                "untraversable as the builder's umask left them")
    for ins in later:
        if not (_widens_regular_files(ins.body) and _widens_directories(ins.body)):
            return (f"{DOCKERFILE.name}:{ins.start}: this widening RUN does not make regular "
                    "files other-readable AND directories other-traversable")
    for ins in later:
        missing = [name for name in DENY_LIST if not _excludes(ins.body, name)]
        if missing:
            return (f"{DOCKERFILE.name}:{ins.start}: the widening RUN excludes none of "
                    f"{missing!r} from its match, so it would publish what .dockerignore keeps "
                    "out of the build context")
    return None


def _only_widening_run(text: str) -> Instruction:
    runs = [ins for ins in instructions(text) if ins.keyword == "RUN" and _widens(ins.body)]
    assert len(runs) == 1, (
        "these mutation helpers assume exactly one mode-widening RUN; found "
        f"{[(ins.start, ins.body[:40]) for ins in runs]}")
    return runs[0]


def _without(text: str, ins: Instruction) -> str:
    lines = text.splitlines(keepends=True)
    return "".join(lines[: ins.start - 1] + lines[ins.end:])


def _commented_out(text: str, ins: Instruction) -> str:
    lines = text.splitlines(keepends=True)
    block = ["# " + line for line in lines[ins.start - 1: ins.end]]
    return "".join(lines[: ins.start - 1] + block + lines[ins.end:])


def _moved_before_the_copy(text: str) -> str:
    run = _only_widening_run(text)
    copy = _tree_wide_copy(instructions(text))
    assert copy is not None and copy.index < run.index, "the widening RUN is already before it"
    moved = _without(text, run)
    lines = moved.splitlines(keepends=True)
    return "".join(lines[: copy.start - 1] + text.splitlines(keepends=True)[run.start - 1: run.end]
                   + lines[copy.start - 1:])


def test_the_shipped_dockerfile_widens_the_tree_after_the_copy():
    refusal = widening_recipe_refusal(DOCKERFILE.read_text())
    assert refusal is None, refusal


def test_deleting_the_widening_run_is_refused():
    text = DOCKERFILE.read_text()
    mutated = _without(text, _only_widening_run(text))
    assert mutated != text
    refusal = widening_recipe_refusal(mutated)
    assert refusal is not None and "never widened after it" in refusal, refusal


def test_weakening_other_read_to_other_write_is_refused():
    text = DOCKERFILE.read_text()
    run = _only_widening_run(text)
    block = text.splitlines(keepends=True)[run.start - 1: run.end]
    weakened = [line.replace("chmod a+r ", "chmod a+w ") for line in block]
    assert weakened != block, "the widening RUN no longer widens files with `chmod a+r`"
    lines = text.splitlines(keepends=True)
    mutated = "".join(lines[: run.start - 1] + weakened + lines[run.end:])
    refusal = widening_recipe_refusal(mutated)
    assert refusal is not None and "other-readable" in refusal, refusal


def test_moving_the_widening_run_before_the_copy_is_refused():
    refusal = widening_recipe_refusal(_moved_before_the_copy(DOCKERFILE.read_text()))
    assert refusal is not None and "BEFORE the tree-wide COPY" in refusal, refusal


def test_commenting_out_the_widening_run_is_refused():
    text = DOCKERFILE.read_text()
    mutated = _commented_out(text, _only_widening_run(text))
    assert mutated != text
    refusal = widening_recipe_refusal(mutated)
    assert refusal is not None and "never widened after it" in refusal, refusal


def test_the_deny_list_is_excluded_from_the_match_not_chmod_ed_back():
    """A widening RUN that only chmods afterwards is not the contract either."""
    text = DOCKERFILE.read_text()
    run = _only_widening_run(text)
    lines = text.splitlines(keepends=True)
    block = list(lines[run.start - 1: run.end])
    for name in DENY_LIST:
        block = [re.sub(NAME_MATCH.format(re.escape(name)) + r"\s*", "", line) for line in block]
    mutated = "".join(lines[: run.start - 1] + block + lines[run.end:])
    assert mutated != text
    refusal = widening_recipe_refusal(mutated)
    assert refusal is not None and "excludes none of" in refusal, refusal
