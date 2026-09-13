"""scaffold_bible — novel_init's finalize tool step.

By the time this runs, the design agent has already written the seven raw bible
files INTO the code repo (mode:write + repo_apply, DPE-style):

  novel/bible/{overview.md, compass.md, world.yaml, pacing.yaml,
               characters.yaml (a YAML list of cards), threads.yaml, arcs.yaml}

This tool normalizes them into the runtime layout and freezes the baseline:
  - splits characters.yaml into per-character files (progression/reconcile stay
    per-card; git diffs stay readable), then removes characters.yaml
  - fills mechanical defaults (status/progression on cards, status/hints on
    threads, status on arcs + per-node status:pending on plot nodes)
  - builds the derived index, git-commits the normalized tree, then stamps the
    ``novel-genesis`` git TAG as the reconcile baseline (no duplicated snapshot
    directory — git is the history)

Deterministic, no LLM. A completed index/tag blocks a second run; a private
gitdir recovery record distinguishes a failed freeze and makes retry safe.
"""

import json
import os
import subprocess
from pathlib import Path

from aitelier import novel_state as ns


_RECOVERY_FILE = "aitelier-novel-genesis-recovery.json"


class _GitFailure(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise _GitFailure(f"git {' '.join(args)}: {detail}")
    return result


def _git_context(root: Path) -> tuple[Path, Path]:
    top = ns.git_toplevel(root)
    if top is None:
        raise ValueError(f"scaffold_bible: {root} is not a Git worktree")
    if top != root.resolve():
        raise ValueError(
            f"scaffold_bible: target must be the Git worktree root {top}, got {root}"
        )
    source_checkout = Path(__file__).resolve().parents[3]
    if root.resolve() == source_checkout:
        raise ValueError("scaffold_bible: refusing to scaffold into the AItelier source checkout")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()
    return top, git_dir


def _read_recovery(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"scaffold_bible: unreadable recovery record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"scaffold_bible: invalid recovery record {path}")
    return value


def _write_recovery(path: Path, value: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _remember_failure(path: Path, record: dict, exc: Exception) -> ValueError:
    record.setdefault("first_error", str(exc))
    record["phase"] = "freeze_failed"
    _write_recovery(path, record)
    return ValueError(
        "scaffold_bible: Git freeze failed; normalized bible was preserved and "
        f"retrying novel_init/scaffold is safe. First Git error: {record['first_error']}"
    )


def _tag_commit(root: Path) -> str:
    result = _git(root, "rev-parse", "-q", "--verify",
                  f"refs/tags/{ns.GENESIS_TAG}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _baseline_readable(root: Path, commit: str) -> bool:
    if not commit:
        return False
    required = [
        "novel/bible/overview.md", "novel/bible/compass.md",
        "novel/bible/world.yaml", "novel/bible/pacing.yaml",
        "novel/bible/threads.yaml", "novel/bible/arcs.yaml",
        "novel/state/index.yaml",
    ]
    for rel in required:
        if _git(root, "cat-file", "-e", f"{commit}:{rel}", check=False).returncode:
            return False
    characters = _git(
        root, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", commit,
        "novel/bible/characters", check=False)
    return (characters.returncode == 0
            and any(line.endswith(".yaml") for line in characters.stdout.splitlines())
            and _git(root, "cat-file", "-e",
                     f"{commit}:novel/bible/characters.yaml", check=False).returncode != 0)


def _freeze(root: Path, recovery_path: Path, record: dict, *, recovering: bool
            ) -> tuple[str, bool]:
    """Commit exactly novel/ and create a verified, never-overwritten genesis tag."""
    try:
        commit = str(record.get("commit") or "")
        if commit and not _baseline_readable(root, commit):
            raise _GitFailure(f"recorded genesis commit {commit} is not readable")
        if not commit:
            dirty = _git(root, "status", "--porcelain", "--", "novel").stdout
            if dirty:
                _git(root, "add", "--", "novel")
                _git(root, "commit", "--only", "-m", "世界设定：初始化归一化",
                     "--", "novel")
                commit = _git(root, "rev-parse", "HEAD").stdout.strip()
            else:
                additions = _git(
                    root, "log", "--format=%H", "--diff-filter=A", "--",
                    "novel/state/index.yaml").stdout.splitlines()
                candidates = [sha for sha in additions if _baseline_readable(root, sha)]
                if len(candidates) != 1:
                    raise _GitFailure(
                        "cannot identify one normalized genesis commit from "
                        f"novel/state/index.yaml (candidates={candidates})")
                commit = candidates[0]
            if not _baseline_readable(root, commit):
                raise _GitFailure(
                    f"commit {commit or '<missing>'} does not contain the normalized novel baseline")
            record["commit"] = commit
            record["phase"] = "committed"
            _write_recovery(recovery_path, record)

        existing = _tag_commit(root)
        if existing and existing != commit:
            raise _GitFailure(
                f"tag {ns.GENESIS_TAG} already points to {existing}; refusing to overwrite it")
        if not existing:
            _git(root, "tag", ns.GENESIS_TAG, commit)
        tagged = _tag_commit(root)
        if tagged != commit or not _baseline_readable(root, tagged):
            raise _GitFailure(
                f"tag {ns.GENESIS_TAG} is not a readable baseline for commit {commit}")
    except _GitFailure as exc:
        raise _remember_failure(recovery_path, record, exc) from exc
    recovery_path.unlink(missing_ok=True)
    return commit, recovering


def _require(path: Path, label: str) -> str:
    if not path.is_file():
        raise ValueError(f"scaffold_bible: design did not write {label} at {path}")
    return path.read_text(encoding="utf-8")


def scaffold_bible(*, project_root: str = "", workspace_root: str = "",
                   **kwargs) -> dict:
    # The bible lives in the code repo (project_root); design's repo_apply put it
    # there. In unit tests project_root is empty → fall back to workspace_root.
    _base = project_root or workspace_root
    if not _base or not Path(_base).is_absolute():
        # Never fall back to "." → the process CWD, which in the container is the
        # AItelier source repo bind-mounted at /app: a missing injection here
        # would git-commit + tag into AItelier's own repo (cf. the readme_* bug).
        raise ValueError(
            "scaffold_bible: project_root/workspace_root must be an absolute path "
            f"(got project_root={project_root!r}, workspace_root={workspace_root!r}) "
            "— refusing to resolve against the process CWD")
    ws = Path(_base).resolve()
    _top, git_dir = _git_context(ws)
    recovery_path = git_dir / _RECOVERY_FILE
    recovery = _read_recovery(recovery_path)
    bib = ns.bible_dir(ws)

    index_exists = (ns.state_dir(ws) / "index.yaml").exists()
    if not recovery and (_tag_commit(ws) or index_exists):
        raise ValueError(
            "scaffold_bible: novel already scaffolded (genesis tag or state/index.yaml exists) "
            "— novel_init runs once per novel; edit the bible through chapter runs.")

    recovering_normalized = bool(recovery and not (bib / "characters.yaml").exists())
    if recovering_normalized:
        for rel in ("overview.md", "compass.md", "world.yaml", "pacing.yaml",
                    "threads.yaml", "arcs.yaml"):
            _require(bib / rel, f"novel/bible/{rel}")
        characters = list((bib / "characters").glob("*.yaml"))
        if not characters:
            raise ValueError(
                "scaffold_bible: recovery found no normalized character cards; "
                "bible left untouched for inspection")
        ns.chapters_dir(ws).mkdir(parents=True, exist_ok=True)
        ns.rebuild_index(ws)
        commit, recovered = _freeze(
            ws, recovery_path, recovery, recovering=True)
        return {"scaffolded": True, "characters": len(characters),
                "threads": len(ns.load_yaml(bib / "threads.yaml", []) or []),
                "arcs": len(ns.load_yaml(bib / "arcs.yaml", []) or []),
                "committed": True, "genesis_tagged": True,
                "commit": commit, "recovered": recovered}

    overview = _require(bib / "overview.md", "novel/bible/overview.md")
    if not overview.strip():
        raise ValueError("scaffold_bible: overview.md (总纲) is empty")
    _require(bib / "compass.md", "novel/bible/compass.md")

    # ── Phase 1: VALIDATE everything before touching anything (a failed
    # scaffold must not leave a half-normalized bible behind) ──
    chars_path = bib / "characters.yaml"
    characters = ns.load_yaml(chars_path, None)
    if not isinstance(characters, list) or not characters:
        raise ValueError("scaffold_bible: novel/bible/characters.yaml must be a "
                         "non-empty YAML list of character cards")
    for card in characters:
        if not str((card or {}).get("name") or "").strip():
            raise ValueError(f"scaffold_bible: character without a name: {card}")

    threads = ns.load_yaml(bib / "threads.yaml", []) or []
    arcs = ns.load_yaml(bib / "arcs.yaml", []) or []
    for a in arcs:
        nodes = a.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(
                f"scaffold_bible: arc '{a.get('name')}' has no plot nodes — "
                "plot is node-driven; every arc needs an ordered `nodes:` list")
        seen_ids: set[str] = set()
        for nd in nodes:
            nid = str((nd or {}).get("id") or "").strip()
            if not nid or not str(nd.get("beat") or "").strip():
                raise ValueError(
                    f"scaffold_bible: arc '{a.get('name')}' node missing id/beat: {nd}")
            if nid in seen_ids:
                raise ValueError(
                    f"scaffold_bible: arc '{a.get('name')}' duplicate node id '{nid}'")
            seen_ids.add(nid)

    # Thread reveal gates must point at real nodes (fail loud now, not chapter 200).
    arc_names = {str(a.get("name")): a for a in arcs}
    for t in threads:
        gate = t.get("earliest_reveal")
        if isinstance(gate, dict):
            ga, gn = str(gate.get("arc") or ""), str(gate.get("node") or "")
            a = arc_names.get(ga)
            ids = {str(nd.get("id")) for nd in (a.get("nodes") or [])} if a else set()
            if a is None or gn not in ids:
                raise ValueError(
                    f"scaffold_bible: thread '{t.get('name')}' earliest_reveal "
                    f"points at unknown node {ga}/{gn}")

    # Record recovery before the first mutation. A process interrupted anywhere
    # in normalization can safely repeat the idempotent writes or rebuild the
    # derived index from the already-split cards.
    recovery = recovery or {"started": True, "phase": "normalizing"}
    _write_recovery(recovery_path, recovery)

    # ── Phase 2: normalize + write ──
    for card in characters:
        card.setdefault("status", "alive")
        card.setdefault("progression", [])
        # Opening balance: the card as authored at genesis (design prompt
        # mandates first-appearance state). Kept ON the card so the probe can
        # feed 初始→现在 without touching git in the hot path; characters
        # created later get theirs stamped in apply_events (create: true).
        card["initial"] = {k: v for k, v in card.items()
                          if k not in ("initial", "progression")}
        ns.dump_yaml(ns.character_path(ws, str(card["name"]).strip()), card)
    chars_path.unlink()

    for t in threads:
        t.setdefault("status", "open")
        t.setdefault("hints", [])
    ns.dump_yaml(bib / "threads.yaml", threads)

    for a in arcs:
        a.setdefault("status", "active")
        for nd in a["nodes"]:
            nd.setdefault("status", "pending")
    ns.dump_yaml(bib / "arcs.yaml", arcs)

    ns.chapters_dir(ws).mkdir(parents=True, exist_ok=True)
    recovery["phase"] = "normalized"
    _write_recovery(recovery_path, recovery)
    ns.rebuild_index(ws)

    commit, recovered = _freeze(
        ws, recovery_path, recovery, recovering=False)

    return {"scaffolded": True, "characters": len(characters),
            "threads": len(threads), "arcs": len(arcs),
            "committed": True, "genesis_tagged": True,
            "commit": commit, "recovered": recovered}
