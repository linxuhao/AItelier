"""git_history — read-only access to the repository's git history.

The reviewer's third source of evidence. The diff says what changed; the working
tree says what the code is now; only the history says what it USED to be and why
it stopped being that. Two questions come up constantly in review and neither of
the other two sources can answer either:

  * "Why is this line written this way?"      → blame, then show
  * "Is this change reintroducing something   → search (git's pickaxe), then show
     a past commit deliberately removed?"

Read-only by construction: every mode is a query. There is no argument that can
reach a command which writes, checks out, or moves a ref, because the git
argv is assembled here from an allowlisted mode and validated values — the
caller never supplies a git subcommand or a flag.

NO FALLBACK ROOT, deliberately. `project_root` arrives empty for a run that owns
no repository (a diff-only review; any `repo_mode: none` run launched without
`against_project`). Resolving that to the process's cwd is a documented trap in
this codebase: it is how a native tool ended up rooted at the CONTAINER's /app
and answered questions about AItelier's own source when asked about a project.
An absent repository is reported as an error naming the cause, so a reviewer is
told it has no history rather than being quietly shown somebody else's.
"""

import re
import subprocess
from pathlib import Path

_TIMEOUT = 30
_MAX_COUNT_CAP = 100
_MAX_CHARS = 24000          # matches the read tool's window; keeps a commit readable
_BLAME_MAX_LINES = 200
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _repo(project_root: str) -> Path:
    """The repository to query, or raise ValueError explaining why there is none."""
    if not (project_root or "").strip():
        raise ValueError(
            "no repository is available to this run, so it has no git history. "
            "A diff-only review sees only the diff it was given; relaunch with "
            "against_project=<project_id> to review against the codebase.")
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repository path does not exist: {root}")
    if not (root / ".git").exists():
        raise ValueError(f"not a git repository: {root}")
    return root


def _rel(path: str, *, required: bool) -> str:
    """Validate a repo-relative path. Rejects absolute, traversal and .git.

    Same strictness as repo_remove_file: reject rather than silently rewrite, so
    a caller that meant something else finds out instead of getting an answer
    about a different file.
    """
    p = (path or "").strip().replace("\\", "/")
    if not p:
        if required:
            raise ValueError("path is required for this mode")
        return ""
    if p.startswith("/") or Path(p).is_absolute():
        raise ValueError(f"path must be repo-relative, not absolute: {path!r}")
    parts = Path(p).parts
    if ".." in parts:
        raise ValueError(f"path escapes the repository: {path!r}")
    if parts and parts[0] == ".git":
        raise ValueError("the .git directory is not readable through this tool")
    return p


def _run(root: Path, argv: list[str]) -> str:
    """Run one git query. Never a shell, never a caller-supplied subcommand."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *argv],
            capture_output=True, text=True, timeout=_TIMEOUT)
    except FileNotFoundError:
        raise ValueError("git is not installed in this environment")
    except subprocess.TimeoutExpired:
        raise ValueError(f"git took longer than {_TIMEOUT}s and was stopped")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"git exited {proc.returncode}"
        raise ValueError(err.splitlines()[0][:400])
    return proc.stdout


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_CHARS:
        return text, False
    return text[:_MAX_CHARS], True


def _count(max_count) -> int:
    try:
        n = int(max_count) if max_count else 20
    except (TypeError, ValueError):
        n = 20
    return max(1, min(n, _MAX_COUNT_CAP))


def _commits(out: str) -> list[dict]:
    """Parse the fixed `%h\x1f%an\x1f%ad\x1f%s` format into rows."""
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) == 4:
            rows.append({"sha": parts[0], "author": parts[1],
                         "date": parts[2], "subject": parts[3]})
    return rows


_FMT = "--pretty=format:%h\x1f%an\x1f%ad\x1f%s"
_DATE = "--date=short"


def git_history(*, mode: str = "", path: str = "", query: str = "",
                sha: str = "", start_line: int = 0, end_line: int = 0,
                max_count: int = 0, project_root: str = "", **_ignored) -> dict:
    """Query the repository's history. Returns {mode, ...} or {error}."""
    try:
        root = _repo(project_root)
        m = (mode or "").strip().lower()

        if m == "log":
            rel = _rel(path, required=False)
            argv = ["log", _FMT, _DATE, f"-n{_count(max_count)}"]
            if rel:
                argv += ["--", rel]
            commits = _commits(_run(root, argv))
            return {"mode": "log", "path": rel, "commits": commits,
                    "count": len(commits)}

        if m == "search":
            q = (query or "").strip()
            if not q:
                raise ValueError("query is required for mode=search")
            # -S is git's pickaxe: commits where the OCCURRENCE COUNT of the
            # string changed, i.e. where it was introduced or removed. That is
            # the question being asked ("was this deliberately taken out?"), and
            # it is not the same as -G, which matches any diff line mentioning it.
            argv = ["log", _FMT, _DATE, f"-n{_count(max_count)}", "-S", q]
            rel = _rel(path, required=False)
            if rel:
                argv += ["--", rel]
            commits = _commits(_run(root, argv))
            return {"mode": "search", "query": q, "path": rel,
                    "commits": commits, "count": len(commits),
                    "note": ("commits that ADDED or REMOVED this string; empty "
                             "means it was never introduced or removed on this "
                             "history, not that it is absent from the tree")}

        if m == "blame":
            rel = _rel(path, required=True)
            try:
                start = max(1, int(start_line) if start_line else 1)
            except (TypeError, ValueError):
                start = 1
            try:
                end = int(end_line) if end_line else start + 40
            except (TypeError, ValueError):
                end = start + 40
            if end < start:
                end = start
            if end - start + 1 > _BLAME_MAX_LINES:
                end = start + _BLAME_MAX_LINES - 1
            out = _run(root, ["blame", "-L", f"{start},{end}",
                              "--date=short", "-w", "--", rel])
            text, truncated = _truncate(out)
            return {"mode": "blame", "path": rel, "start_line": start,
                    "end_line": end, "blame": text, "truncated": truncated}

        if m == "show":
            s = (sha or "").strip()
            if not s:
                raise ValueError("sha is required for mode=show")
            if not _SHA_RE.match(s):
                # Only a hex sha, so no argument can be read as a revision
                # expression or an option. Get one from log/search/blame.
                raise ValueError(
                    f"sha must be a hex commit id, got {sha!r}; take one from "
                    "mode=log, mode=search or mode=blame")
            out = _run(root, ["show", "--stat", "--patch", _DATE, s])
            text, truncated = _truncate(out)
            return {"mode": "show", "sha": s, "commit": text,
                    "truncated": truncated}

        raise ValueError(
            f"unknown mode {mode!r}; use one of: log, search, blame, show")

    except ValueError as e:
        return {"error": str(e)}
