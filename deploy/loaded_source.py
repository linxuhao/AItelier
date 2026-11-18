#!/usr/bin/env python3
"""Check that a RUNNING backend serves the source bytes a checkout holds.

WHY THIS EXISTS
---------------
``/health`` reports ``source_identity``: the checkout root a serving process
imported from, a digest over the covered modules' files captured ONCE at import,
and when that capture happened. Until this module existed, nothing read it, so
"which source bytes is the backend actually running?" was still answered by
eyeballing a disk tree - which cannot tell a process that imported the new commit
from one that is still serving the old bytes.

This is that field's consumer: one command that fetches the answer, recomputes
the digest over the file list THE ANSWER reports (never a local constant, so it
checks what the process claims rather than what this host believes), and compares
the two. The recomputation calls ``core.source_identity.compute_digest`` - the
same implementation api/main.py captures with - so there is no second copy of the
rule here to drift from it. api.main is deliberately NOT imported: a verifier that
loaded the app would only prove something about itself.

USAGE
-----
    python -m deploy.loaded_source --url http://127.0.0.1:4444/health --tree <checkout>
    python -m deploy.loaded_source --url http://127.0.0.1:4444/health --tree <checkout> --json

- ``--url``  the /health endpoint to ask.
- ``--tree`` the checkout to check against, usually the commit meant to be
  deployed. Covered files are read at their answer-relative paths under it.
- ``--json`` print the same report as one JSON object on stdout instead of text.
- ``--timeout`` seconds to wait for the answer (default 10).

WHAT A MISMATCH NAMES
---------------------
The reported digest alone cannot localize a difference - it is one hash over
every file - so on a mismatch the answer's OWN root is read for the same covered
files and each file whose bytes differ from ``--tree`` is named, as is a covered
file missing from ``--tree``. When the reported root is not readable from this
host (the deployed checkout is not mounted here) the mismatch still stands and the
files are reported as unnameable, with the reason. Nothing is guessed.

EXIT CODES
----------
0  match: the tree's bytes reproduce the digest the process reports.
1  mismatch: they do not. The differing files are named when they can be.
2  unverifiable: the URL could not be reached, answered something that is not
   JSON, or answered without a usable ``source_identity`` block - including an
   answer that itself declares it could not read its own sources. A missing,
   empty, unsafe (absolute or ``..``) file list is refused the same way.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import sys
import urllib.error
import urllib.request

from core.source_identity import SourceUnreadable, compute_digest

EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_UNVERIFIABLE = 2

DEFAULT_TIMEOUT = 10.0

_HEX_DIGITS = frozenset("0123456789abcdef")


class Unverifiable(Exception):
    """The answer cannot be checked at all (as opposed to being checked and wrong)."""


def _safe_relative(rel: str) -> str:
    """Reject a reported path that would escape the tree it is joined to."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise Unverifiable(f"source_identity.files names a path outside the tree: {rel!r}")
    return rel


