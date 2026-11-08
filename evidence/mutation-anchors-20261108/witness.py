"""Witness driver for the four mutations invalidated by the raised literals.

For each NAME given on the command line:
  1. build a fresh scratch tree OUTSIDE the worktree from `git archive HEAD`;
  2. apply NAME's edits (from tools/mutation_catalog/mutations.py of THIS
     worktree) to a second scratch tree; every anchor must hit exactly once;
  3. run, in the project's disposable container form, the targeted selection
     on the CONTROL tree (untouched) and on the MUTANT tree, saving each
     stdout as a .txt transcript under evidence/mutation-anchors-20261108/.

Container form (per the project rules):
  docker run --rm --network none --cpus 2 --memory 2g --init -u 1000:1000
    -v <scratch>:/w -w /w -e PYTHONPATH=/w -e PYTHONDONTWRITEBYTECODE=1
    -e HOME=/tmp aitelier:latest /usr/local/bin/python -m pytest -q
    -p no:cacheprovider <targeted ids>
No `timeout` is used together with `--rm`.
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
EVIDENCE = WORKTREE / "evidence" / "mutation-anchors-20261108"
sys.path.insert(0, str(WORKTREE / "tools" / "mutation_catalog"))
from mutations import MUTATIONS  # noqa: E402

IMAGE = "aitelier:latest"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


def _head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(WORKTREE),
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _archive_into(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    arc = subprocess.run(["git", "archive", "HEAD"], cwd=str(WORKTREE),
                         capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=arc, check=True)


def _apply(tree: Path, edits) -> list[str]:
    """Apply edits in order; each anchor must hit exactly once. Returns the
    per-edit hit report. Raises SystemExit on an anchor miss."""
    report = []
    texts: dict[str, str] = {}
    for i, edit in enumerate(edits):
        rel = edit["file"]
        if rel not in texts:
            texts[rel] = (tree / rel).read_text(encoding="utf-8")
        hits = texts[rel].count(edit["anchor"])
        report.append(f"edit[{i}] {rel}: anchor hits={hits}")
        if hits != 1:
            raise SystemExit(f"ANCHOR MISS edit[{i}] {rel}: hits={hits}")
        texts[rel] = texts[rel].replace(edit["anchor"], edit["replacement"], 1)
    for rel, text in texts.items():
        (tree / rel).write_text(text, encoding="utf-8")
    return report


def _docker_pytest(tree: Path, targets: list[str], name: str) -> int:
    argv = [
        "docker", "run", "--rm", "--network", "none", "--cpus", "2",
        "--memory", "2g", "--init", "-u", "1000:1000",
        "-v", f"{tree}:/w", "-w", "/w",
        "-e", "PYTHONPATH=/w", "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp", IMAGE,
        "/usr/local/bin/python", "-m", "pytest", "-q",
        "-p", "no:cacheprovider", *targets,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, argv, proc.stdout, proc.stderr


def _write(name: str, arm: str, rc: int, argv, out: str, err: str,
           diff_report: list[str], head: str, started: str) -> None:
    path = EVIDENCE / f"witness-{name}-{arm}.txt"
    path.write_text(
        f"# witness {name} arm={arm}\n"
        f"# head={head}\n# started={started} ended={_now()}\n"
        f"# edits: {'; '.join(diff_report) if diff_report else '(none: control)'}\n"
        f"# argv: {' '.join(argv)}\n"
        f"# BARE_RC={rc}\n\n"
        f"## stdout\n{out}\n## stderr\n{err}\n",
        encoding="utf-8")


def run_one(name: str) -> dict:
    spec = MUTATIONS[name]
    targets = list(spec["targeted"])
    head = _head()
    started = _now()
    base = Path(tempfile.mkdtemp(prefix=f"mut-{name}-", dir="/tmp"))
    control = base / "control"
    mutant = base / "mutant"
    _archive_into(control)
    _archive_into(mutant)
    hits = _apply(mutant, spec["edits"])

    c_rc, c_argv, c_out, c_err = _docker_pytest(control, targets, name)
    _write(name, "control", c_rc, c_argv, c_out, c_err, [], head, started)
    m_rc, m_argv, m_out, m_err = _docker_pytest(mutant, targets, name)
    _write(name, "mutant", m_rc, m_argv, m_out, m_err, hits, head, started)

    reds = sorted({line.split(" ")[1] for line in (m_out + m_err).splitlines()
                   if line.startswith(("FAILED ", "ERROR "))})
    shutil.rmtree(base, ignore_errors=True)
    return {"name": name, "targeted": targets, "control_rc": c_rc,
            "mutant_rc": m_rc, "mutant_reds": reds,
            "edit_hits": hits, "head": head}


def main(names: list[str]) -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    results = [run_one(n) for n in names]
    out = EVIDENCE / "witness-summary.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    for r in results:
        prev[r["name"]] = r
    out.write_text(json.dumps(prev, indent=2, sort_keys=True), encoding="utf-8")
    for r in results:
        print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
