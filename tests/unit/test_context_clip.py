"""A truncated multi-file context entry must say what it dropped, and how to get it.

`{from: repository, path: "design/"}` concatenates a whole directory into ONE
context entry, each file behind a "### <relpath>" header, in ALPHABETICAL order
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
    return f"### {name}\n" + "\n".join(f"{tag}{i}" for i in range(n_lines))


def _bundle(*specs):
    return "\n\n".join(_file(n, k) for n, k in specs)


# ── the untouched path ───────────────────────────────────────────────────────

def test_content_under_budget_is_untouched():
    body = "### a.md\n" + "\n".join(str(i) for i in range(10))
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
    doc = ("### real.md\n"
           + "\n".join(["### 7.1 属性 20 是捏人上限", "prose"] * 800))
    out = clip(REPO, doc)
    assert "7.1" not in out.split("用 read 工具接着读：")[1]
    assert "read(path='design/real.md'" in out


def test_a_section_numbered_like_a_version_is_not_a_file():
    doc = "### real.md\n" + "\n".join(["### v1.2", "prose"] * 800)
    out = clip(REPO, doc)
    assert "read(path='design/v1.2'" not in out


def test_the_marker_still_says_the_remainder_exists():
    out = clip(REPO, _bundle(("a.md", 900), ("b.md", 900)))
    assert "剩余部分依然存在" in out


def test_a_step_entry_carries_its_source_so_read_can_find_it():
    """A step-source file lives in the PROMOTED dir, which a bare read() misses."""
    out = clip("Step 5 — verdict.md", _file("verdict.md", MAX_CONTEXT_LINES + 10))
    assert "source='step:5'" in out


def test_extensionless_bundle_members_are_visible_to_the_manifest():
    """Dockerfile / LICENSE / README are exactly what a repository bundle holds.

    Requiring an extension folded their content into the PRECEDING file's span
    — inflating its reported length and its whole/cut verdict — and left the
    file itself out of the manifest entirely, reproducing the "a file the agent
    never heard of" failure the manifest exists to prevent.
    """
    from core.prompt_assembler import _FILE_HEADER_RE

    for header in ("### Dockerfile", "### LICENSE", "### README",
                   "### .gitignore", "### Makefile",
                   "### design/40_ux_backlog.md", "### scripts/autoload/gm.gd"):
        assert _FILE_HEADER_RE.match(header), header


def test_prose_headings_are_not_read_as_file_boundaries():
    """A false boundary splits a real file's span and invents a manifest row.

    Single-token rules out English prose ("### API design"); ASCII-only rules
    out the Chinese section headings these very design docs use.
    """
    from core.prompt_assembler import _FILE_HEADER_RE

    for header in ("### API design", "### 记录", "### 队列", "### a b c",
                   "### Why six layers",
                   # a dotted token whose last segment is numeric is a version,
                   # not a filename — the one thing the original pattern got right
                   "### v1.2", "### 1.5.42"):
        assert not _FILE_HEADER_RE.match(header), header
