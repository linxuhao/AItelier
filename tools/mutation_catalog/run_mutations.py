"""Apply named mutations to detached git worktrees, run each at two scopes,
count how often the mutated lines ran, and record which tests went red.

Run from the root of a COMMITTED tree. The runner never edits the checkout it
is started from: every run (the control and each mutation) happens in its own
`git worktree add --detach` of HEAD.

    python tools/mutation_catalog/run_mutations.py                # every name
    python tools/mutation_catalog/run_mutations.py M21 DUPIMPL    # a subset
    python tools/mutation_catalog/run_mutations.py --shard 2/4    # names[1::4]
    python tools/mutation_catalog/run_mutations.py --report       # merge

Protocol of one invocation (a shard is one invocation):

1. CLEAN: `git status --porcelain` of the root, the output directory
   excepted, must be empty. Otherwise exit 3 before anything runs.
2. EMPTY CONTROL: a worktree of HEAD with no edit, run at the TARGETED scope
   (the union of the selected mutations' `targeted` lists) and at the FULL
   scope (`FULL_SCOPE`). If either bare exit code is not 0, no kill is
   reported and the runner exits 2.
3. Each mutation, in a fresh worktree: every anchor is checked BEFORE any edit
   (applied in order, each must hit exactly once; otherwise the mutation is
   `anchor-error`, never a kill); the edits are applied and the diff is
   recorded; the targeted and the full scope run, each with
   `ignition_plugin` loaded; the worktree is restored with `git checkout --
   .`, its `git status --porcelain` must then be empty (otherwise exit 3),
   and it is removed.
4. Killers, per scope: the red tests minus that scope's control reds. For a
   `behaviour` mutation (the default), a red test that read a mutated file's
   text while it ran and executed none of the mutated lines is a source-text
   witness and is removed from the killers. For a `text` mutation (the guarded property IS the text: a
   duplicated line, a deleted contract sentence) a reader of the text is the
   behavioural witness and stays. `killed` = some scope has a killer.
5. CLEAN again, as in 1 (otherwise exit 3).

Ignition is `line_hits` (executions of the replacement's lines inside the
pytest process) for a `behaviour` mutation and `file_reads` (reads of the
mutated file's text during a test) for a `text` mutation; both are logged.

Exit status: 0 every selected mutation killed; 1 some survived, hit an anchor
error or could not run; 2 control not green; 3 a tree was not clean.

Output (`OUT_DIR`, `.txt` and `.json` only): `CONTROL<tag>.txt`,
`<NAME>.txt`, `summary<tag>.json`; `--report` merges every `summary*.json`
there into `summary.txt`, control rows first.
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutations import FULL_SCOPE, GOAL_TABLE, MUTATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_REL = "final/logs/mutations"
OUT_DIR = ROOT / OUT_REL
PLUGIN_DIR_REL = "tools/mutation_catalog"


class AnchorError(Exception):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _root_porcelain() -> str:
    return _git(ROOT, "status", "--porcelain", "--", ".",
                f":(exclude){OUT_REL}").stdout.strip()


def _make_worktree() -> Path:
    base = os.environ.get("MUTATION_WORKTREE_DIR") or None
    if base:
        Path(base).mkdir(parents=True, exist_ok=True)
    wt = Path(tempfile.mkdtemp(prefix="mut-wt-", dir=base)) / "tree"
    proc = _git(ROOT, "worktree", "add", "--detach", str(wt), "HEAD")
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr}")
    return wt


def _remove_worktree(wt: Path) -> None:
    _git(ROOT, "worktree", "remove", "--force", str(wt))
    shutil.rmtree(wt.parent, ignore_errors=True)


def _changed_offsets(anchor: str, replacement: str) -> list[int]:
    """Line indices, within `replacement`, that the edit wrote (lines of the
    replacement that are not carried over unchanged from the anchor)."""
    old = anchor.splitlines(keepends=True)
    new = replacement.splitlines(keepends=True)
    changed: list[int] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
            a=old, b=new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            changed.extend(range(j1, j2))
    return changed


def _plan_edits(tree: Path, edits):
    """Apply the edits in memory, in order. Returns ({file: new_text},
    {file: [numbers of the lines the edits wrote]}); raises AnchorError when
    an anchor does not hit exactly once."""
    texts: dict[str, str] = {}
    marks: dict[str, list[list[int]]] = {}
    for edit in edits:
        rel = edit["file"]
        if rel not in texts:
            texts[rel] = (tree / rel).read_text(encoding="utf-8")
            marks[rel] = []
        text = texts[rel]
        hits = text.count(edit["anchor"])
        if hits != 1:
            raise AnchorError(f"anchor hit {hits} times (need exactly 1): "
                              f"{rel}: {edit['anchor'][:70]!r}")
        start = text.index(edit["anchor"])
        delta = len(edit["replacement"]) - len(edit["anchor"])
        for mark in marks[rel]:
            if mark[0] > start:
                mark[0] += delta
        offsets = _changed_offsets(edit["anchor"], edit["replacement"])
        marks[rel].append([start, offsets])
        texts[rel] = (text[:start] + edit["replacement"]
                      + text[start + len(edit["anchor"]):])
    lines: dict[str, list[int]] = {}
    for rel, file_marks in marks.items():
        numbers: set[int] = set()
        for start, offsets in file_marks:
            first = texts[rel].count("\n", 0, start) + 1
            numbers.update(first + offset for offset in offsets)
        lines[rel] = sorted(numbers)
    return texts, lines


def _run_pytest(tree: Path, targets, spec: dict | None, tag: str):
    """Run pytest in `tree`. Returns (bare_rc, output, ignition, argv)."""
    argv = [sys.executable, "-m", "pytest", "-q", "-rfE",
            "-p", "no:cacheprovider", "-p", "ignition_plugin", *targets]
    out_json = Path(tempfile.mkstemp(prefix=f"ign-{tag}-",
                                     suffix=".json")[1])
    env = {**os.environ,
           "PYTHONPATH": f"{tree}{os.pathsep}{tree / PLUGIN_DIR_REL}",
           "PYTHONDONTWRITEBYTECODE": "1",
           "MUTATION_IGNITION_SPEC": json.dumps(spec or {}),
           "MUTATION_IGNITION_OUT": str(out_json)}
    proc = subprocess.run(argv, cwd=str(tree), capture_output=True,
                          text=True, env=env)
    try:
        ignition = json.loads(out_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ignition = None
    out_json.unlink(missing_ok=True)
    return proc.returncode, proc.stdout + proc.stderr, ignition, argv


def _red_test_ids(output: str) -> list[str]:
    ids = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            ids.add(line.split(" ")[1])
    return sorted(ids)


def _tail_counts(output: str) -> str:
    found = [line for line in output.splitlines() if " in " in line and (
        "passed" in line or "failed" in line or "error" in line)]
    return found[-1].strip("= ") if found else "(no pytest summary line)"


def _targeted_union(names) -> list[str]:
    seen: list[str] = []
    for name in names:
        for target in MUTATIONS[name].get("targeted", []):
            if target not in seen:
                seen.append(target)
    return seen


def run_control(names, tag: str) -> dict:
    wt = _make_worktree()
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    targeted = _targeted_union(names)
    started = _now()
    trc, tout, _, targv = _run_pytest(wt, targeted, None, "control-t")
    frc, fout, _, fargv = _run_pytest(wt, FULL_SCOPE, None, "control-f")
    _remove_worktree(wt)
    result = {
        "name": "CONTROL" + tag, "status": "control", "head": head,
        "targeted_rc": trc, "full_rc": frc,
        "targeted_counts": _tail_counts(tout), "full_counts": _tail_counts(fout),
        "targeted_reds": _red_test_ids(tout), "full_reds": _red_test_ids(fout),
        "names": list(names), "started": started, "ended": _now()}
    (OUT_DIR / f"CONTROL{tag}.txt").write_text(
        f"# EMPTY CONTROL{tag}: no edit applied\n"
        f"# head={head}\n# worktree={wt} (removed)\n"
        f"# names covered={' '.join(names)}\n"
        f"# started={started} ended={result['ended']}\n\n"
        f"## targeted scope: {' '.join(targv)}\n# cwd={wt}\n{tout}\n"
        f"BARE_RC_TARGETED={trc}\n\n"
        f"## full scope: {' '.join(fargv)}\n# cwd={wt}\n{fout}\n"
        f"BARE_RC_FULL={frc}\n\n"
        f"# targeted reds={result['targeted_reds']}\n"
        f"# full reds={result['full_reds']}\n", encoding="utf-8")
    return result


def run_one(name: str, mutation: dict, control: dict) -> dict:
    kind = mutation.get("kind", "behaviour")
    wt = _make_worktree()
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    started = _now()
    try:
        texts, lines = _plan_edits(wt, mutation["edits"])
    except AnchorError as exc:
        _remove_worktree(wt)
        (OUT_DIR / f"{name}.txt").write_text(
            f"# {name}: anchor-error\n# head={head}\n# {exc}\n",
            encoding="utf-8")
        return {"name": name, "kind": kind, "status": "anchor-error",
                "detail": str(exc), "head": head}
    for rel, text in texts.items():
        (wt / rel).write_text(text, encoding="utf-8")
    diff = _git(wt, "diff").stdout
    spec = {"lines": {str(wt / rel): nums for rel, nums in lines.items()
                      if rel.endswith(".py")},
            "files": [str(wt / rel) for rel in texts]}
    trc, tout, tign, targv = _run_pytest(wt, mutation.get("targeted", []),
                                         spec, f"{name}-t")
    frc, fout, fign, fargv = _run_pytest(wt, FULL_SCOPE, spec, f"{name}-f")
    _git(wt, "checkout", "--", ".")
    porcelain = _git(wt, "status", "--porcelain").stdout.strip()
    _remove_worktree(wt)

    def _killers(output, ign, control_reds):
        reds = set(_red_test_ids(output)) - set(control_reds)
        readers = (set((ign or {}).get("source_readers") or [])
                   - set((ign or {}).get("igniting_tests") or []))
        witnesses = reds & readers if kind == "behaviour" else set()
        return sorted(reds - witnesses), sorted(witnesses)

    t_kill, t_text = _killers(tout, tign, control["targeted_reds"])
    f_kill, f_text = _killers(fout, fign, control["full_reds"])
    field = "line_hits" if kind == "behaviour" else "file_reads"
    t_ign = (tign or {}).get(field)
    f_ign = (fign or {}).get(field)
    if porcelain:
        status = "not-restored"
    elif trc not in (0, 1) or frc not in (0, 1):
        status = "run-error"
    elif t_kill or f_kill:
        status = "killed"
    else:
        status = "SURVIVED"
    result = {
        "name": name, "kind": kind, "status": status, "head": head,
        "ignition_field": field,
        "targeted_ignition": t_ign, "full_ignition": f_ign,
        "targeted_rc": trc, "full_rc": frc,
        "targeted_counts": _tail_counts(tout), "full_counts": _tail_counts(fout),
        "targeted_killers": t_kill, "full_killers": f_kill,
        "targeted_text_witnesses": t_text, "full_text_witnesses": f_text,
        "porcelain_after_restore": porcelain,
        "started": started, "ended": _now()}
    (OUT_DIR / f"{name}.txt").write_text(
        f"# {name} ({kind}): {mutation.get('file_note', '')}\n"
        f"# head={head}\n# worktree={wt} (removed)\n"
        f"# started={started} ended={result['ended']}\n"
        f"# replacement lines={json.dumps(lines)}\n\n"
        f"## diff applied\n{diff}\n"
        f"## targeted scope: {' '.join(targv)}\n# cwd={wt}\n{tout}\n"
        f"BARE_RC_TARGETED={trc}\n"
        f"IGNITION_TARGETED={json.dumps(tign, sort_keys=True)}\n\n"
        f"## full scope: {' '.join(fargv)}\n# cwd={wt}\n{fout}\n"
        f"BARE_RC_FULL={frc}\n"
        f"IGNITION_FULL={json.dumps(fign, sort_keys=True)}\n\n"
        f"# control targeted reds={control['targeted_reds']}\n"
        f"# control full reds={control['full_reds']}\n"
        f"# targeted killers={t_kill}\n# targeted source-text witnesses={t_text}\n"
        f"# full killers={f_kill}\n# full source-text witnesses={f_text}\n"
        f"# porcelain after restore={porcelain!r}\n"
        f"# status={status}\n", encoding="utf-8")
    return result


def _row(r: dict) -> str:
    def cell(v):
        return "–" if v is None else str(v)
    if r["status"] == "control":
        return (f"| {r['name']} | control | – | {r['targeted_rc']} | "
                f"{r['full_rc']} | {r['targeted_counts']} / {r['full_counts']}"
                f" | reds: {len(r['targeted_reds'])} / {len(r['full_reds'])} "
                f"| `{r['name']}.txt` |")
    if r["status"] == "anchor-error":
        return (f"| {r['name']} | anchor-error | – | – | – | {r['detail']} "
                f"| – | `{r['name']}.txt` |")
    killers = sorted(set(r["targeted_killers"]) | set(r["full_killers"]))
    shown = "; ".join(k.split("::")[-1] for k in killers[:4])
    more = f" (+{len(killers) - 4})" if len(killers) > 4 else ""
    return (f"| {r['name']} | {r['status']} | {r['ignition_field']} "
            f"{cell(r['targeted_ignition'])} / {cell(r['full_ignition'])} | "
            f"{r['targeted_rc']} | {r['full_rc']} | "
            f"{len(r['targeted_killers'])} / {len(r['full_killers'])} | "
            f"{shown}{more} | `{r['name']}.txt` |")


def report() -> int:
    results = []
    for path in sorted(OUT_DIR.glob("summary*.json")):
        results.extend(json.loads(path.read_text(encoding="utf-8")))
    controls = [r for r in results if r["status"] == "control"]
    rest = [r for r in results if r["status"] != "control"]
    order = {n: i for i, n in enumerate(GOAL_TABLE)}
    rest.sort(key=lambda r: (order.get(r["name"], len(order)), r["name"]))
    heads = sorted({r.get("head") for r in results})
    lines = [
        f"# Mutation runner report, merged from {len(controls)} control(s) "
        f"and {len(rest)} mutation(s); generated {_now()} by "
        f"`python {PLUGIN_DIR_REL}/run_mutations.py --report`",
        f"# head(s) measured: {', '.join(h or '?' for h in heads)}",
        "",
        "| name | status | ignition (targeted / full) | targeted bare RC "
        "| full bare RC | killers (targeted / full) | killer tests | log |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [_row(r) for r in controls + rest]
    survived = [r["name"] for r in rest if r["status"] != "killed"]
    lines += ["", f"# killed: {len(rest) - len(survived)} of {len(rest)}",
              f"# not killed: {', '.join(survived) if survived else 'none'}"]
    (OUT_DIR / "summary.txt").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")
    print("\n".join(lines))
    return 0 if not survived and all(
        c["targeted_rc"] == 0 and c["full_rc"] == 0 for c in controls) else 1


def main(argv) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if argv[:1] == ["--report"]:
        return report()
    tag = ""
    if argv[:1] == ["--shard"]:
        index, total = (int(x) for x in argv[1].split("/"))
        names = sorted(MUTATIONS)[index - 1::total]
        tag = f"_shard{index}of{total}"
        argv = argv[2:]
    else:
        names = argv or sorted(MUTATIONS)
    unknown = [n for n in names if n not in MUTATIONS]
    if unknown:
        print(f"unknown mutation name(s): {unknown}")
        return 1

    dirty = _root_porcelain()
    if dirty:
        print(f"ROOT NOT CLEAN before the run:\n{dirty}")
        return 3
    print(f"[{_now()}] root={ROOT} head={_git(ROOT, 'rev-parse', 'HEAD').stdout.strip()}")
    print(f"[{_now()}] empty control{tag} over {len(names)} name(s)...", flush=True)
    control = run_control(names, tag)
    results = [control]
    if control["targeted_rc"] != 0 or control["full_rc"] != 0:
        print(f"CONTROL NOT GREEN (targeted rc={control['targeted_rc']}, "
              f"full rc={control['full_rc']}). Refusing to report kills. "
              f"Reds: {control['targeted_reds']} {control['full_reds']}")
        (OUT_DIR / f"summary{tag}.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8")
        return 2
    print(f"[{_now()}] control green (targeted rc=0, full rc=0).", flush=True)

    for name in names:
        print(f"[{_now()}]   {name}...", end=" ", flush=True)
        result = run_one(name, MUTATIONS[name], control)
        results.append(result)
        print(result["status"], flush=True)
        (OUT_DIR / f"summary{tag}.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8")
        if result["status"] == "not-restored":
            print(f"WORKTREE NOT RESTORED after {name}: "
                  f"{result['porcelain_after_restore']}")
            return 3

    dirty = _root_porcelain()
    if dirty:
        print(f"ROOT NOT CLEAN after the run:\n{dirty}")
        return 3
    for r in results:
        print(_row(r))
    failed = [r["name"] for r in results[1:] if r["status"] != "killed"]
    if failed:
        print("NOT KILLED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
