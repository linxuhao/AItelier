"""The corruption catalog is director-defined, applied as written.

Every corruption the director named lives in ONE module,
tools/prose_corruptions/catalog.py. This file proves the catalog is complete,
that an edit entry refuses bytes that do not occur exactly once, that each
entry which CREATES a new prompt module is caught by the constant-accounting
check naming that module, and that the runner and scanner this branch ships
are themselves measured.
"""

import importlib.util
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

NAMED_IDS = ("K1", "K2", "K3", "K4", "K5", "K6a", "K6b", "K6c", "K6d", "K6e")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOD = _load("tt_catalog_probe",
            "tests/unit/test_truncation_is_not_a_formatting_mistake.py")
CATALOG = _load("prose_corruption_catalog",
                "tools/prose_corruptions/catalog.py")
RUNNER = _load("prose_corruption_runner",
               "tools/prose_corruptions/run_corruptions.py")
SCAN = _load("prose_corruption_scan",
             "tools/prose_corruptions/scan_joined_lines.py")


def test_the_catalog_carries_every_named_corruption():
    ids = [entry["id"] for entry in CATALOG.CORRUPTIONS]
    assert sorted(ids) == sorted(NAMED_IDS), ids
    for entry in CATALOG.CORRUPTIONS:
        assert entry["what"], entry
        assert entry["rule"], entry


def test_each_edit_entry_refuses_a_missing_old_byte():
    """The catalog cannot drift: an `old` that is absent is an error."""
    for entry in CATALOG.CORRUPTIONS:
        if entry["kind"] != "edit":
            continue
        with pytest.raises(AssertionError, match="exactly once"):
            CATALOG.apply_edits("nothing like the old bytes here", entry)


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


def _head_core_tree(root):
    """`root/core` rebuilt from HEAD: names and bytes both committed."""
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD", "core"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert listing.returncode == 0, listing.stderr
    (root / "core").mkdir(parents=True, exist_ok=True)
    for line in listing.stdout.splitlines():
        rel = line.strip()
        if rel.endswith(".py"):
            (root / rel).write_text(MOD._head_read(rel), encoding="utf-8")


