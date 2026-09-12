"""closeout_gate — what did this card deliver, and how hard must the review look?

Runs as a CONTEXT SOURCE of t_impl_review, right after the item's on_deliver
commit(s). It reads the commits at HEAD whose subject carries `t_impl` (the
delivery and, if any, the queued-deletion commit that follows it), diffs them
against their base, and reports files, +/- lines and protected paths touched.

Depth is a ratchet, never a skip:
  standard — small diff, no protected path, no deletion commit
  deep     — a protected path (playtest/, tests/, the design ledgers, export
             config), a deletion commit, > 300 changed lines or > 8 files
The reviewer's template says what each depth demands. A gate that could say
"skip" would be the pass-on-absence defect with a new name.
"""
import subprocess
from pathlib import Path

PROTECTED = ("playtest/", "tests/", "design/90_decisions.md", "design/99_changelog.md",
             "design/01_process.md", "design/00_roadmap.md", ".github/", "project.godot",
             "export_presets.cfg")
_DEEP_LINES = 300
_DEEP_FILES = 8


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PROTECTED)


def closeout_gate(*, project_root: str = "", owns=None,
                  shared_hotspots=None, task_name: str = "",
                  scope_violations=None, **kwargs) -> dict:
    attempted = list(scope_violations or [])
    attempted_detail = ", ".join(
        f"{item.get('tool', '?')} {item.get('requested_path', '?')}"
        for item in attempted)
    attempted_note = (f"; {len(attempted)} attempted out-of-scope mutation(s): "
                      f"{attempted_detail}" if attempted else "")
    if not project_root or not Path(project_root).is_absolute():
        return {"depth": "deep", "error": "closeout_gate: project_root must be an absolute "
                "path — no delivery to inspect; review the whole working tree",
                "scope_violations": attempted,
                "summary": "[closeout_gate] no repository root injected → depth: deep" + attempted_note,
                "content": "[closeout_gate] no repository root injected → depth: deep" + attempted_note}
    repo = Path(project_root)
    try:
        rows = [ln.split("\t", 1) for ln in _git(repo, "log", "-6", "--format=%H%x09%s").splitlines() if ln]
    except Exception as e:
        return {"depth": "deep", "error": f"closeout_gate: {e}",
                "scope_violations": attempted,
                "summary": f"[closeout_gate] git unreadable ({e}) → depth: deep" + attempted_note,
                "content": f"[closeout_gate] git unreadable ({e}) → depth: deep" + attempted_note}
    head: list[tuple[str, str]] = []
    for sha, subj in rows:
        if "t_impl" in subj:
            head.append((sha, subj))
        else:
            break
    if not head:
        return {"depth": "deep", "commits": [], "files": [], "protected": [],
                "scope_violations": attempted,
                "summary": "[closeout_gate] HEAD is not a t_impl delivery commit — nothing "
                           "attributable to this card; review the whole working tree → depth: deep"
                           + attempted_note,
                "content": "[closeout_gate] HEAD is not a t_impl delivery commit → depth: deep"
                           + attempted_note}
    base = head[-1][0] + "~1"
    try:
        numstat = _git(repo, "diff", "--numstat", base, "HEAD")
    except Exception:
        numstat = _git(repo, "diff", "--numstat", "--root", "HEAD") if len(rows) == len(head) else ""
    files, added, deleted = [], 0, 0
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        a, dl, path = parts
        a = int(a) if a.isdigit() else 0
        dl = int(dl) if dl.isdigit() else 0
        added += a; deleted += dl
        files.append({"path": path, "added": a, "deleted": dl})
    protected = [f["path"] for f in files if _is_protected(f["path"])]
    outside = []
    if owns is not None:
        from core.write_scope import WriteScope, out_of_scope
        scope = WriteScope(str(task_name or "code-task"), tuple(owns or ()),
                           tuple(shared_hotspots or ()))
        outside = out_of_scope((f["path"] for f in files), scope)
    deletion_commit = any("delete" in subj for _, subj in head)
    reasons = []
    if protected:
        reasons.append(f"touches protected path(s): {', '.join(protected)}")
    if deletion_commit:
        reasons.append("a queued-deletion commit landed with this card")
    if added + deleted > _DEEP_LINES:
        reasons.append(f"{added + deleted} changed lines > {_DEEP_LINES}")
    if len(files) > _DEEP_FILES:
        reasons.append(f"{len(files)} files > {_DEEP_FILES}")
    if outside:
        reasons.append(f"out-of-scope delivered path(s): {', '.join(outside)}")
    if attempted:
        reasons.append(f"{len(attempted)} attempted out-of-scope mutation(s)")
    depth = "deep" if reasons else "standard"
    lines = [f"[closeout_gate] depth: **{depth}**" + (" — " + "; ".join(reasons) if reasons else
             " — small diff, no protected path"),
             "commits under review: " + "; ".join(f"{s[:7]} {j}" for s, j in head),
             f"{len(files)} file(s), +{added} / -{deleted} lines:"]
    for f in files:
        flag = "  ⚠ protected" if f["path"] in protected else ""
        lines.append(f"  - {f['path']}  (+{f['added']} / -{f['deleted']}){flag}")
    if attempted:
        lines.append("attempted scope violations (refused before mutation):")
        for item in attempted:
            provenance = ""
            if item.get("step_instance_id") is not None:
                provenance = (f" (step instance {item['step_instance_id']}, "
                              f"claim epoch {item.get('claim_epoch', 0)})")
            lines.append(
                f"  - {item.get('tool', '?')} {item.get('requested_path', '?')}"
                + provenance)
    return {"depth": depth, "reasons": reasons, "commits": [s for s, _ in head],
            "files": files, "protected": protected, "added": added, "deleted": deleted,
            "out_of_scope": outside, "scope_violations": attempted,
            "summary": "\n".join(lines), "content": "\n".join(lines)}
