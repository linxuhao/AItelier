"""Output destination migration, applied only by a supporting runtime at boot.

Data-file changes never repin live/historical runs. Backups retain original bytes;
ambiguous legacy copy contracts fail rather than guessing a destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

COPY_TOOLS = {"repo_apply", "repo_delete"}
CODE_SLOTS = {"linter_manifest", "readme"}
ARTIFACT_SLOTS = {"design", "report"}


def _tools(value):
    if isinstance(value, dict):
        if "tool" in value:
            yield value["tool"]
        for child in value.values():
            yield from _tools(child)
    elif isinstance(value, list):
        for child in value:
            yield from _tools(child)


def _remove_copy_hooks(lifecycle):
    for event in list(lifecycle):
        value = lifecycle[event]
        if isinstance(value, dict) and value.get("tool") in COPY_TOOLS:
            del lifecycle[event]
        elif isinstance(value, list):
            kept = [x for x in value if not isinstance(x, dict) or x.get("tool") not in COPY_TOOLS]
            if kept:
                lifecycle[event] = kept
            else:
                del lifecycle[event]


def migrate_document(document: dict) -> list[dict]:
    changes = []

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        lifecycle = value.get("lifecycle")
        if value.get("id") and isinstance(lifecycle, dict) and "repo_apply" in set(_tools(lifecycle)):
            output = value.setdefault("output", {})
            mode = output.get("mode", value.get("output_mode"))
            fixed = output.get("fixed") or {}
            if not fixed and mode == "write":
                output["target"] = "code"
                output.pop("carry_forward", None)
                destinations = {"*": "code"}
            elif fixed and set(fixed) <= CODE_SLOTS | ARTIFACT_SLOTS:
                output["target"] = "artifact"
                destinations = {}
                for slot, entry in list(fixed.items()):
                    target = "code" if slot in CODE_SLOTS else "artifact"
                    destinations[slot] = target
                    if isinstance(entry, str):
                        entry = {"file": entry}
                        fixed[slot] = entry
                    entry["target"] = target
            else:
                raise ValueError(f"{value['id']}: copy delivery has unknown output contract; classify it explicitly before migrating")
            _remove_copy_hooks(lifecycle)
            if not lifecycle:
                del value["lifecycle"]
            changes.append({"step": value["id"], "destinations": destinations})
        # The native lint tool can auto-fix files. It must run BEFORE the
        # candidate commit, not leave new dirty code behind after delivery.
        output = value.get("output") or {}
        if value.get("id") and output.get("target") == "code":
            lifecycle = value.get("lifecycle") or {}
            checks = lifecycle.get("after_deliver")
            if isinstance(checks, list):
                mutating = [x for x in checks if isinstance(x, dict) and x.get("tool") == "lint"]
                if mutating:
                    remaining = [x for x in checks if x not in mutating]
                    if remaining:
                        lifecycle["after_deliver"] = remaining
                    else:
                        del lifecycle["after_deliver"]
                    if not lifecycle:
                        value.pop("lifecycle", None)
                    validation = value.setdefault("validation", [])
                    for check in mutating:
                        if check not in validation:
                            validation.append(check)
                    changes.append({"step": value["id"], "mutating_checks_moved_before_commit": len(mutating)})
        for child in list(value.values()):
            visit(child)

    visit(document)
    return changes


def require_output_engine() -> None:
    from skillflow.graph import StepNode
    from skillflow.core import SkillFlow
    if ("output_target" not in StepNode.__dataclass_fields__
            or not hasattr(SkillFlow, "output_directory")):
        raise RuntimeError("This AItelier build requires the bundled SkillFlow output-target engine. "
                           "Rebuild/install the matching wheel before restarting; old engines may ignore output.target.")



def write_migrated_config(path: Path, original: bytes, rendered: bytes, backup_dir: Path) -> Path:
    """Shared CLI/boot persistence: original backup, atomic replacement, no lost update."""
    if path.is_symlink():
        raise RuntimeError(f"Refusing to migrate a symlinked config: {path}")
    if path.read_bytes() != original:
        raise RuntimeError(f"Config changed while being migrated: {path}")
    sha = hashlib.sha256(original).hexdigest()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (path.name + "." + sha + ".bak")
    if backup.exists():
        if backup.read_bytes() != original:
            raise RuntimeError(f"Migration backup is corrupt: {backup}")
    else:
        backup.write_bytes(original)
    fd, pending = tempfile.mkstemp(prefix=".output-migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != original:
            raise RuntimeError(f"Config changed while being migrated: {path}")
        os.chmod(pending, path.stat().st_mode & 0o777)
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)
    return backup


def migrate_generated_outputs(config_dir: Path) -> list[dict]:
    require_output_engine()
    from skillflow.graph import PipelineGraph
    backup_dir = config_dir.parent / "migration_backups" / "output-target-v1"
    reports = []
    for path in sorted(config_dir.glob("gen_*.yaml")):
        original = path.read_bytes()
        try:
            document = yaml.safe_load(original)
        except yaml.YAMLError:
            continue  # normal registration reports a malformed pre-existing file
        if not isinstance(document, dict):
            continue
        changes = migrate_document(document)
        if not changes:
            continue
        PipelineGraph._from_dict(document)  # validate everything before any write
        rendered = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
        sha = hashlib.sha256(original).hexdigest()
        backup = write_migrated_config(path, original, rendered, backup_dir)
        reports.append({"path": str(path), "backup": str(backup), "before_sha256": sha,
                        "after_sha256": hashlib.sha256(rendered).hexdigest(), "changes": changes})
    if reports:
        from skillflow.output_targets import atomic_json
        atomic_json(backup_dir / "last-migration.json", {"configs": reports,
                    "note": "Only registered config files changed; pinned run graph versions were not repointed."})
    return reports
