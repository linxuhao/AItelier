# Director-defined prose corruptions - rev 8

Deliverable of this round: the corruption catalog the director defined, applied
byte-for-byte by a runner, with the self-proof fixtures separated from the
live-tree tripwire, K6c restored to the r5 m14 line, and an `--only` selector.

## What is in the tree

| path | role |
|---|---|
| `tools/prose_corruptions/catalog.py` | the corruption bytes, one entry per corruption; the only place they live |
| `tools/prose_corruptions/run_corruptions.py` | applies entries to a throwaway worktree and runs the selected pytest scope there |
| `tools/prose_corruptions/scan_joined_lines.py` | scans a range's added lines for the three splice shapes |
| `tests/unit/test_prose_corruption_catalog.py` | catalog completeness, the two poles per K, the scanner's positives and its r6 false positives |
| `tests/unit/test_truncation_is_not_a_formatting_mistake.py` | the checker and the fixtures that drive it from the catalog |

## The corruptions (unchanged bytes)

| id | file | change | rule that must fire |
|---|---|---|---|
| K1 | `templates/fix_tests.md` | the stale-hunk sentence becomes `send a smaller hunk.` | `stale_hunk_remedy` |
| K2 | `templates/fix_tests.md` (after K1) | `quote the \`sha\` from its \`citation\`` drops both nouns | `reference_advice_missing` |
| K3 | `core/output_migration.py` | the ZH sentence announcing the reference example becomes a newline | `unannounced_reference_example` |
| K4 | `core/zz_new_prompt.py` (new) | a plain `SYSTEM_PROMPT` constant | `unaccounted_prose_constant` |
| K5 | `core/zz_new_prompt.py` (new) | the same prompt as an f-string | `unaccounted_prose_constant` |
| K6a | r2 EN guidance | duplicated two-line run (`rev` 3b3d6560) | `repeated_run` |
| K6b | r2 ZH guidance | corrupted block (`rev` 3b3d6560) | `repeated_run` |
| K6c | `core/dpe_pipeline.py` | duplicate the `# ── Why did a JSON reply fail to parse?` banner line | `adjacent_duplicate_line` |
| K6d | r3 ZH guidance | trailing backtick (`rev` fad833b0) | `odd_backtick_count` |
| K6e | `templates/fix_tests.md` | clipped block (`rev` 3b3d6560) | `severed_clause` |

K6c is now the r5 m14 line (locator `# ── Why did a JSON reply fail to
parse?`, asserted unique); the old `Step dispatch` entry is gone, not kept
alongside. `test_k6c_duplicates_exactly_one_line_and_overwrites_nothing`
asserts the repeated line is that banner, that `git diff` adds exactly one
line, and that the added line is not `Step dispatch`.

## The two poles of the self-proof fixtures (criterion 4, item 1)

The self-proof fixtures take their clean pole from HEAD
(`MOD._head_read` -> `git show HEAD:<path>`), which the runner never commits,
so the same bytes are intact in the pipeline worktree and in the reviewer's
one-shot container. The live-tree tripwire reads the worktree
(`MOD._read`). One simulated damage per corruption drives both:

* `test_the_self_proof_fixture_is_green_on_a_damaged_live_tree[Kx]` -
  monkeypatches `_read` to return the runner's damaged bytes, then asserts the
  HEAD clean pole carries no violation and the corrupted pole still fires the
  catalog's rule naming the surface.
* `test_the_live_tree_scan_names_the_rule_when_the_tree_is_damaged[Kx]` -
  the same monkeypatch, then the corpus derived through `_read` is asserted
  red on that surface, naming the rule.
* `test_the_new_module_fixture_is_green_and_the_scan_names_the_module` - the
  K4/K5 fixture builds its own tree (unaffected by the damaged `_read`) and
  the accounting scan names `core/zz_new_prompt.py`.

