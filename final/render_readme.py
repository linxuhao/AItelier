"""Fill final/README.md's mutation table from the runner's own report, then
check every citation in the README.

The table rows are copied verbatim from final/logs/mutations/summary.txt
(written by `run_mutations.py --report`); only the log column is widened to
the in-tree path. A citation is a code span followed by ` in ` and a code
span naming a file under final/logs/; the snippet must occur verbatim in
that file. Exit 1 if any citation is missing.

Run from the tree root: python3 final/render_readme.py
"""
import re
import sys
from pathlib import Path

README = Path("final/README.md")
SUMMARY = Path("final/logs/mutations/summary.txt")
BEGIN = "<!-- BEGIN final/logs/mutations/summary.txt -->"
END = "<!-- END final/logs/mutations/summary.txt -->"

text = README.read_text(encoding="utf-8")
rows = [line for line in SUMMARY.read_text(encoding="utf-8").splitlines()
        if line.startswith("|")]
rows = [re.sub(r"`([A-Za-z0-9_]+\.txt)` \|$", r"`final/logs/mutations/\1` |", row)
        for row in rows]
head, rest = text.split(BEGIN, 1)
_, tail = rest.split(END, 1)
text = head + BEGIN + "\n" + "\n".join(rows) + "\n" + END + tail
README.write_text(text, encoding="utf-8")
print("table_rows_written", len(rows))

missing = 0
pairs = re.findall(r"`([^`\n]+)` in `(final/logs/[^`\n]+)`", text)
for snippet, path in pairs:
    log = Path(path)
    found = log.is_file() and snippet in log.read_text(encoding="utf-8",
                                                       errors="replace")
    missing += not found
    print("FOUND  " if found else "MISSING", path, repr(snippet))
print("citations_checked", len(pairs), "missing", missing)
sys.exit(1 if missing else 0)
