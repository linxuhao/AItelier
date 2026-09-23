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
    """Applied to the real surface, every edit changes the file."""
    for entry in CATALOG.CORRUPTIONS:
        if entry["kind"] != "edit":
            continue
        text = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
        assert CATALOG.apply_edits(text, entry) != text, entry["id"]


def _tree_with_new_module(tmp_path, entry):
    core = tmp_path / "core"
    shutil.copytree(REPO_ROOT / "core", core,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (core / Path(entry["path"]).name).write_text(entry["content"],
                                                 encoding="utf-8")
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
    """
    core = tmp_path / "core"
    shutil.copytree(REPO_ROOT / "core", core,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (core / "zz_borrowed_schema.py").write_text(
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
    clean = MOD._prose_corpus()[surface]
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
        assert name not in exempted, namee


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
    split = [
        (20, "    prefer `references` and quote the `sha` from its"),
        (21, ""),
        (22, "    `citation` swapped back for the legacy advice."),
    ]
    assert "two_statements_one_line" in [s for s, _ in SCAN.scans(joined)]
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


def test_the_runner_copies_the_tree_and_does_not_mutate_the_source(tmp_path):
    """`_copy_tree` carries the catalog into the copy; `_apply` is confined."""
    dest = tmp_path / "copy"
    RUNNER._copy_tree(dest)
    assert (dest / "tools" / "prose_corruptions" / "catalog.py").exists()
    assert (dest / "tests" / "unit"
            / "test_prose_corruption_catalog.py").exists()
    entry = next(e for e in CATALOG.CORRUPTIONS if e["id"] == "K1")
    before = (REPO_ROOT / entry["path"]).read_text(encoding="utf-8")
    RUNNER._apply(entry, dest, CATALOG)
    assert (dest / entry["path"]).read_text(encoding="utf-8") != before
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
    corpus = MOD._prose_corpus()
    clean = corpus[surface]
    corrupted = CATALOG.surface_text(entry, MOD._read, MOD._git_file)
    assert corrupted != clean, f"{entry['id']}: the fixture is not corrupt"
    violations = MOD._prose_violations(surface, corrupted)
    assert violations, f"{entry['id']}: no violation on the corrupted {surface}"
    assert any(entry["rule"] in v for v in violations), \
        (entry["id"], entry["rule"], violations)
    assert any(surface in v for v in violations), violations
    assert not MOD._prose_violations(surface, clean), (surface, "clean is red")


def test_k6c_duplicates_exactly_one_line_and_overwrites_nothing():
    """K6c is the m12 shape: one repeated banner line, not a whole-file swap."""
    import difflib
    entry = next(e for e in CATALOG.CORRUPTIONS if e["id"] == "K6c")
    text = (REPO_ROOT / "core" / "dpe_pipeline.py").read_text(encoding="utf-8")
    corrupted = CATALOG.apply_to_text(text, entry)
    assert len(corrupted.splitlines()) == len(text.splitlines()) + 1
    added = [ln for ln in difflib.unified_diff(
        text.splitlines(), corrupted.splitlines(), lineterm="")
        if ln.startswith("+") and not ln.startswith("+++")]
    assert len(added) == 1, added
    assert entry["locate"] in added[0], added


def test_the_empty_mutation_leaves_every_surface_clean():
    """No corruption applied: every corpus surface carries no violation."""
    corpus = MOD._prose_corpus()
    survivors = {name: MOD._prose_violations(name, text)
                 for name, text in corpus.items()
                 if MOD._prose_violations(name, text)}
    assert not survivors, survivors