Pole ① (clean tree) is `test_each_existing_surface_corruption_fires_its_named_rule[Kx]`
and `test_each_known_corruption_is_caught_by_name[Kx]`.

## Commands the reviewer can run (criterion 4, item 4)

Whole suite, one line per corruption. Each applies one entry to a clean
throwaway worktree and runs the WHOLE suite there; the table row's selection
column repeats the scope, so a narrow run is never read as the whole suite.
The per-row result is measured by the reviewer (`final/` says so; the implement
step cannot run the runner).

    python tools/prose_corruptions/run_corruptions.py --only K1  --out logs/K1_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K2  --out logs/K2_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K3  --out logs/K3_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K4  --out logs/K4_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K5  --out logs/K5_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K6a --out logs/K6a_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K6b --out logs/K6b_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K6c --out logs/K6c_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K6d --out logs/K6d_whole.txt
    python tools/prose_corruptions/run_corruptions.py --only K6e --out logs/K6e_whole.txt

Card-file selection (the empty-mutation control on this card's own test files,
no corruption applied):

    python tools/prose_corruptions/run_corruptions.py --targets tests/unit/test_truncation_is_not_a_formatting_mistake.py tests/unit/test_prose_corruption_catalog.py --out logs/prose_corruptions_card_files.txt

Base pole (K1/K3/K4/K5 leave that tree green):

    python tools/prose_corruptions/run_corruptions.py --rev 527beafa --out logs/prose_corruptions_base.txt

Whole-suite result column: measured by the reviewer.

## Empty-mutation control: the numbers this round actually produced

The implement step has `focused_check` only. Its controls, bare exit status,
log `logs/prose_corruptions_rev8_focused.txt`:

| probe | scope | exit status | result |
|---|---|---|---|
| `tests/unit/test_prose_corruption_catalog.py` | card file | 0 | 45 passed |
| `tests/unit/test_truncation_is_not_a_formatting_mistake.py` | card file | 0 | 45 passed |
| `...::test_re_exempting_a_corpus_prompt_is_an_assertion_error_naming_it` | mutation x1 | 0 | 1 passed |

No whole-suite number is written here: the implement step did not run the
suite.

## The three splice shapes (criterion 5, item 2)

Log: `logs/prose_corruptions_rev8_scan.txt`. The scanner's r6 report named nine
lines on this branch's range `527beafa..7c43f6a5`; all nine are alignment space
runs inside a quoted literal, an aligned trailing comment, or rule characters
quoted inside a sentence of prose. After the repair the same probe reports
`hits: 0` on the same range. The nine, each now a fixture in
`test_the_spliced_line_scanner_is_silent_on_the_r6_false_positives`:

| line | what it is |
|---|---|
| `tests/unit/test_prose_corruption_catalog.py:147` | alignment spaces inside a quoted sample string |
| `tests/unit/test_prose_corruption_catalog.py:148` | same, continuation literal |
| `tests/unit/test_prose_corruption_catalog.py:152` | `` `sha` `` inside a quoted prose sample |
| `tests/unit/test_prose_corruption_catalog.py:154` | alignment inside a quoted sample |
| `tests/unit/test_prose_corruption_catalog.py:162` | alignment inside a quoted sample |
| `tests/unit/test_prose_corruption_catalog.py:163` | alignment inside a quoted sample |
| `tools/prose_corruptions/catalog.py:25` | aligned trailing `# r2 candidate` comment |
| `tools/prose_corruptions/catalog.py:26` | aligned trailing `# r3 candidate` comment |
| `tools/prose_corruptions/scan_joined_lines.py:11` | rule characters quoted in the module docstring |

The positive shapes still fire: `test_the_spliced_line_scanner_fires_on_the_splice_shapes`
plants a two-statements-one-line sample, a banner-glued sample and a
sentence-split sample and requires each name;
`test_the_spliced_line_scanner_is_silent_on_clean_lines` requires silence on
intact equivalents. Scanning ALL added lines of the range is done by the
reviewer.

