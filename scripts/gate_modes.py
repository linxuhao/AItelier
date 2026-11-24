#!/usr/bin/env python3
"""Compute the AItelier gate plan for a diff.

Stdlib only.  Printing the plan is the default and has no side effects; only
``--run`` executes the jobs, in order, with fail-fast.  Each job is a separate
entry with an argv and a reason, so the expensive JS suites are never buried
inside the serial Python suite.

Examples::

    python scripts/gate_modes.py --mode targeted --changed tests/unit/test_x.py
    python scripts/gate_modes.py --mode merge --changed core/ai_router.py
    python scripts/gate_modes.py --mode full --run

The targeted rule: every changed path must be under tests/, docs/ or design/,
be a Markdown file anywhere, or be a Python file whose executable AST is
identical before and after (docstrings normalised to ``ast.Pass``; comments do
not enter the AST).  Anything else refuses the targeted mode by name.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
PYTEST_PLAIN = ["python", "-m", "pytest", "tests"]

WHOLE_TREE_GUARDS = [
    "tests/unit/test_truncation_is_not_a_formatting_mistake.py",
    "tests/unit/test_prose_corruption_catalog.py",
]
DRIVER_NODE_SUITE = "tests/integration/test_driver_node_suite_runs.py"
WEB_VITEST_SUITE = "tests/integration/test_web_suite_runs.py"
ALLOWED_TREE_PREFIXES = ("tests/", "docs/", "design/")


def normalise(path: str) -> Optional[str]:
    """Return a repo-relative POSIX path, or None for an unsafe path."""
    candidate = path.strip().strip('"').replace("\\", "/")
    parsed = PurePosixPath(candidate)
    if not candidate or parsed.is_absolute() or ".." in parsed.parts:
        return None
    return parsed.as_posix()


def strip_docstrings(tree: ast.AST) -> ast.AST:
    """Replace docstrings with ``pass`` while leaving executable code intact."""
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body[0] = ast.Pass()
    return tree


def executable_ast(source: str) -> str:
    """Fingerprint executable Python; comments and docstrings are invisible."""
    return ast.dump(strip_docstrings(ast.parse(source)), include_attributes=False)


def git_source(path: str, base: str, root: Path) -> Optional[str]:
    """Read the pre-diff blob from git; None means the file did not exist."""
    proc = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def has_git_diff(path: str, base: str, root: Path) -> bool:
    """Whether a product Python path really differs from the base ref."""
    proc = subprocess.run(
        ["git", "diff", "--quiet", base, "--", path],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 1


def file_source(path: str, root: Path) -> Optional[str]:
    """Read the post-diff blob from the tree; None means the file is gone."""
    target = root / path
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="replace")


def classify_changed_paths(
    changed_paths: Iterable[str],
    *,
    root: Optional[Path] = None,
    base: str = "HEAD",
    old_sources: Optional[Mapping[str, Optional[str]]] = None,
    unproven_paths: Iterable[str] = (),
) -> list[str]:
    """Return the sorted paths that forbid the targeted mode.

    ``old_sources`` lets callers inject pre-diff blobs (value None = absent)
    instead of reading git.  Passing changed paths and blobs directly is the
    cheap, hermetic way to use this function; the CLI falls back to git.
    ``unproven_paths`` are product Python paths with no demonstrable diff: a
    bare path on a clean checkout cannot claim the comment-only exemption.
    """
    tree = Path(root) if root is not None else REPO_ROOT
    offenders: list[str] = []
    unproven = set(unproven_paths)
    for raw in sorted(set(changed_paths)):
        path = normalise(raw)
        if path is None:
            offenders.append(raw)
            continue
        if path in unproven:
            offenders.append(path)
            continue
        if path.startswith(ALLOWED_TREE_PREFIXES) or path.lower().endswith(".md"):
            continue
        if path.endswith(".py"):
            if old_sources is not None and path in old_sources:
                before = old_sources[path]
            else:
                before = git_source(path, base, tree)
            after = file_source(path, tree)
            if before is not None and after is not None:
                try:
                    if executable_ast(before) == executable_ast(after):
                        continue
                except (SyntaxError, ValueError):
                    # An unparsable blob cannot prove that only prose changed.
                    pass
        offenders.append(path)
    return offenders


def is_pytest_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return path.startswith("tests/") and path.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def job(name: str, argv: list[str], why: str) -> dict:
    return {"name": name, "argv": argv, "why": why}


def build_plan(
    mode: str,
    changed_paths: Iterable[str] = (),
    *,
    root: Optional[Path] = None,
    base: str = "HEAD",
    old_sources: Optional[Mapping[str, Optional[str]]] = None,
    unproven_paths: Iterable[str] = (),
) -> dict:
    """Build a JSON-serialisable gate plan; never executes anything."""
    paths = [normalise(p) or p for p in changed_paths]
    plan: dict = {"mode": mode, "changed_paths": paths, "allowed": True, "jobs": []}
    if mode not in ("targeted", "merge", "full"):
        plan["allowed"] = False
        plan["reason"] = f"unknown gate mode {mode!r}"
        return plan

    if mode == "targeted":
        offenders = classify_changed_paths(
            paths,
            root=root,
            base=base,
            old_sources=old_sources,
            unproven_paths=unproven_paths,
        )
        if offenders:
            plan["allowed"] = False
            plan["offending_path"] = offenders[0]
            plan["offending_paths"] = offenders
            plan["prescribed_mode"] = "merge"
            plan["reason"] = (
                f"{offenders[0]} changes executable code or is outside the "
                "tests/docs/design/Markdown allowlist; targeted mode refuses it. "
                "Prescribed mode: merge (or full)."
            )
            plan["jobs"] = []
            return plan

        affected = sorted({p for p in paths if p and is_pytest_file(p)})
        if affected:
            plan["jobs"].append(
                job(
                    "affected-tests",
                    PYTEST + affected,
                    "The changed pytest files themselves; explicit files, no deselection.",
                )
            )
        plan["jobs"].append(
            job(
                "whole-tree-guards",
                PYTEST + WHOLE_TREE_GUARDS,
                "Prose/structure guards read HEAD and every core module; a "
                "tests/docs-only diff can break them without touching their files.",
            )
        )
        return plan

    # merge and full keep the complete Python selection.  The two JS pytest
    # rows are exposed as their own jobs as well: no -m, --deselect, --ignore
    # or --deselect marker is applied to the Python tree.
    plan["jobs"].append(
        job(
            "python-tree",
            list(PYTEST_PLAIN),
            "Plain `pytest tests`: exactly the repository gate's selection; "
            "pytest.ini addopts and every marker selection are untouched.",
        )
    )
    plan["jobs"].append(
        job(
            "driver-node-suite",
            PYTEST + [DRIVER_NODE_SUITE],
            "Driver+director node suite is its own job (measured 370-895 s), "
            "not nested in the serial Python suite.",
        )
    )
    plan["jobs"].append(
        job(
            "web-vitest-suite",
            PYTEST + [WEB_VITEST_SUITE],
            "SPA vitest suite is its own job (measured 106-118 s).",
        )
    )
    return plan


def run_jobs(jobs: Iterable[dict]) -> int:
    for entry in jobs:
        print(f"+ {' '.join(entry['argv'])}", file=sys.stderr, flush=True)
        result = subprocess.run(entry["argv"], check=False)
        if result.returncode:
            print(
                f"gate job {entry['name']} failed with {result.returncode}",
                file=sys.stderr,
            )
            return result.returncode
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("targeted", "merge", "full"), default="targeted")
    parser.add_argument(
        "--changed",
        action="extend",
        nargs="+",
        default=[],
        metavar="PATH",
        help="changed repo-relative paths (repeatable)",
    )
    parser.add_argument("paths", nargs="*", help="changed paths, as positional arguments")
    parser.add_argument("--base", default="HEAD", help="git ref holding the pre-diff blobs")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository checkout")
    parser.add_argument(
        "--old-source",
        action="append",
        default=[],
        metavar="PATH=FILE|PATH=-",
        help="pre-diff blob for a path (FILE contents, or - for absent); skips git",
    )
    parser.add_argument(
        "--run", action="store_true", help="execute the jobs in order, fail-fast"
    )
    args = parser.parse_args(argv)

    old_sources: dict[str, Optional[str]] = {}
    for item in args.old_source:
        path, _, filename = item.partition("=")
        if not filename:
            parser.error(f"--old-source needs PATH=FILE or PATH=- : {item!r}")
        if filename == "-":
            old_sources[path] = None
        else:
            old_sources[path] = Path(filename).read_text(
                encoding="utf-8", errors="replace"
            )

    unproven: list[str] = []
    if args.mode == "targeted":
        for raw in [*args.changed, *args.paths]:
            path = normalise(raw)
            if (
                path
                and path.endswith(".py")
                and not path.startswith(ALLOWED_TREE_PREFIXES)
                and path not in old_sources
                and not has_git_diff(path, args.base, Path(args.root))
            ):
                unproven.append(path)

    plan = build_plan(
        args.mode,
        [*args.changed, *args.paths],
        root=Path(args.root),
        base=args.base,
        old_sources=old_sources,
        unproven_paths=unproven,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    if args.run and plan["allowed"]:
        return run_jobs(plan["jobs"])
    if args.run:
        print(plan["reason"], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