def _tree_with_new_module(tmp_path, entry):
    _head_core_tree(tmp_path)
    (tmp_path / entry["path"]).write_text(entry["content"], encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("entry", CATALOG.new_module_entries(),
                         ids=lambda e: e["id"])
def test_a_new_prompt_module_is_unaccounted_and_names_itself(tmp_path, entry):
    """K4/K5: a new module's prompt constant is red, naming module and name.

    The constant is matched by (module, name), so a new file declaring its own
    `SYSTEM_PROMPT` cannot borrow another file's exemption.
    """
    root = _tree_with_new_module(tmp_path, entry)
    missing = MOD._unaccounted_prose_constants(root)
    assert any(item.startswith(f"{entry['path']}:") for item in missing), missing


def test_an_exempt_name_in_another_module_is_still_red(tmp_path):
    """A keyed exemption is not a licence for the NAME anywhere.

    `SCHEMA` is exempt for specific state-schema modules. A brand new module
    that declares its own `SCHEMA` is a new surface and must be red, naming
    that module — the escape a bare-name table allowed.
    The core/ the new module lands in is HEAD's, so the check does not read
    the live worktree's bytes at all.
    """
    _head_core_tree(tmp_path)
    (tmp_path / "core" / "zz_borrowed_schema.py").write_text(
        'SCHEMA = """\nCREATE TABLE borrowed (\n'
        '    id INTEGER PRIMARY KEY,\n'
        '    body TEXT NOT NULL,\n'
        '    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n);\n"""\n',
        encoding="utf-8")
    missing = MOD._unaccounted_prose_constants(tmp_path)
    assert "core/zz_borrowed_schema.py:SCHEMA" in missing, missing
def test_a_rule_name_is_catalog_data_not_a_substring_of_the_prose():
    """The checker emits the catalog's name, never a word it found in prose.

    A substring rule (`"sha" in text`) is forbidden. This sets a canary name
    for the duplicate-line slot, corrupts core/dpe_pipeline.py in memory (one
    repeated banner line, which carries no rule-name text), and demands the
    CANARY name fire. It then points the slot at the real name and proves a
    surface that only *mentions* the canary word is not named by it: the rule
    name is read from data, never matched against the prose.
    """
    surface = "core/dpe_pipeline.py"
    entry = next(e for e in CATALOG.CORRUPTIONS if e["id"] == "K6c")
    clean = MOD._head_read(surface)
    corrupted = CATALOG.apply_to_text(clean, entry)
    MOD.PROSE_RULES.clear()
    try:
        MOD.PROSE_RULES["adjacent"] = "m12_duplicate_banner"
        violations = MOD._prose_violations(surface, corrupted)
        assert any("m12_duplicate_banner" in v and surface in v
                   for v in violations), violations
        assert not any("adjacent_duplicate_line" in v for v in violations)
        MOD.PROSE_RULES.clear()
        MOD.PROSE_RULES["adjacent"] = "adjacent_duplicate_line"
        mentions = (corrupted
                    + "\n# m12_duplicate_banner is only a word here\n")
        assert not any("m12_duplicate_banner" in v for v in
                       MOD._prose_violations("templates/x.md", mentions))
    finally:
        MOD.PROSE_RULES.clear()


def test_the_card_leaves_no_stray_prompt_module_or_probe():
    """The clean worktree carries none of the scratch files earlier rounds left.

    K4/K5 create `core/zz_new_prompt.py` and r5 left a `tests/unit/_tmp_git_probe.py`;
    neither may be committed. If one were on disk, the accounting check would
    read it as a real surface and the whole corruption study would measure a
    committed defect, not the catalog's planted one.
    """
    assert not (REPO_ROOT / "core" / "zz_new_prompt.py").exists()
    assert not (REPO_ROOT / "tests" / "unit" / "_tmp_git_probe.py").exists()



def test_the_agent_facing_prompt_constants_are_corpus_surfaces():
    """The prompts an agent reads are measured, not exempted."""
    corpus = MOD._prose_corpus()
    for name in MOD.PROSE_PROMPT_CONSTANTS:
        assert name in corpus, name
        exempted = {":".join(key) for key in MOD.PROSE_CONSTANT_EXEMPTIONS}
    for name in ("core/meta_agent.py:SYSTEM_PROMPT",
                 "core/state_driver_guide.py:STATE_DRIVER_GUIDE",
                 "core/meta_conversation.py:META_JSON_SCHEMA",
                 "core/meta_conversation.py:_INTENT_SCHEMA"):
        assert name not in exempted, name


def test_every_exemption_is_keyed_by_module_and_name_with_a_reason():
    for key, reason in MOD.PROSE_CONSTANT_EXEMPTIONS.items():
        assert isinstance(key, tuple) and len(key) == 2, key
        module, name = key
        assert module.startswith("core/") and module.endswith(".py"), key
        assert name and name.isidentifier(), key
        assert isinstance(reason, str) and reason.strip(), key
        assert (REPO_ROOT / module).exists(), key


def test_the_config_system_prompts_are_corpus_surfaces():
    """agent_configs system prompts are agent-facing prose in the corpus."""
    corpus = MOD._prose_corpus()
    names = [name for name in corpus
             if name.startswith("agent_configs/coding_task.yaml:")]
    assert names, sorted(corpus)
    for name in names:
        assert "system_prompt" not in name, name


def test_the_spliced_line_scanner_fires_on_the_splice_shapes():
    """Self-proof: the scanner used for this branch's added lines works.

    A scan that reports zero hits proves nothing unless the same scanner
    reports one on each shape. These samples are the shapes earlier rounds
    actually committed: two statements joined onto one line, and a sentence
    cut in two by a blank line.
    """
    joined = [
        (10, "    out = subprocess.run([\"git\", \"show\", "
             "f\"{rev}:{rel}\"],                         cwd=REPO_ROOT, "
             "capture_output=True, text=True)"),
    ]
    glued = [
        (30, "# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
             "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
             "Step dispatch"),
    ]
    split = [
        (20, "    prefer `references` and quote the `sha` from its"),
        (21, ""),
        (22, "    `citation` swapped back for the legacy advice."),
    ]
    assert "two_statements_one_line" in [s for s, _ in SCAN.scans(joined)]
    assert "banner_glued" in [s for s, _ in SCAN.scans(glued)]
    assert "sentence_split" in [s for s, _ in SCAN.scans(split)]


def test_the_spliced_line_scanner_is_silent_on_clean_lines():
    clean = [
        (1, "    out = subprocess.run([\"git\", \"show\", \"HEAD\"],"),
        (2, "                         cwd=REPO_ROOT, capture_output=True)"),
        (3, ""),
        (4, "# \u2500\u2500 The apply_patch grant boundary "
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500"),
        (5, "# `_exec_tool` refuses apply_patch unless the schema has it."),
    ]
    assert SCAN.scans(clean) == [], SCAN.scans(clean)


def test_the_spliced_line_scanner_is_silent_on_the_r6_false_positives():
    """The nine lines the r6 scanner named on this branch's own range.

    Every one is an alignment space run inside a quoted literal, an aligned
    trailing comment, or rule characters quoted inside a sentence of prose.
    Each is a real line of the 527beafa..7c43f6a5 diff and must be silent,
    while the shapes above still fire.
    """
    false_positives = [
        (147, "        (10, \"    out = subprocess.run([\\\"git\\\", "
              "\\\"show\\\", \""),
        (148, "             \"f\\\"{rev}:{rel}\\\"],                         "
              "cwd=REPO_ROOT, \""),
        (152, "        (20, \"    prefer `references` and quote the `sha` "
              "from its\"),"),
        (154, "        (22, \"    `citation` swapped back for the legacy "
              "advice.\"),"),
        (162, "        (1, \"    out = subprocess.run([\\\"git\\\", "
              "\\\"show\\\", \\\"HEAD\\\"],\"),"),
        (163, "        (2, \"                         cwd=REPO_ROOT, "
              "capture_output=True)\"),"),
        (25, "R2 = \"3b3d6560\"                                    "
             "# r2 candidate"),
        (26, "R3 = \"fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722\"    "
             "# r3 candidate"),
        (11, "      a comment rule (\u2500\u2500\u2500, ===, ***) ends before "
             "the line does and the next"),
    ]
    assert SCAN.scans(false_positives) == [], SCAN.scans(false_positives)


def test_the_runner_copies_the_tree_and_does_not_mutate_the_source(tmp_path):
    """`_copy_tree` carries the catalog into the copy; `_apply` is confined.

    The dest half of the assertion compares against HEAD through the catalog
    itself, so it stays meaningful while a corruption run has the live tree
    damaged; only the unchanged-source half reads the live worktree, because
    that is the thing it must see unchanged.
    """
    dest = tmp_path / "copy"
    RUNNER._copy_tree(dest)
    assert (dest / "tools" / "prose_corruptions" / "catalog.py").exists()
    assert (dest / "tests" / "unit"
            / "test_prose_corruption_catalog.py").exists()
    entry = next(e for e in CATALOG.CORRUPTIONS if e["id"] == "K1")
    before = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
    RUNNER._apply(entry, dest, CATALOG)
    head_text = MOD._head_read(entry["path"])
    assert (dest / entry["path"]).read_text(encoding="utf-8") == \
        CATALOG.apply_to_text(head_text, entry)
    assert (REPO_ROOT / entry["path"]).read_text(encoding="utf-8") == before


def test_the_catalog_names_revisions_the_runner_can_reach():
    """A fixture quoting an old revision needs that revision fetcheable."""
    reachable = [rev[:8] for rev in RUNNER.HISTORY_REVS]
    for entry in CATALOG.CORRUPTIONS:
        if entry.get("rev"):
            assert entry["rev"][:8] in reachable, entry["id"]

# ── The corruptions are reproduced in isolation and fire their rule ──────
#
# Earlier rounds measured each corruption by running the suite in a worktree
# the runner had ALREADY corrupted, so the test's "clean" copy was the damaged
# file: most rows went red on a missing-bytes precondition, not on detection.
# These checks apply each catalog entry to a clean in-memory copy, never touch
# disk, and demand the named RULE fire — so every row is a detection result and
# the empty-mutation control (the same surfaces intact) is green by the assert
# that the clean copy carries no violation.
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


def test_the_empty_mutation_leaves_every_surface_clean():
    """No corruption applied: every corpus surface carries no violation."""
    corpus = MOD._prose_corpus()
    survivors = {name: MOD._prose_violations(name, text)
                 for name, text in corpus.items()
                 if MOD._prose_violations(name, text)}
    assert not survivors, survivors


# ── The self-proof fixtures and the live-tree tripwire are two things ────
#
# The self-proof fixtures take their clean pole from HEAD, which the runner
# never commits to, so they stay green while a corruption run has the live
# tree damaged. The live-tree scan takes its bytes from the worktree, so it is
# the thing that goes red and names the rule. The pair below drives both from
# ONE simulated damage per corruption: `_read` is monkeypatched to return the
# runner's bytes, then (a) the fixture's clean pole is asserted intact and its
# detection still fires from HEAD, and (b) the live-tree corpus is asserted
# red, naming the surface and the catalog's rule.


def _damaged_live_read(entry):
    """A `_read` that returns the runner's damaged bytes for `entry`'s file."""
    damaged = CATALOG.corrupt_file_text(entry, MOD._head_read, MOD._git_file)
    target = CATALOG.entry_file_path(entry)

    def read(rel):
        return damaged if rel == target else MOD._head_read(rel)

    return read


@pytest.mark.parametrize("entry", CATALOG.existing_surface_entries(),
                         ids=lambda e: e["id"])
def test_the_self_proof_fixture_is_green_on_a_damaged_live_tree(monkeypatch,
                                                               entry):
    """Pole ② for a fixture: the live tree is damaged, the fixture is green.

    `_read` returns the runner's bytes, so anything reading the live tree sees
    the corruption. The fixture reads HEAD and must still find its clean pole
    intact and its corrupted pole red — it measures detection, not a
    precondition.
    """
    surface = CATALOG.surface_name(entry)
    monkeypatch.setattr(MOD, "_read", _damaged_live_read(entry))
    clean = CATALOG.clean_surface_text(entry, MOD._head_read)
    assert not MOD._prose_violations(surface, clean), (surface, "clean pole")
    corrupted = CATALOG.surface_text(entry, MOD._head_read, MOD._git_file)
    assert corrupted != clean, entry["id"]
    assert any(entry["rule"] in v and surface in v
               for v in MOD._prose_violations(surface, corrupted)), entry["id"]


@pytest.mark.parametrize("entry", CATALOG.existing_surface_entries(),
                         ids=lambda e: e["id"])
def test_the_live_tree_scan_names_the_rule_when_the_tree_is_damaged(
        monkeypatch, entry):
    """Pole ② for the tripwire: the live scan is red and names the rule.

    The corpus is derived through the damaged `_read`, so the surface the
    fixture just proved green is the surface that goes red here.
    """
    surface = CATALOG.surface_name(entry)
    monkeypatch.setattr(MOD, "_read", _damaged_live_read(entry))
    corpus = MOD._prose_corpus()
    assert surface in corpus, sorted(corpus)
    violations = MOD._prose_violations(surface, corpus[surface])
    assert violations, (entry["id"], surface)
    assert any(entry["rule"] in v and surface in v for v in violations), \
        (entry["id"], entry["rule"], violations)


def test_the_new_module_fixture_is_green_and_the_scan_names_the_module(
        monkeypatch, tmp_path):
    """K4/K5: the fixture builds a tree; the scan names the module.

    A new-module corruption has no surface in the live corpus, so its fixture
    builds its own tree. With `_read` damaged that fixture is unaffected, and
    the accounting scan on the tree names `core/zz_new_prompt.py`.
    """
    for entry in CATALOG.new_module_entries():
        monkeypatch.setattr(MOD, "_read",
                            lambda rel: "damaged live bytes")
        root = tmp_path / entry["id"]
        _head_core_tree(root)
        (root / entry["path"]).write_text(entry["content"], encoding="utf-8")
        missing = MOD._unaccounted_prose_constants(root)
        assert any(item.startswith(f"{entry['path']}:") for item in missing), \
            (entry["id"], missing)


def test_re_exempting_a_corpus_prompt_is_an_assertion_error_naming_it(
        monkeypatch):
    """Mutation x1: the named assertion fails, never an unbound identifier.

    x1 puts `core/meta_conversation.py:META_JSON_SCHEMA` back into the
    exemption table and runs the real check. It must raise AssertionError
    naming that constant; a misspelt message variable would raise NameError
    instead and report nothing about the constant.
    """
    name = "core/meta_conversation.py:META_JSON_SCHEMA"
    monkeypatch.setitem(MOD.PROSE_CONSTANT_EXEMPTIONS,
                        ("core/meta_conversation.py", "META_JSON_SCHEMA"),
                        "re-exempted by mutation x1")
    assert name in MOD._prose_corpus(), name
    with pytest.raises(AssertionError, match="META_JSON_SCHEMA") as caught:
        test_the_agent_facing_prompt_constants_are_corpus_surfaces()
    assert not isinstance(caught.value, NameError)


# ── The runner can run one corruption without running the whole catalog ──


def test_only_selects_one_corruption_and_applies_only_that_one(tmp_path):
    """`--only K3` plans and applies exactly one entry, never the rest.

    Proved from the plan and the applied bytes, so it does not need the whole
    suite: an unrelated file that a different corruption would rewrite is left
    byte-identical.
    """
    every = RUNNER.select_entries(CATALOG, ())
    assert [e["id"] for e in every] == [e["id"] for e in CATALOG.CORRUPTIONS]
    selected = RUNNER.select_entries(CATALOG, ("K3",))
    assert [e["id"] for e in selected] == ["K3"]
    with pytest.raises(AssertionError, match="no such corruption"):
        RUNNER.select_entries(CATALOG, ("K99",))

    dest = tmp_path / "tree"
    rels = sorted({CATALOG.entry_file_path(e) for e in CATALOG.CORRUPTIONS
                   if e["kind"] != "add"})
    before = {}
    for rel in rels:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        before[rel] = MOD._head_read(rel)
        target.write_text(before[rel], encoding="utf-8")
    for entry in selected:
        RUNNER._apply(entry, dest, CATALOG)
    changed = sorted(rel for rel in before
                     if (dest / rel).read_text(encoding="utf-8") != before[rel])
    assert changed == ["core/output_migration.py"], changed


def test_the_empty_control_applies_no_corruption(tmp_path, monkeypatch):
    """`control=True` runs the same selection with NOTHING applied.

    This is the true empty-mutation control: one plan row, `_apply` never
    called, so no path in the throwaway tree is touched. Proved by making
    `_apply` fail the run if it is ever reached, and by the recorded row.
    """
    def refuse(entry, worktree, catalog):
        raise AssertionError(f"the control applied {entry['id']}")

    monkeypatch.setattr(RUNNER, "_apply", refuse)
    monkeypatch.setattr(RUNNER, "_copy_tree",
                        lambda dest, rev=None: dest.mkdir(parents=True,
                                                          exist_ok=True))
    monkeypatch.setattr(RUNNER, "_bare_run", lambda command, cwd: (0, "ok"))
    monkeypatch.setattr(RUNNER, "_git",
                        lambda args, cwd: SimpleNamespace(stdout="",
                                                          returncode=0,
                                                          stderr=""))
    rows = RUNNER.run_all(targets=("tests/unit/test_a.py",
                                   "tests/unit/test_b.py"), control=True)
    assert [row["id"] for row in rows] == ["CONTROL"], rows
    assert rows[0]["rc"] == 0, rows
    assert rows[0]["changed"] == "", rows
    assert rows[0]["selection"] == ("tests/unit/test_a.py "
                                    "tests/unit/test_b.py"), rows


@pytest.mark.parametrize("entry", CATALOG.CORRUPTIONS,
                         ids=lambda e: e["id"])
def test_the_catalog_list_stays_green_on_a_damaged_live_tree(monkeypatch,
                                                             tmp_path,
                                                             entry):
    """Pole for the round-9 list, catalog half: every listed test green.

    `_read` returns the runner's damaged bytes; the tests on this file's
    list take their bytes from HEAD and must stay green for ALL ten K
    entries, new-module ones included.
    """
    monkeypatch.setattr(MOD, "_read", _damaged_live_read(entry))
    test_each_edit_entry_applies_to_its_own_surface()
    test_k6c_duplicates_exactly_one_line_and_overwrites_nothing()
    test_a_rule_name_is_catalog_data_not_a_substring_of_the_prose()
    test_the_runner_copies_the_tree_and_does_not_mutate_the_source(tmp_path)
    if entry["kind"] == "add":
        test_a_new_prompt_module_is_unaccounted_and_names_itself(
            tmp_path / entry["id"], entry)