## Repairs this round

* `tests/unit/test_prose_corruption_catalog.py` - the assertion message at the
  old `:159` was the unbound `namee`; it is `name` again, and
  `test_re_exempting_a_corpus_prompt_is_an_assertion_error_naming_it` drives
  mutation x1 through the real check and requires AssertionError, not
  NameError.
* The scanner stopped reporting a space run inside a string or after a comment
  marker (`_code_mask`, `_is_apostrophe`) and stopped reporting rule characters
  quoted in prose (`_banner_glued` examines only comment lines).
* K6c moved from the `Step dispatch` line to the r5 m14 banner.
* The self-proof fixtures read HEAD; the live-tree scan reads the worktree.
* `run_corruptions.py` gained `--only`.

## Files changed this round, read back verbatim

### `tools/prose_corruptions/catalog.py` (read back, whole file)

```python
"""Director-defined prose corruptions, byte for byte.

Each entry is one corruption of an agent-facing prose surface. The bytes in
`old` and `new` are quoted verbatim from the director; nothing here rewrites,
weakens or replaces them. `apply_edits` refuses an `old` that does not occur
exactly once, so an entry can never silently drift away from the tree it is
supposed to damage.

One catalog, two readers:

  * tests/unit/test_truncation_is_not_a_formatting_mistake.py derives its
    corruption fixtures from here, so the bytes that measure the checker are
    the bytes the runner applies;
  * tools/prose_corruptions/run_corruptions.py applies each entry to a clean
    git worktree, runs the whole suite there and records the bare exit code.

`surface` is the name the checker's corpus uses for the damaged file, or None
for an entry that CREATES a new module (there is no existing surface to name;
those entries are measured through the constant-accounting check).
`rule` names the checker rule that must fire on the corrupted bytes.
"""

import re

R2 = "3b3d6560"                                    # r2 candidate
R3 = "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722"    # r3 candidate

# K1: templates/fix_tests.md loses the local remedy for a stale hunk.
K1_OLD = ("If a hunk is stale or\n"
          "ambiguous, reread that range and cite its `sha` instead of "
          "copying more of the\n"
          "file. Group")
K1_NEW = ("If a hunk is stale or\n"
          "ambiguous, send a smaller hunk.\n"
          "Group")

# K2: applied after K1, the reference advice loses what to cite and where the
# citation comes from; the file then contains no `sha` at all.
K2_OLD = "read the range, quote the `sha` from its `citation`, and send only"
K2_NEW = "read the range, and send only"

# K3: the ZH reference example loses the sentence that announces it.
K3_OLD = "\u539f\u6837\u8d34\u56de\u6765\uff0c\u53ea\u63d0\u4f9b\u65b0"
K3_OLD += "\u6587\u672c\uff1a\n"
K3_NEW = "\n"

# K4/K5: a NEW module carrying agent-facing prose the accounting table has
# never heard of. K5 is the same prompt as an f-string, so a checker that only
# reads `ast.Constant` misses it.
K4_CONTENT = (
    'SYSTEM_PROMPT = """You are the reviewer. Read the diff carefully and\n'
    "report every defect you find, citing file and line for each one of them.\n"
    "report every defect you find, citing file and line for each one of them.\n"
    '"""\n')
K5_CONTENT = (
    'ROLE = "reviewer"\n'
    'NEW_AGENT_PROMPT = f"""You are the {ROLE}. Read the diff carefully and\n'
    "report every defect you find, citing file and line for each one of them.\n"
    "report every defect you find, citing file and line for each one of them.\n"
    '"""\n')

# K6c is the r5 m14 shape: duplicate the single comment-rule banner line
# `# \u2500\u2500 Why did a JSON reply fail to parse?` in core/dpe_pipeline.py, so the git
# diff after applying it is exactly one added line, not a whole-file overwrite
# of core/dpe_pipeline.py. The locator is that line's unique text;
# `duplicate_line` refuses a locator that does not select exactly one line.
K6C_LOCATE = "# \u2500\u2500 Why did a JSON reply fail to parse?"

