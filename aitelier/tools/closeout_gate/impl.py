"""closeout_gate — what did this card deliver, and how hard must the review look?

The normal reviewer binds a committed per-item receipt before calling this
helper. Its complete base-to-commit tree diff reports paths and scope; receipt
file lists cannot hide changes. Non-receipt callers retain legacy depth context.

Depth is a ratchet, never a skip:
  standard — small diff, no protected path, no deletion commit
  deep     — a protected path (playtest/, tests/, the design ledgers, export
             config), a deletion commit, > 300 changed lines or > 8 files
The reviewer's template says what each depth demands. A gate that could say
"skip" would be the pass-on-absence defect with a new name.
"""
import re
from pathlib import Path

from skillflow import closed_git

PROTECTED = ("playtest/", "tests/", "design/90_decisions.md", "design/99_changelog.md",
             "design/01_process.md", "design/00_roadmap.md", ".github/", "project.godot",
             "export_presets.cfg")
_DEEP_LINES = 300
_DEEP_FILES = 8


def _git(repo: Path, *args: str) -> str:
    r = closed_git.run(repo, *args, check=False)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PROTECTED)


def closeout_gate(*, project_root: str = "", owns=None,
                  shared_hotspots=None, task_name: str = "",
                  scope_violations=None, code_receipt=None, **kwargs) -> dict:
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
    attributed = code_receipt is not None
    if attributed:
        try:
            if not isinstance(code_receipt, dict) or code_receipt.get("target") != "code":
                raise ValueError("missing code output receipt")
            base, commit = (code_receipt.get(k) for k in ("base_commit", "commit"))
            if any(not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{40}", s)
                   for s in (base, commit)):
                raise ValueError("receipt requires exact commit SHAs")
            for sha in (base, commit):
                if _git(repo, "rev-parse", "--verify", sha + "^{commit}").strip() != sha:
                    raise ValueError("receipt does not name a commit")
            if _git(repo, "rev-parse", "HEAD").strip() != commit:
                raise ValueError("receipt commit is not the current review HEAD")
            from core.closed_git import clean_against_head
            clean, why = clean_against_head(repo)
            if not clean:
                raise ValueError("current review Source is not the committed receipt: " + why)
            _git(repo, "merge-base", "--is-ancestor", base, commit)
            options = ("--no-ext-diff", "--no-textconv", "--no-renames")
            records = _git(repo, "diff", *options, "--name-status", "-z", base, commit, "--").split("\0")
            if records[-1] != "" or len(records[:-1]) % 2:
                raise ValueError("invalid NUL-delimited diff")
            # Key by path, not status: several additions/deletions may coexist.
            statuses = {records[i + 1]: records[i] for i in range(0, len(records) - 1, 2)}
            numstat = _git(repo, "diff", *options, "--numstat", "-z", base, commit, "--")
            files, added, deleted = [], 0, 0
            for record in numstat.split("\0"):
                if not record:
                    continue
                a, dl, path = record.split("\t", 2)
                binary = a == "-" or dl == "-"
                a, dl = int(a) if a.isdigit() else 0, int(dl) if dl.isdigit() else 0
                item = {"path": path, "added": a, "deleted": dl,
                        "status": statuses[path], "binary": binary}
                if statuses[path] == "D":
                    raw = closed_git.run(repo, "show", f"{base}:{path}", text=False).stdout
                    item["base_content"] = None if binary else raw[:24000].decode("utf-8", errors="replace")
                    item["base_content_truncated"] = not binary and len(raw) > 24000
                files.append(item)
                added += a
                deleted += dl
            if set(statuses) != {f["path"] for f in files}:
                raise ValueError("diff path inventories disagree")
            head = [(commit, "current item receipt")]
        except Exception as exc:
            return {"passed": False, "attribution_valid": False, "depth": "deep",
                    "error": f"closeout attribution: {exc}", "scope_violations": attempted}
    else:
        return _legacy_closeout(repo, owns, shared_hotspots, task_name, attempted)
    return _summarize(files, added, deleted, head, owns, shared_hotspots,
                      task_name, attempted, base_commit=base,
                      commit=commit, attribution_valid=True, no_changes=not files)


def _legacy_closeout(repo, owns, shared_hotspots, task_name, attempted):
    """Non-receipt callers keep their legacy depth-only context."""
    attempted_note = f"; {len(attempted)} attempted out-of-scope mutation(s)" if attempted else ""
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
    return _summarize(files, added, deleted, head, owns, shared_hotspots, task_name, attempted)


def _summarize(files, added, deleted, head, owns, shared_hotspots, task_name, attempted, **metadata):
    protected = [f["path"] for f in files if _is_protected(f["path"])]
    outside = []
    if owns is not None:
        from core.write_scope import WriteScope, out_of_scope
        scope = WriteScope(str(task_name or "code-task"), tuple(owns or ()),
                           tuple(shared_hotspots or ()))
        outside = out_of_scope((f["path"] for f in files), scope)
    deletion_commit = any(f.get("status") == "D" for f in files) or any("delete" in subj for _, subj in head)
    reasons = []
    if protected:
        reasons.append(f"touches protected path(s): {', '.join(protected)}")
    if deletion_commit:
        reasons.append("a deletion landed with this card")
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
        if f.get("status") == "D":
            lines.append(f"    base Source ({metadata.get('base_commit', '')}): " +
                         ("binary" if f.get("binary") else f["base_content"]))
            if f.get("base_content_truncated"):
                lines.append("    [base Source truncated at 24000 bytes; full deletion review still required]")
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
    return {**metadata, "depth": depth, "reasons": reasons, "commits": [s for s, _ in head],
            "files": files, "protected": protected, "added": added, "deleted": deleted,
            "out_of_scope": outside, "scope_violations": attempted,
            "summary": "\n".join(lines), "content": "\n".join(lines)}
