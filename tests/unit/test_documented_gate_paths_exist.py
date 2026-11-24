"""Every path in a runnable tests/README command must exist on the tree.

The runbook's ```bash blocks are runnable commands; so are inline code spans
that start with a Python/pytest invocation or a tools/ script.  Prose, Python
snippets and option values (for example --basetemp=/tmp/xb) are not gate paths
and are deliberately ignored.  This guard never runs pytest recursively: it
parses text and calls Path.exists().
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "tests" / "README.md"

FENCED_BASH = re.compile(r"^```bash[^\n]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
COMMAND_HEADS = {"python", "python3", "pytest"}
PATH_SUFFIXES = (
    ".py", ".md", ".json", ".js", ".mjs", ".ts", ".sh", ".yaml", ".yml",
    ".toml", ".txt",
)


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return []


def runnable_commands(readme: str) -> list[list[str]]:
    """Extract only commands the runbook presents as runnable."""
    commands: list[list[str]] = []
    for block in FENCED_BASH.findall(readme):
        for line in block.splitlines():
            tokens = _tokens(line)
            if tokens:
                commands.append(tokens)
    for span in INLINE_CODE.findall(readme):
        tokens = _tokens(span)
        if not tokens:
            continue
        head = tokens[0]
        if head in COMMAND_HEADS or head.startswith("tools/"):
            commands.append(tokens)
    return commands


def documented_paths(commands: list[list[str]]) -> list[str]:
    """Repo-relative path arguments of the extracted commands, in first-seen order."""
    found: dict[str, None] = {}
    for argv in commands:
        for token in argv[1:]:
            if token.startswith(("-", "/", "$")) or "=" in token:
                continue  # flags, absolute temp paths, shell variables
            if token in {"&&", "|", ">", "<", ";", "..."}:
                continue
            if "/" not in token and not token.endswith(PATH_SUFFIXES):
                continue
            if "/" in token and not token.lower().endswith(PATH_SUFFIXES):
                continue
            found[token] = None
    return list(found)


def test_every_documented_gate_path_exists() -> None:
    commands = runnable_commands(README.read_text(encoding="utf-8"))
    assert len(commands) >= 5, "README command extraction went blank; guard is blind"
    paths = documented_paths(commands)
    assert len(paths) >= 3, "README path extraction went blank; guard is blind"
    missing = [path for path in paths if not (ROOT / path).exists()]
    assert not missing, "documented runnable path(s) missing from tree: " + ", ".join(missing)