# K6: the bytes that were already corrupt in r2/r3, kept as they were.
K6_ENTRIES = (
    {"id": "K6a", "what": "r2 EN duplicated two-line run",
     "rev": R2, "block": "EN", "surface": "GUIDANCE_EN", "rule": "repeated_run"},
    {"id": "K6b", "what": "r2 ZH corruption",
     "rev": R2, "block": "ZH", "surface": "GUIDANCE_ZH", "rule": "repeated_run"},
    {"id": "K6d", "what": "r3 ZH trailing backtick",
     "rev": R3, "block": "ZH", "surface": "GUIDANCE_ZH",
     "rule": "odd_backtick_count"},
    {"id": "K6e", "what": "fix_tests.md clipped block",
     "rev": R2, "path": "templates/fix_tests.md",
     "surface": "templates/fix_tests.md", "rule": "severed_clause"},
)

CORRUPTIONS = (
    {"id": "K1", "what": "fix_tests.md stale-hunk remedy replaced",
     "kind": "edit", "path": "templates/fix_tests.md",
     "edits": ({"old": K1_OLD, "new": K1_NEW},),
     "surface": "templates/fix_tests.md", "rule": "stale_hunk_remedy"},
    {"id": "K2", "what": "fix_tests.md reference advice loses cite/where",
     "kind": "edit", "path": "templates/fix_tests.md",
     "edits": ({"old": K1_OLD, "new": K1_NEW},
               {"old": K2_OLD, "new": K2_NEW}),
     "surface": "templates/fix_tests.md", "rule": "reference_advice_missing"},
    {"id": "K3", "what": "ZH reference example left unannounced",
     "kind": "edit", "path": "core/output_migration.py", "block": "ZH",
     "edits": ({"old": K3_OLD, "new": K3_NEW},),
     "surface": "GUIDANCE_ZH", "rule": "unannounced_reference_example"},
    {"id": "K4", "what": "new module, plain prompt constant",
     "kind": "add", "path": "core/zz_new_prompt.py", "content": K4_CONTENT,
     "surface": None, "rule": "unaccounted_prose_constant"},
    {"id": "K5", "what": "new module, prompt constant as an f-string",
     "kind": "add", "path": "core/zz_new_prompt.py", "content": K5_CONTENT,
     "surface": None, "rule": "unaccounted_prose_constant"},
    {"id": "K6c", "what": "duplicate the JSON-parse banner line",
     "kind": "dupline", "path": "core/dpe_pipeline.py",
     "locate": K6C_LOCATE, "surface": "core/dpe_pipeline.py",
     "rule": "adjacent_duplicate_line"},
) + tuple(dict(entry, kind="git") for entry in K6_ENTRIES)


