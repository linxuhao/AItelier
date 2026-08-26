"""A truncated multi-file context entry must say what it dropped, and how to get it.

`{from: repository, path: "design/"}` concatenates a whole directory into ONE
context entry, each file behind a "### FILE: <relpath>" marker, in ALPHABETICAL order
— so which files survive the line budget is decided by their names.

Live, 2026-08-26: the game's design/ bundle ran 1871 lines against a 1500
budget. 40_ux_backlog.md, 90_decisions.md, 99_changelog.md and README.md were
dropped whole — the architect and the PM had never once seen the
design-decisions record — and nothing said a file was missing: the marker named
lines[0], the FIRST file, the one file guaranteed to be intact, and pointed at
`read(path='00_overview.md', start_line=1500)`: line 1500 of an 82-line file.

An agent can fail to page a file it was told about. It cannot want a file it has
never heard of.
"""
import pytest

from core.prompt_assembler import PromptAssembler, MAX_CONTEXT_LINES

clip = PromptAssembler._clip_context_entry
REPO = "Repository — design/"


def _file(name, n_lines, tag="x"):
    return f"### FILE: {name}\n" + "\n".join(f"{tag}{i}" for i in range(n_lines))


def _bundle(*specs):
    return "\n\n".join(_file(n, k) for n, k in specs)


# ── the untouched path ───────────────────────────────────────────────────────

def test_content_under_budget_is_untouched():
    body = "### FILE: a.md\n" + "\n".join(str(i) for i in range(10))
    assert clip(REPO, body) == body


def test_single_file_entry_keeps_its_hint():
    out = clip(REPO, _file("one.md", MAX_CONTEXT_LINES + 50))
    assert f"read(path='design/one.md', start_line={MAX_CONTEXT_LINES})" in out
    assert "未包含" not in out


# ── the manifest ─────────────────────────────────────────────────────────────

def test_every_file_is_named_even_when_its_content_is_gone():
    out = clip(REPO, _bundle(("00_a.md", 800), ("40_b.md", 800),
                             ("90_decisions.md", 50), ("99_changelog.md", 400)))
    for name in ("00_a.md", "40_b.md", "90_decisions.md", "99_changelog.md"):
        assert name in out, f"{name} is invisible to the agent"


def test_dropped_files_are_marked_and_given_a_plain_read():
    out = clip(REPO, _bundle(("00_a.md", 800), ("40_b.md", 800),
                             ("90_decisions.md", 50)))
    assert "✗ design/90_decisions.md" in out
    assert "read(path='design/90_decisions.md')" in out


def test_the_cut_file_is_given_its_own_line_offset():
    """The old hint gave the BUNDLE's line number, which overshoots the file."""
    out = clip(REPO, _bundle(("00_first.md", 900), ("40_middle.md", 900)))
    assert "read(path='design/40_middle.md', start_line=" in out
    line = int(out.split("read(path='design/40_middle.md', start_line=")[1]
               .split(")")[0])
    assert 0 < line < 900, f"start_line={line} is not inside 40_middle.md"


def test_paths_are_repo_relative_not_bundle_relative():
    """`read` resolves against the repo root — measured from the architect's own
    calls (scripts/…, playtest/…). A bundle-relative path is a second dead end."""
    out = clip(REPO, _bundle(("00_a.md", 900), ("90_decisions.md", 900)))
    assert "read(path='design/" in out
    assert "read(path='90_decisions.md'" not in out


def test_whole_files_are_marked_whole():
    out = clip(REPO, _bundle(("00_a.md", 100), ("40_b.md", 2000)))
    assert "✓ design/00_a.md" in out


# ── the budget still holds ───────────────────────────────────────────────────

def test_the_manifest_is_charged_against_the_budget():
    """Otherwise every added file quietly enlarges the prompt it was meant to bound."""
    out = clip(REPO, _bundle(*[(f"f{i:02d}.md", 200) for i in range(20)]))
    assert len(out.splitlines()) <= MAX_CONTEXT_LINES + 3


# ── the old bug ──────────────────────────────────────────────────────────────

def test_markdown_headings_are_not_mistaken_for_files():
    """The design docs are full of '### 7.1 属性 20 是捏人上限'. Treating those as
    file boundaries points the agent at read(path='7.1 属性 20 …')."""
    doc = ("### FILE: real.md\n"
           + "\n".join(["### 7.1 属性 20 是捏人上限", "prose"] * 800))
    out = clip(REPO, doc)
    assert "7.1" not in out.split("用 read 工具接着读：")[1]
    assert "read(path='design/real.md'" in out


