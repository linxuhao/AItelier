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

# K6: the bytes that were already corrupt in r2/r3, kept as they were.
K6_ENTRIES = (
    {"id": "K6a", "what": "r2 EN duplicated two-line run",
     "rev": R2, "block": "EN", "surface": "GUIDANCE_EN", "rule": "repeated_run"},
    {"id": "K6b", "what": "r2 ZH corruption",
     "rev": R2, "block": "ZH", "surface": "GUIDANCE_ZH", "rule": "repeated_run"},
    {"id": "K6c", "what": "duplicate banner in core/dpe_pipeline.py",
     "rev": R2, "path": "core/dpe_pipeline.py",
     "surface": "core/dpe_pipeline.py", "rule": "adjacent_duplicate_line"},
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
    text = apply_edits(read(entry["path"]), entry)
    if entry.get("block"):
        return _guidance(text, entry["block"])
    return text


def existing_surface_entries():
    """Entries that damage a surface the checker's corpus already carries."""
    return [entry for entry in CORRUPTIONS if entry["surface"] is not None]


def new_module_entries():
    """Entries that create a module under core/ instead of editing one."""
    return [entry for entry in CORRUPTIONS if entry["kind"] == "add"]