def apply_edits(text, entry):
    """`entry`'s edits applied to `text`, each `old` required exactly once."""
    for edit in entry["edits"]:
        old, new = edit["old"], edit["new"]
        found = text.count(old)
        if found != 1:
            raise AssertionError(
                f"{entry['id']}: the old bytes must occur exactly once, "
                f"found {found}: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def duplicate_line(text, locate):
    """Insert a copy of the single line containing `locate`, exactly once.

    The corruption is one repeated banner line, so applying it changes the
    file by a single added line. A locator that is absent or that matches more
    than one line raises, so the entry can never damage a different line or
    overwrite a whole file.
    """
    matched = [ln for ln in text.splitlines(keepends=True) if locate in ln]
    if len(matched) != 1:
        raise AssertionError(
            f"the locator must select exactly one line, found "
            f"{len(matched)}: {locate!r}")
    line = matched[0] if matched[0].endswith("\n") else matched[0] + "\n"
    return text.replace(matched[0], matched[0] + line, 1)


def apply_to_text(text, entry):
    """The corrupted text of an on-disk surface, built from a clean copy.

    A `dupline` entry repeats one line; every other on-disk entry replaces
    bytes through `apply_edits`.
    """
    if entry["kind"] == "dupline":
        return duplicate_line(text, entry["locate"])
    return apply_edits(text, entry)


def surface_name(entry):
    return entry["surface"]


def _guidance(text, block):
    match = re.search(rf'STRICT_PATCH_GUIDANCE_{block} = """(.*?)"""', text,
                      re.S)
    if not match:
        raise AssertionError(f"no STRICT_PATCH_GUIDANCE_{block} in the tree")
    return match.group(1)


def surface_text(entry, read, show):
    """The corrupted text of `entry`'s surface, built from a clean tree.

    `read(rel)` returns a file from the tree; `show(rev, rel)` returns the
    file as of `rev`. Neither writes anything.
    """
    if entry.get("rev"):
        if entry.get("block"):
            return _guidance(show(entry["rev"], "core/output_migration.py"),
                             entry["block"])
        return show(entry["rev"], entry["path"])
    text = apply_to_text(read(entry["path"]), entry)
    if entry.get("block"):
        return _guidance(text, entry["block"])
    return text


HEAD_READ_HINT = (
    "Pass a reader that returns the file as of HEAD, e.g. "
    "`lambda rel: _git_file('HEAD', rel)`, never the live worktree read: a "
    "runner that has already corrupted the worktree would hand the fixture "
    "its own damaged bytes as the \"clean\" pole, and the fixture would fail "
    "on a precondition instead of measuring detection."
)


def clean_surface_text(entry, head_read):
    """The INTACT bytes of `entry`'s surface, from a mutation-immune source.

    `head_read(rel)` must return the file as of the committed HEAD, not the
    working tree on disk. HEAD is the same committed content in the pipeline
    worktree and in the reviewer's one-shot container, and the corruption
    runner never commits its edit, so this is the clean pole even while the
    runner has the live tree damaged. See HEAD_READ_HINT.
    """
    assert head_read is not None, HEAD_READ_HINT
    if entry.get("block"):
        return _guidance(head_read("core/output_migration.py"), entry["block"])
    return head_read(entry["path"])


def entry_file_path(entry):
    """The file `entry` damages; block-only entries damage output_migration."""
    return entry.get("path", "core/output_migration.py")


def corrupt_file_text(entry, read, show):
    """The bytes the runner writes into `entry`'s FILE, built from a clean copy.

    Mirrors the runner's `_apply` exactly, so a test can reconstruct the live
    bytes a corruption run leaves behind without invoking the runner. `read`
    supplies a clean file and `show(rev, rel)` an old revision; neither writes.
    """
    if entry["kind"] == "add":
        return entry["content"]
    if entry.get("rev") and entry.get("block"):
        text = read("core/output_migration.py")
        block = _guidance(show(entry["rev"], "core/output_migration.py"),
                          entry["block"])
        return re.sub(
            rf'(?s)(STRICT_PATCH_GUIDANCE_{entry["block"]} = """)(.*?)(""")',
            lambda m: m.group(1) + block + m.group(3), text)
    if entry.get("rev"):
        return show(entry["rev"], entry["path"])
    return apply_to_text(read(entry["path"]), entry)


def existing_surface_entries():
    """Entries that damage a surface the checker's corpus already carries."""
    return [entry for entry in CORRUPTIONS if entry["surface"] is not None]


def new_module_entries():
    """Entries that create a module under core/ instead of editing one."""
    return [entry for entry in CORRUPTIONS if entry["kind"] == "add"]

# The rule a corruption must fire is DATA, held here and looked up by the
# checker (tests/.../test_truncation_is_not_a_formatting_mistake.py
# set_active_corruption reads this table); it is never a substring of the
# damaged prose, the shape the director forbade ("sha" in text). Slots name the
# violation-emitting rule (adjacent / repeated_run / odd_backtick / severed /
# unannounced_example / reference_advice / stale_hunk); each value is the rule
# name the checker emits for that corruption, equal to the entry's `rule`.
PROSE_RULES = {
    "K1": {"stale_hunk": "stale_hunk_remedy"},
    "K2": {"reference_advice": "reference_advice_missing"},
    "K3": {"unannounced_example": "unannounced_reference_example"},
    "K6a": {"repeated_run": "repeated_run"},
    "K6b": {"repeated_run": "repeated_run"},
    "K6c": {"adjacent": "adjacent_duplicate_line"},
    "K6d": {"odd_backtick": "odd_backtick_count"},
    "K6e": {"severed": "severed_clause"},
}
```

### `tools/prose_corruptions/run_corruptions.py` (changed regions, read back)

```python
def select_entries(catalog, only=()):
    """The catalog entries `only` selects; empty `only` keeps every entry.

    `only` is a sequence of corruption ids (K1, K6c, ...). An id the catalog
    does not carry is an error, so a typo cannot silently run just the
    remaining entries.
    """
    entries = list(catalog.CORRUPTIONS)
    if not only:
        return entries
    known = {entry["id"]: entry for entry in entries}
    unknown = [name for name in only if name not in known]
    assert not unknown, f"no such corruption id(s): {unknown}"
    return [known[name] for name in only]


def run_all(rev=None, log=None, targets=(), raw_dir=None, only=()):
    catalog = _load_catalog()
    rows = []
    selection = " ".join(targets) if targets else "tests/ (whole suite)"
    source_sha = _git(["rev-parse", "HEAD"], REPO_ROOT).stdout.strip()
    source_before = _git(["status", "--porcelain"], REPO_ROOT).stdout
    for entry in select_entries(catalog, only):
```

and, in `main()`:

```python
    parser.add_argument("--raw-dir", default=None,
                        help="directory for one raw-output file per "
                             "corruption (default: alongside --out)")
    parser.add_argument("--only", nargs="*", default=[],
                        help="apply only these corruption ids (e.g. --only K3); "
                             "empty means every catalog entry")
    args = parser.parse_args()
```

```python
    rows = run_all(rev=args.rev, log=log, targets=tuple(args.targets),
                   raw_dir=raw_dir, only=tuple(args.only))
```

### `tools/prose_corruptions/scan_joined_lines.py` (changed regions, read back)

```python
def _banner_glued(text):
    """True when a COMMENT line's rule stops before the line does.

    Only a line that is a comment is examined: the rule characters inside a
    sentence of prose (``a comment rule (───, ===, ***) ends before``) are
    quoting the shape, not exhibiting it.
    """
    stripped = text.strip()
    if not stripped.startswith(("#", "//")):
        return False
    if not BANNER.search(stripped):
        return False
    last = None
    for match in BANNER.finditer(stripped):
        last = match
    return bool(stripped[last.end():].strip())


def _code_mask(text):
    """True per character that is CODE, not inside a string or a comment.

    A run of alignment spaces inside a quoted literal or an aligned trailing
    comment is that text's own indentation; only a run that is entirely code
    is the indentation of a swallowed continuation line.
    """
    mask = []
    quote = None
    escaped = False
    for index, ch in enumerate(text):
        if quote:
            mask.append(False)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "'" and _is_apostrophe(text, index):
            mask.append(True)
            continue
        if ch in "\"'":
            mask.append(False)
            quote = ch
            continue
        if ch == "#":
            mask.append(False)
            quote = "#"
            continue
        mask.append(True)
    return mask


def _is_apostrophe(text, index):
    """True when the `'` at `index` is the one in `don't`, not a quote."""
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return before.isalnum() and after.isalnum()


def _two_statements_one_line(text):
    """True when indentation that belonged at column 0 sits mid-line."""
    if text.lstrip().startswith(("#", "//", "*", "-", ">")):
        return False
    if not CODE_HINT.search(text):
        return False
    # A markdown table or aligned comment column is not a splice.
    if "|" in text:
        return False
    mask = _code_mask(text)
    for match in MID_LINE_GAP.finditer(text):
        if all(mask[i] for i in range(match.start(), match.end())):
            return True
    return False
```

### `tests/unit/test_truncation_is_not_a_formatting_mistake.py` (changed regions, read back)

```python
def _head_read(rel):
    """The committed bytes of `rel` as of HEAD, not the working tree.

    The self-proof fixtures below take their clean pole from here. The
    corruption runner damages a throwaway copy's working tree and never
    commits, so HEAD is intact in the pipeline worktree and in the reviewer's
    one-shot container: the fixtures stay green while the live tree is
    damaged, and only the live-tree scan goes red. Tests that must see the
    live tree read it through `_read`, which the tripwire below redirects.
    """
    return _git_file("HEAD", rel)
```

```python
def _guidance_from_file(text, block):
    """The `STRICT_PATCH_GUIDANCE_<block>` literal inside `text`."""
    match = re.search(
        rf'STRICT_PATCH_GUIDANCE_{block} = """(.*?)"""', text, re.S)
    assert match, f"no STRICT_PATCH_GUIDANCE_{block} in the file text"
    return match.group(1)


def _prose_corpus(reader=None):
    """Every agent-facing prose surface, derived from the filesystem.

    Nothing here is a hand-written list of surfaces: templates/*.md is a
    glob, agent_configs/*.yaml is a glob, and the prompt constants are read
    out of the modules that declare them, so a surface created after this
    test was written is IN the corpus on the next run without touching this
    file.

    `reader` is the source the file-backed surfaces are read from: the live
    worktree by default (resolved at call time, so a test can redirect
    `_read`), so a corruption run's damaged tree goes red in
    `test_the_empty_mutation_leaves_every_surface_clean`; the self-proof
    fixtures pass `_head_read` instead, so their clean pole is HEAD and stays
    intact while the live tree is corrupted.
    """
    import yaml

    if reader is None:
        reader = _read
    # The guidance blocks are corpus surfaces too, and they are read through
    # the same `reader`: a runner that damaged core/output_migration.py must
    # show up as a violation on GUIDANCE_EN / GUIDANCE_ZH, not be masked by
    # the module constant that was imported once at collection time.
    migration = reader("core/output_migration.py")
    corpus = {
        "GUIDANCE_EN": _guidance_from_file(migration, "EN"),
        "GUIDANCE_ZH": _guidance_from_file(migration, "ZH"),
    }
    for md in sorted((REPO_ROOT / "templates").glob("*.md")):
        corpus[f"templates/{md.name}"] = reader(f"templates/{md.name}")
    for rel in PROSE_CODE_FILES:
        corpus[rel] = reader(rel)
```

```python
def _core_module_paths(root=REPO_ROOT):
    """Every `*.py` module under `root`/core, sorted."""
    return sorted((root / "core").glob("*.py"))


def _prose_constants(root=REPO_ROOT):
    """{(module path, constant name): text} for every prose-sized constant."""
    found = {}
    for py in _core_module_paths(root):
        rel = f"core/{py.name}"
```

```python
def _corruption_loader(entry):
    """The corrupted SURFACE text: git bytes, or HEAD's file plus edits.

    The clean pole is read from HEAD through `_head_read`, not from the live
    worktree: the corruption runner damages a throwaway copy without
    committing, so a fixture that read the live tree would take the runner's
    own damaged bytes as "clean" and go red on a precondition instead of
    measuring detection.
    """

    def load():
        if entry.get("rev"):
            if entry.get("block"):
                return _git_guidance(entry["rev"], entry["block"])
            return _git_file(entry["rev"], entry["path"])
        return _CATALOG.apply_to_text(_head_read(entry["path"]), entry)

    return load
```

```python
@pytest.mark.parametrize("label", sorted(GIT_CORRUPTIONS))
def test_each_known_corruption_is_caught_by_name(label):
    name, load = GIT_CORRUPTIONS[label]
    set_active_corruption(label)
    try:
        corrupted = load()
        clean = CATALOG.clean_surface_text(_CATALOG_BY_ID[label], _head_read)
        assert corrupted != clean, \
            f"{label}: the fixture is not actually corrupt"
```

### `tests/unit/test_prose_corruption_catalog.py` (changed regions, read back)

```python
def test_each_edit_entry_applies_to_its_own_surface():
    """Applied to its own surface, every edit changes the file.

    The bytes come from HEAD, not the live worktree: this is a
    catalog-drift check and must stay green while a corruption run has the
    live tree damaged (the runner would have applied the same `old` once
    already, so a live read would find zero occurrences and fail on a
    precondition).
    """
    for entry in CATALOG.CORRUPTIONS:
        if entry["kind"] != "edit":
            continue
        text = MOD._head_read(entry["path"])
        assert CATALOG.apply_edits(text, entry) != text, entry["id"]
```

```python
@pytest.mark.parametrize("entry", CATALOG.existing_surface_entries(),
                         ids=lambda e: e["id"])
def test_each_existing_surface_corruption_fires_its_named_rule(entry):
    surface = CATALOG.surface_name(entry)
    clean = CATALOG.clean_surface_text(entry, MOD._head_read)
    corrupted = CATALOG.surface_text(entry, MOD._head_read, MOD._git_file)
    assert corrupted != clean, f"{entry['id']}: the fixture is not corrupt"
    violations = MOD._prose_violations(surface, corrupted)
    assert violations, f"{entry['id']}: no violation on the corrupted {surface}"
    assert any(entry["rule"] in v for v in violations), \
        (entry["id"], entry["rule"], violations)
    assert any(surface in v for v in violations), violations
    assert not MOD._prose_violations(surface, clean), (surface, "clean is red")


def test_k6c_duplicates_exactly_one_line_and_overwrites_nothing():
    """K6c is the r5 m14 shape: one repeated banner line, not a whole-file swap.

    The repeated line is `# ── Why did a JSON reply fail to parse?` in
    core/dpe_pipeline.py, the banner the r5 review measured. The bytes come
    from HEAD, so the check is a statement about the committed file and holds
    while a runner has the live tree damaged.
    """
    import difflib
    entry = next(e for e in CATALOG.CORRUPTIONS if e["id"] == "K6c")
    assert entry["locate"] == "# ── Why did a JSON reply fail to parse?"
    text = MOD._head_read("core/dpe_pipeline.py")
    corrupted = CATALOG.apply_to_text(text, entry)
    assert len(corrupted.splitlines()) == len(text.splitlines()) + 1
    added = [ln for ln in difflib.unified_diff(
        text.splitlines(), corrupted.splitlines(), lineterm="")
        if ln.startswith("+") and not ln.startswith("+++")]
    assert len(added) == 1, added
    assert entry["locate"] in added[0], added
    assert "Step dispatch" not in added[0], added
```

The two-pole tests, the x1 test and the `--only` test were added after those
and are read back by `read tests/unit/test_prose_corruption_catalog.py`
(`test_the_self_proof_fixture_is_green_on_a_damaged_live_tree`,
`test_the_live_tree_scan_names_the_rule_when_the_tree_is_damaged`,
`test_the_new_module_fixture_is_green_and_the_scan_names_the_module`,
`test_re_exempting_a_corpus_prompt_is_an_assertion_error_naming_it`,
`test_only_selects_one_corruption_and_applies_only_that_one`,
`test_the_spliced_line_scanner_is_silent_on_the_r6_false_positives`).