def test_a_section_numbered_like_a_version_is_not_a_file():
    doc = "### FILE: real.md\n" + "\n".join(["### v1.2", "prose"] * 800)
    out = clip(REPO, doc)
    assert "read(path='design/v1.2'" not in out


def test_the_marker_still_says_the_remainder_exists():
    out = clip(REPO, _bundle(("a.md", 900), ("b.md", 900)))
    assert "剩余部分依然存在" in out


def test_a_step_entry_carries_its_source_so_read_can_find_it():
    """A step-source file lives in the PROMOTED dir, which a bare read() misses."""
    out = clip("Step 5 — verdict.md", _file("verdict.md", MAX_CONTEXT_LINES + 10))
    assert "source='step:5'" in out


def test_the_file_marker_is_explicit_so_nothing_has_to_be_guessed():
    """A bare `### <name>` cannot be told apart from a section heading in the
    CONTENT being concatenated, and every rule for guessing trades one failure
    for the other: require an extension and `### Dockerfile` goes invisible,
    accept any bare word and `### Notes` invents a file and splits a real one.
    Both were live here. The explicit token removes the question."""
    from core.prompt_assembler import _FILE_HEADER_RE

    for header, want in (
            ("### FILE: Dockerfile", "Dockerfile"),
            ("### FILE: LICENSE", "LICENSE"),
            ("### FILE: .gitignore", ".gitignore"),
            ("### FILE: design/40_ux_backlog.md", "design/40_ux_backlog.md"),
            ("### FILE: scripts/autoload/gm.gd", "scripts/autoload/gm.gd"),
            ("### FILE: a file with spaces.md", "a file with spaces.md")):
        m = _FILE_HEADER_RE.match(header)
        assert m and m.group(1) == want, header

    # Every one of these used to sit on one side or the other of the guess.
    for header in ("### Dockerfile", "### Notes", "### Overview", "### v1.2",
                   "### 记录", "### API design", "### design/40_ux.md",
                   "FILE: not-a-heading"):
        assert not _FILE_HEADER_RE.match(header), header


def test_the_emitters_on_both_sides_use_the_same_marker():
    """The marker is a contract between skillflow (which concatenates the files)
    and this module (which clips the result). A parser aligned to one emitter
    and not the other reads every bundle as a single unnamed blob.

    Read the INSTALLED skillflow, deliberately — the interesting failure is not
    "someone edited the checkout", it is "the deployed wheel is older than the
    parser". That is what shipped: 1.5.42 in the container emitting the bare
    marker while this module already required `FILE:`, with no manifest and no
    dropped-file rows to show for it. This assertion is what the pin
    (`skillflow-py>=1.5.43`) and `prompt_assembler._check_emitter_agrees` back
    up at install time and at runtime respectively.
    """
    import inspect
    import re
    from pathlib import Path

    from core import prompt_assembler

    ours = inspect.getsource(prompt_assembler)
    assert '"### FILE: ' in ours or "### FILE: " in ours

    sf = Path(inspect.getfile(__import__(
        "skillflow.context", fromlist=["x"]))).read_text(encoding="utf-8")
    assert '_FILE_MARKER = "### FILE: "' in sf, (
        "skillflow emits a different marker than this module parses")
    # Match the SHAPE, not one spelling: three of the five original emitters
    # used the local `rel`, but two spelled it inline as
    # `f"### {f.relative_to(step_dir)}"`. A guard that only knew the first form
    # let a revert in the second form through, silently.
    bare = re.findall(r'f"### \{', sf)
    assert not bare, (
        f"{len(bare)} bare-marker emitter(s) survived in skillflow — this "
        f"module's parser stops seeing file boundaries and clips bundles "
        f"without a manifest")


def test_a_marker_mismatch_is_reported_rather_than_absorbed():
    """The skew's whole danger is that it degrades SILENTLY, so the detector
    must speak. Drives it against a stand-in emitter, since the real one now
    agrees."""
    import sys
    import types

    from core import prompt_assembler

    real = sys.modules.get("skillflow.context")
    fake = types.ModuleType("skillflow.context")
    fake._FILE_MARKER = "### "                      # 1.5.42's emitter
    sys.modules["skillflow.context"] = fake
    try:
        msg = prompt_assembler._check_emitter_agrees()
    finally:
        if real is not None:
            sys.modules["skillflow.context"] = real
        else:
            del sys.modules["skillflow.context"]

    assert "1.5.43" in msg and "manifest" in msg, msg
    # And it stays quiet when they agree.
    assert prompt_assembler._check_emitter_agrees() == ""
