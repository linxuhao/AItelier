"""Scan a range's ADDED lines for the three shapes a spliced edit produces.

An edit applied at the wrong column against a stale snapshot leaves one of
three mechanical traces in the added lines:

  two_statements_one_line
      a run of four or more spaces lands in the MIDDLE of a code line,
      because the indentation that belonged at the start of a swallowed
      continuation line ended up between two statements on one line. A run
      that opens a quoted string, that sits inside one, or that ends at a
      comment marker is aligned text, not a splice, so neither is reported;
  banner_glued
      a comment rule (───, ===, ***) ends before the line does and the next
      line's text is stuck to it, because the newline between them was eaten.
      Only a line that IS a comment is examined, so rule characters quoted
      inside ordinary prose are not this shape;
  sentence_split
      a paragraph line stops without terminal punctuation, an added blank
      line follows it, and the line after that resumes in lower case — a
      sentence cut in two by an inserted newline.

Run it against the added lines of any TRACKED range (an untracked new file is
not in `git diff`; stage it or diff it separately to scan it):

    python tools/prose_corruptions/scan_joined_lines.py <base-rev> [head-rev]

It prints one hit per offending added line with file, line number and shape,
then the added-line count and the hit count. A clean range prints `hits: 0`.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

BANNER = re.compile(r"(\u2500{3,}|-{5,}|={5,}|\*{5,}|#{4,})")
MID_LINE_GAP = re.compile(r"\S {4,}\S")
TERMINAL = re.compile(r"[.!?;:`\)\]\}\"']$")
CODE_HINT = re.compile(r"[=(]")


def added_lines(base, head):
    """{path: [(lineno, text)]} for every added line in the range."""
    rng = f"{base}..{head}" if head else base
    diff = subprocess.run(
        ["git", "diff", "--unified=0", rng],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    out = {}
    path = None
    lineno = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            lineno = int(match.group(1)) if match else None
            continue
        if path is None or lineno is None:
            continue
        if line.startswith("-"):
            continue
        if line.startswith("+"):
            out.setdefault(path, []).append((lineno, line[1:]))
            lineno += 1
    return out


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


def _sentence_split(lines, index):
    """True when this line was cut in two by an inserted blank line."""
    lineno, text = lines[index]
    stripped = text.strip()
    if not stripped or stripped.startswith(("#", "//", "*", ">", "|")):
        return False
    if TERMINAL.search(stripped):
        return False
    if index + 2 >= len(lines):
        return False
    blank_lineno, blank = lines[index + 1]
    next_lineno, following = lines[index + 2]
    if blank.strip() or blank_lineno != lineno + 1:
        return False
    if next_lineno != blank_lineno + 1:
        return False
    tail = following.strip()
    return bool(tail) and not tail[0].isupper() and following[:1] in (" ", "\t")


def scans(lines):
    """The shapes `lines` — [(lineno, text)] — exhibit, as (shape, lineno)."""
    hits = []
    for index, (lineno, text) in enumerate(lines):
        if _banner_glued(text):
            hits.append(("banner_glued", lineno))
        if _two_statements_one_line(text):
            hits.append(("two_statements_one_line", lineno))
        if _sentence_split(lines, index):
            hits.append(("sentence_split", lineno))
    return hits


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1"
    head = sys.argv[2] if len(sys.argv) > 2 else None
    per_file = added_lines(base, head)
    total = 0
    for path in sorted(per_file):
        for shape, lineno in scans(per_file[path]):
            print(f"{path}:{lineno}: {shape}")
            total += 1
    rng = f"{base}..{head}" if head else base
    print(f"command: git diff --unified=0 {rng}, added lines scanned")
    print(f"added lines: {sum(len(v) for v in per_file.values())}")
    print(f"hits: {total}")


if __name__ == "__main__":
    main()