def fetch_identity(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """The answer's ``source_identity`` block, or Unverifiable saying why not."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise Unverifiable(f"could not reach {url}: {type(exc).__name__}: {exc}") from exc
    try:
        answer = json.loads(body)
    except ValueError as exc:
        raise Unverifiable(f"{url} did not answer JSON: {exc}") from exc
    if not isinstance(answer, dict):
        raise Unverifiable(f"{url} answered {type(answer).__name__}, not a JSON object")
    identity = answer.get("source_identity")
    if not isinstance(identity, dict):
        raise Unverifiable(f"the answer from {url} carries no source_identity block")

    files = identity.get("files")
    if (not isinstance(files, list) or not files
            or not all(isinstance(rel, str) and rel for rel in files)):
        raise Unverifiable("source_identity.files is not a non-empty list of paths")
    for rel in files:
        _safe_relative(rel)

    digest = identity.get("digest")
    if digest is None:
        raise Unverifiable(
            "the process declares it could not read its own sources: "
            + str(identity.get("reason")))
    if (not isinstance(digest, str) or len(digest) != 64
            or not set(digest) <= _HEX_DIGITS):
        raise Unverifiable("source_identity.digest is not a lowercase hex sha256")

    root = identity.get("root")
    if not isinstance(root, str) or not root:
        raise Unverifiable("source_identity.root is not a path")
    return identity


def _differing_files(tree: Path, reported_root: str, files) -> tuple[list[str] | None, str | None]:
    """Covered files whose BYTES differ between ``tree`` and the process's root.

    Returns ``(differing, reason)``: ``differing`` is None when the comparison
    could not be made at all, and ``reason`` says why (or None). Byte comparison,
    not hashing: the rule's loop must not be copied here.
    """
    process_tree = Path(reported_root)
    if not process_tree.is_dir():
        return None, (f"the reported root {reported_root} is not readable from this "
                      "host, so the differing files cannot be named")
    if process_tree.resolve() == tree.resolve():
        return None, (f"the reported root IS the checker's tree ({tree}), so the bytes "
                      "here are the process's only if they were unchanged since import")
    differing = []
    for rel in files:
        try:
            ours = (tree / rel).read_bytes()
        except OSError:
            differing.append(rel)       # missing from the checked tree: a difference
            continue
        try:
            theirs = (process_tree / rel).read_bytes()
        except OSError as exc:
            return None, (f"could not read {process_tree / rel} at the reported root: "
                          f"{type(exc).__name__}")
        if ours != theirs:
            differing.append(rel)
    return differing, None


def verify(url: str, tree, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch the answer and check it against ``tree``; a report, never an exit code."""
    tree = Path(tree)
    identity = fetch_identity(url, timeout)
    files = tuple(identity["files"])
    report = {"url": url, "tree": str(tree), "reported_root": identity["root"],
              "captured_at": identity.get("captured_at"),
              "reported_digest": identity["digest"],
              "recomputed_digest": None, "files": list(files),
              "status": None, "differing_files": None, "differing_reason": None}
    if not tree.is_dir():
        raise Unverifiable(f"--tree {tree} is not a directory")
    try:
        report["recomputed_digest"] = compute_digest(tree, files)
    except SourceUnreadable as exc:
        # The tree cannot reproduce the digest because a covered file is unreadable
        # or missing there. That is a real difference, named below, not an
        # unverifiable answer.
        missing = str(exc.path.relative_to(tree)) if exc.path.is_relative_to(tree) else str(exc.path)
        report["status"] = "mismatch"
        report["differing_files"] = [missing]
        report["differing_reason"] = str(exc)
        return report

    if report["recomputed_digest"] == report["reported_digest"]:
        report["status"] = "match"
        return report
    report["status"] = "mismatch"
    differing, reason = _differing_files(tree, report["reported_root"], files)
    report["differing_files"] = differing
    report["differing_reason"] = reason
    return report


def render_text(report: dict) -> str:
    lines = [f"loaded source: {report['url']}",
             f"  reported root    : {report['reported_root']}",
             f"  captured_at      : {report['captured_at']}",
             f"  files covered    : {', '.join(report['files'])}",
             f"  reported digest  : {report['reported_digest']}",
             f"  recomputed digest: {report['recomputed_digest']}  (tree {report['tree']})"]
    if report["status"] == "match":
        lines.append("verdict: MATCH - the tree's bytes reproduce the digest the "
                     "process reports")
        return "\n".join(lines)
    lines.append("verdict: MISMATCH - the tree's bytes do NOT reproduce the digest "
                 "the process reports")
    if report["differing_files"]:
        lines.append("  differing files  : " + ", ".join(report["differing_files"]))
    else:
        lines.append("  differing files  : cannot be named: " + str(report["differing_reason"]))
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deploy.loaded_source",
        description=(
            "Check that a running backend's /health source_identity digest matches "
            "the source bytes in a checkout. Exits 0 on match, 1 on mismatch, "
            "2 when the answer cannot be checked."
        ),
        epilog=(
            "The covered file list comes from the answer itself; the digest is "
            "recomputed with core.source_identity.compute_digest."
        ),
    )
    parser.add_argument("--url", required=True,
                        help="/health endpoint of the running backend")
    parser.add_argument("--tree", required=True,
                        help="checkout to check against (covered files are read "
                             "at their answer-relative paths under it)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        metavar="SECONDS", help="timeout for the /health request")
    parser.add_argument("--json", action="store_true",
                        help="print the report as one JSON object instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        report = verify(args.url, args.tree, timeout=args.timeout)
    except Unverifiable as exc:
        if args.json:
            print(json.dumps({"url": args.url, "tree": str(args.tree),
                              "status": "unverifiable", "error": str(exc)},
                             indent=2, sort_keys=True))
        print(f"loaded_source: {exc}", file=sys.stderr)
        return EXIT_UNVERIFIABLE
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return EXIT_MATCH if report["status"] == "match" else EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
