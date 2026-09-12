"""Deterministic requirement-inventory and task-coverage gate.

The architect records the effective requirements after owner rulings.  The PM
maps each requirement to either an executable card or the authoritative ruling
that withdrew/superseded it.  The same compact, content-addressed ledger is then
injected into the PM reviewer instead of replaying earlier review transcripts.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_ALLOWED_AUTHORITIES = {"owner", "state"}
_INACTIVE = {"withdrawn", "superseded"}
_STATE_HEADING = "# State goal attempt"


def _canonical(document: dict[str, Any]) -> bytes:
    stripped = dict(document)
    if document.get("document_type") == "requirement_inventory":
        stripped.pop("inventory_sha256", None)
    elif document.get("document_type") == "coverage_ledger":
        stripped.pop("ledger_sha256", None)
    return json.dumps(
        stripped, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def hash_document(document: dict[str, Any]) -> str:
    """Hash the canonical document while excluding only its own hash field."""
    return hashlib.sha256(_canonical(document)).hexdigest()


def _load(path: Path, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{label} not found at {path}"]
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{label} unreadable: {exc}"]
    if not isinstance(value, dict):
        return None, [f"{label} must be a JSON object"]
    return value, []


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _baseline_errors(value: Any, prefix: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    errors = []
    if not _nonempty(value.get("id")):
        errors.append(f"{prefix}.id must be non-empty")
    revision = value.get("revision")
    if not ((isinstance(revision, int) and not isinstance(revision, bool)
            and revision >= 1) or _nonempty(revision)):
        errors.append(f"{prefix}.revision must be a positive integer or non-empty version")
    return errors


def _ruling_errors(ruling: Any, requirement: dict[str, Any],
                   baseline: dict[str, Any]) -> list[str]:
    rid = requirement.get("id", "<unknown>")
    if not isinstance(ruling, dict):
        return [f"{rid}: {requirement.get('status')} requirement needs an authoritative ruling"]
    errors = []
    authority = ruling.get("authority")
    if authority not in _ALLOWED_AUTHORITIES:
        errors.append(
            f"{rid}: ruling authority must be owner or state, got {authority!r}")
    for field in ("source_id", "revision"):
        if not _nonempty(ruling.get(field)):
            errors.append(f"{rid}: ruling.{field} must be non-empty")
    if ruling.get("decision") != requirement.get("status"):
        errors.append(
            f"{rid}: ruling decision {ruling.get('decision')!r} does not match "
            f"status {requirement.get('status')!r}")
    if ruling.get("baseline_id") != baseline.get("id"):
        errors.append(f"{rid}: ruling baseline_id does not match inventory baseline")
    if ruling.get("baseline_revision") != baseline.get("revision"):
        errors.append(f"{rid}: ruling baseline_revision does not match inventory baseline")
    if not _HASH_RE.fullmatch(str(ruling.get("content_sha256", ""))):
        errors.append(f"{rid}: ruling.content_sha256 must be a lowercase sha256")
    return errors


def _inventory_errors(document: dict[str, Any]) -> list[str]:
    errors = []
    allowed = {
        "document_type", "schema_version", "inventory_version", "base_sha",
        "baseline", "state_contract", "requirements", "inventory_sha256",
    }
    unknown = sorted(set(document) - allowed)
    if unknown:
        errors.append(f"inventory has unknown fields: {', '.join(unknown)}")
    if document.get("document_type") != "requirement_inventory":
        errors.append("document_type must be requirement_inventory")
    if document.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    version = document.get("inventory_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("inventory_version must be a positive integer")
    if not _COMMIT_RE.fullmatch(str(document.get("base_sha", ""))):
        errors.append("base_sha must be a full lowercase 40-character commit SHA")

    baseline = document.get("baseline")
    errors.extend(_baseline_errors(baseline, "baseline"))
    baseline = baseline if isinstance(baseline, dict) else {}

    state_contract = document.get("state_contract")
    if state_contract is not None:
        if not isinstance(state_contract, dict):
            errors.append("state_contract must be null or an object")
        else:
            expected = {"project_id", "node_key", "revision", "contract_hash"}
            missing = sorted(expected - set(state_contract))
            extra = sorted(set(state_contract) - expected)
            if missing:
                errors.append(f"state_contract missing fields: {', '.join(missing)}")
            if extra:
                errors.append(f"state_contract has unknown fields: {', '.join(extra)}")
            if not _nonempty(state_contract.get("project_id")):
                errors.append("state_contract.project_id must be non-empty")
            if not _nonempty(state_contract.get("node_key")):
                errors.append("state_contract.node_key must be non-empty")
            revision = state_contract.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                errors.append("state_contract.revision must be a positive integer")
            if not _HASH_RE.fullmatch(str(state_contract.get("contract_hash", ""))):
                errors.append("state_contract.contract_hash must be a lowercase sha256")

    requirements = document.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements must be a non-empty array")
        requirements = []
    ids = []
    for index, requirement in enumerate(requirements):
        prefix = f"requirements[{index}]"
        if not isinstance(requirement, dict):
            errors.append(f"{prefix} must be an object")
            continue
        allowed_req = {"id", "source_locator", "status", "ruling"}
        extra = sorted(set(requirement) - allowed_req)
        if extra:
            errors.append(f"{prefix} has unknown fields: {', '.join(extra)}")
        rid = requirement.get("id")
        if not _ID_RE.fullmatch(str(rid or "")):
            errors.append(f"{prefix}.id must be a stable lowercase id")
        else:
            ids.append(rid)
        if not _nonempty(requirement.get("source_locator")):
            errors.append(f"{prefix}.source_locator must be non-empty")
        status = requirement.get("status")
        if status not in {"active", *_INACTIVE}:
            errors.append(f"{prefix}.status must be active, withdrawn or superseded")
        elif status == "active":
            if requirement.get("ruling") is not None:
                errors.append(f"{rid}: active requirement must not carry a ruling")
        else:
            errors.extend(_ruling_errors(requirement.get("ruling"),
                                         requirement, baseline))
    duplicates = sorted(k for k, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate requirement ids: {', '.join(duplicates)}")

    actual_hash = hash_document(document)
    if document.get("inventory_sha256") != actual_hash:
        errors.append(
            f"inventory_sha256 mismatch: expected {actual_hash}, "
            f"got {document.get('inventory_sha256')!r}")
    return errors


def _manifest_ids(step3: Path) -> tuple[set[str], list[str]]:
    manifest, errors = _load(step3 / "tasks_manifest.json", "tasks_manifest.json")
    if errors:
        return set(), errors
    order = manifest.get("execution_order")
    if not isinstance(order, list):
        return set(), ["tasks_manifest.json execution_order must be an array"]
    ids: list[str] = []
    for group in order:
        if isinstance(group, str):
            ids.append(group)
        elif isinstance(group, list):
            ids.extend(item for item in group if isinstance(item, str))
        else:
            return set(), ["tasks_manifest.json groups must be strings or arrays"]
    duplicates = sorted(k for k, count in Counter(ids).items() if count > 1)
    result_errors = []
    if duplicates:
        result_errors.append(f"duplicate manifest card ids: {', '.join(duplicates)}")
    return set(ids), result_errors


def _coverage_errors(ledger: dict[str, Any], inventory: dict[str, Any],
                     step3: Path) -> list[str]:
    errors = []
    allowed = {
        "document_type", "schema_version", "ledger_version",
        "inventory_sha256", "base_sha", "baseline", "entries", "ledger_sha256",
    }
    unknown = sorted(set(ledger) - allowed)
    if unknown:
        errors.append(f"coverage ledger has unknown fields: {', '.join(unknown)}")
    if ledger.get("document_type") != "coverage_ledger":
        errors.append("document_type must be coverage_ledger")
    if ledger.get("schema_version") != 1:
        errors.append("coverage ledger schema_version must be 1")
    version = ledger.get("ledger_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("ledger_version must be a positive integer")
    if ledger.get("inventory_sha256") != inventory.get("inventory_sha256"):
        errors.append("inventory_sha256 does not identify the approved inventory")
    if ledger.get("base_sha") != inventory.get("base_sha"):
        errors.append("coverage ledger base_sha conflicts with the inventory")
    errors.extend(_baseline_errors(ledger.get("baseline"), "coverage ledger baseline"))
    if ledger.get("baseline") != inventory.get("baseline"):
        errors.append("coverage ledger baseline conflicts with the inventory")

    actual_hash = hash_document(ledger)
    if ledger.get("ledger_sha256") != actual_hash:
        errors.append(
            f"ledger_sha256 mismatch: expected {actual_hash}, "
            f"got {ledger.get('ledger_sha256')!r}")

    requirements = {
        item["id"]: item for item in inventory.get("requirements", [])
        if isinstance(item, dict) and _nonempty(item.get("id"))
    }
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        errors.append("coverage ledger entries must be an array")
        entries = []

    entry_ids = [
        entry.get("requirement_id") for entry in entries
        if isinstance(entry, dict) and _nonempty(entry.get("requirement_id"))
    ]
    duplicates = sorted(k for k, count in Counter(entry_ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate coverage entries: {', '.join(duplicates)}")
    missing = sorted(set(requirements) - set(entry_ids))
    unknown_ids = sorted(set(entry_ids) - set(requirements))
    if missing:
        errors.append(f"missing coverage for requirements: {', '.join(missing)}")
    if unknown_ids:
        errors.append(f"unknown requirements in coverage ledger: {', '.join(unknown_ids)}")

    manifest_ids, manifest_errors = _manifest_ids(step3)
    errors.extend(manifest_errors)
    covered_cards: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entries[{index}] must be an object")
            continue
        rid = entry.get("requirement_id")
        requirement = requirements.get(rid)
        if requirement is None:
            continue
        disposition = entry.get("disposition")
        if requirement.get("status") == "active":
            if disposition != "card":
                errors.append(f"{rid}: active requirement needs a manifest card")
                continue
            card_id = entry.get("card_id")
            if not _nonempty(card_id) or card_id not in manifest_ids:
                errors.append(
                    f"{rid}: manifest card {card_id!r} does not exist in execution_order")
                continue
            if not (step3 / "tasks" / f"{card_id}.json").is_file():
                errors.append(f"{rid}: manifest card {card_id!r} has no task file")
                continue
            if set(entry) != {"requirement_id", "disposition", "card_id"}:
                errors.append(f"{rid}: card disposition has unexpected fields")
            covered_cards.add(card_id)
        else:
            if disposition == "card":
                errors.append(
                    f"{rid}: {requirement.get('status')} requirement cannot create "
                    "a card, including a STOP or hollow disposition card")
                continue
            if disposition != "ruling":
                errors.append(
                    f"{rid}: {requirement.get('status')} requirement needs ruling disposition")
                continue
            if set(entry) != {"requirement_id", "disposition", "ruling"}:
                errors.append(f"{rid}: ruling disposition has unexpected fields")
            if entry.get("ruling") != requirement.get("ruling"):
                errors.append(
                    f"{rid}: ruling differs from the approved authoritative ruling")

    ungrounded_cards = sorted(manifest_ids - covered_cards)
    if ungrounded_cards:
        errors.append(
            "manifest cards have no active requirement coverage: "
            + ", ".join(ungrounded_cards))
    return errors


def _roots(workspace_root: str, step_dir: str, out_dir: str,
           config_name: str = "") -> tuple[Path | None, Path | None, list[str]]:
    raw = workspace_root or step_dir or out_dir
    if not raw:
        return None, None, ["workspace_root is required"]
    root = Path(raw)
    if not root.is_absolute():
        return None, None, ["workspace_root must be absolute"]

    if (root / "requirement_inventory.json").is_file():
        if (root / "requirements_coverage.json").is_file():
            return root.parent / "2", root, []
        return root, None, []

    if config_name and (root / config_name / "2" / "requirement_inventory.json").is_file():
        graph = root / config_name
        return graph / "2", graph / "3", []

    # A project workspace may contain any composed DPE graph name (for example
    # dpe_game_trace), so never hard-code dpe_default_v2 here. The approved
    # inventory uniquely identifies the graph/run artifact we must read.
    graphs = sorted({candidate.parent.parent
                     for candidate in root.glob("*/2/requirement_inventory.json")
                     if candidate.is_file()})
    if len(graphs) == 1:
        graph = graphs[0]
        return graph / "2", graph / "3", []
    if len(graphs) > 1:
        return None, None, [
            f"multiple requirement inventories found from {root}; current graph is ambiguous"
        ]

    if root.name.startswith("3") and root.parent.is_dir():
        return root.parent / "2", root, []
    return None, None, [f"requirement inventory not found from {root}"]


def _git_base_error(project_root: str, base_sha: Any) -> str:
    """Require the inventory base to equal the checkout at authoring time."""
    if not project_root or not _COMMIT_RE.fullmatch(str(base_sha or "")):
        return ""
    root = Path(project_root)
    if not root.is_absolute():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    head = proc.stdout.strip()
    if proc.returncode == 0 and head == base_sha:
        return ""
    return (
        f"inventory base_sha {base_sha!r} does not equal the current checkout {head!r}"
    )


def _state_context(step2: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Read the immutable State attempt envelope from the published run seed."""
    seed = step2.parent / "project_brief.md"
    try:
        raw = seed.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        return None, [f"State seed unreadable: {exc}"]
    if not raw.startswith(_STATE_HEADING):
        return None, []
    remainder = raw[len(_STATE_HEADING):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(remainder)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"State seed has invalid frozen context: {exc}"]
    if not isinstance(value, dict):
        return None, ["State seed frozen context must be an object"]
    return value, []


def _expected_state_contract(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": context.get("state_project_id"),
        "node_key": context.get("node_key"),
        "revision": context.get("revision"),
        "contract_hash": context.get("contract_hash"),
    }


def _expected_baseline(
    step2: Path, state_context: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Derive baseline identity from a frozen source, never from the inventory."""
    if state_context is not None:
        design = state_context.get("design_context")
        if isinstance(design, dict) and design.get("baseline_id"):
            return {
                "id": design.get("baseline_id"),
                "revision": design.get("manifest_hash"),
            }, []
        return {
            "id": "state_contract",
            "revision": state_context.get("contract_hash"),
        }, []

    candidates = (
        (step2.parent.parent / "meta_conversation" / "finalize" /
         "step1_goals.json", "meta_conversation/finalize/step1_goals.json"),
        (step2.parent / "project_brief.md", "project_brief.md"),
    )
    for path, source_id in candidates:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return None, [f"baseline source unreadable: {exc}"]
        return {
            "id": source_id,
            "revision": hashlib.sha256(content).hexdigest(),
        }, []
    return None, []


def _authority_errors(inventory: dict[str, Any], step2: Path) -> list[str]:
    """Bind provenance-shaped fields to the frozen run authority, when present."""
    context, errors = _state_context(step2)
    if errors:
        return errors
    actual = inventory.get("state_contract")
    if context is None:
        if actual is not None:
            errors.append("state_contract must be null outside a State attempt")
    else:
        expected = _expected_state_contract(context)
        if actual != expected:
            errors.append(
                "state_contract does not equal the frozen State attempt context")
    expected_baseline, baseline_errors = _expected_baseline(step2, context)
    errors.extend(baseline_errors)
    if expected_baseline is not None and inventory.get("baseline") != expected_baseline:
        errors.append("inventory baseline does not equal the frozen authority baseline")
    return errors


def _git_head(project_root: str) -> str:
    if not project_root:
        return ""
    root = Path(project_root)
    if not root.is_absolute():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and _COMMIT_RE.fullmatch(sha) else ""


def requirement_coverage(
    document: dict[str, Any] | None = None,
    *,
    workspace_root: str = "",
    project_root: str = "",
    step_dir: str = "",
    out_dir: str = "",
    config_name: str = "",
    **_ignored: Any,
) -> dict[str, Any]:
    """Hash a proposed document, validate a step, or emit compact reviewer context."""
    if document is not None:
        if not isinstance(document, dict):
            return {"passed": False, "error": "document must be an object"}
        document_type = document.get("document_type")
        fields = {
            "requirement_inventory": "inventory_sha256",
            "coverage_ledger": "ledger_sha256",
        }
        hash_field = fields.get(document_type)
        if hash_field is None:
            return {"passed": False,
                    "error": "document_type must be requirement_inventory or coverage_ledger"}
        return {
            "passed": True,
            "document_type": document_type,
            "hash_field": hash_field,
            "sha256": hash_document(document),
        }

    step2, step3, root_errors = _roots(
        workspace_root, step_dir, out_dir, config_name)
    if root_errors:
        head = _git_head(project_root)
        # Before Step 2 writes an inventory, a project-workspace context source
        # intentionally exposes only the checkout authority envelope. A step
        # staging directory with a missing inventory remains a hard failure.
        root = Path(workspace_root or step_dir or out_dir) if (
            workspace_root or step_dir or out_dir) else None
        is_project_workspace = bool(
            root and root.is_absolute()
            and root.name not in {"2", "3"}
            and not root.name.endswith(".tmp"))
        if head and is_project_workspace:
            graph = root / config_name if config_name else root
            state_context, state_errors = _state_context(graph / "2")
            if state_errors:
                return {"passed": False, "error": "; ".join(state_errors)}
            lines = [
                "[requirement_authority_context]",
                f"base_sha={head}",
            ]
            if state_context is None:
                lines.append("state_contract=null")
            else:
                lines.append(
                    "state_contract=" + json.dumps(
                        _expected_state_contract(state_context),
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    )
                )
            expected_baseline, baseline_errors = _expected_baseline(
                graph / "2", state_context)
            if baseline_errors:
                return {"passed": False, "error": "; ".join(baseline_errors)}
            if expected_baseline is not None:
                lines.append("baseline=" + json.dumps(
                    expected_baseline, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")))
            lines.append(
                "Use these exact values in requirement_inventory.json. Reports "
                "and model opinions are observations; only owner or State "
                "sources may withdraw or supersede a requirement."
            )
            return {
                "passed": True,
                "phase": "authority_context",
                "base_sha": head,
                "state_contract": (_expected_state_contract(state_context)
                                   if state_context is not None else None),
                "content": "\n".join(lines),
            }
        return {"passed": False, "error": "; ".join(root_errors)}

    inventory, errors = _load(
        step2 / "requirement_inventory.json", "requirement_inventory.json")
    if inventory is None:
        return {"passed": False, "error": "; ".join(errors)}
    errors.extend(_inventory_errors(inventory))
    errors.extend(_authority_errors(inventory, step2))
    # Exact checkout equality is an authoring-time gate. Later context-source
    # reads may see commits made by prior DPE steps; the promoted inventory is
    # then the frozen, content-addressed base authority.
    raw_root = Path(workspace_root or step_dir or out_dir)
    if raw_root.name.startswith("2"):
        base_error = _git_base_error(project_root, inventory.get("base_sha"))
        if base_error:
            errors.append(base_error)

    if step3 is None or not (step3 / "requirements_coverage.json").is_file():
        if errors:
            return {"passed": False, "error": "; ".join(errors)}
        digest = inventory["inventory_sha256"]
        lines = [
            "[requirement_inventory]",
            f"inventory_version={inventory['inventory_version']}",
            f"inventory_sha256={digest}",
            f"base_sha={inventory['base_sha']}",
            "baseline="
            f"{inventory['baseline']['id']}@{inventory['baseline']['revision']}",
        ]
        for requirement in inventory["requirements"]:
            line = (
                f"{requirement['id']} [{requirement['status']}] "
                f"{requirement['source_locator']}")
            if requirement["status"] in _INACTIVE:
                ruling = requirement["ruling"]
                line += (
                    f" -> {ruling['authority']}:{ruling['source_id']}"
                    f"@{ruling['revision']}")
            lines.append(line)
        return {
            "passed": True,
            "phase": "inventory",
            "inventory_sha256": digest,
            "requirements": len(inventory["requirements"]),
            "content": "\n".join(lines),
        }

    ledger, ledger_load_errors = _load(
        step3 / "requirements_coverage.json", "requirements_coverage.json")
    errors.extend(ledger_load_errors)
    if ledger is not None:
        errors.extend(_coverage_errors(ledger, inventory, step3))
    if errors:
        return {"passed": False, "error": "; ".join(errors)}

    lines = [
        "[validated_requirement_coverage]",
        f"inventory_sha256={inventory['inventory_sha256']}",
        f"ledger_sha256={ledger['ledger_sha256']}",
        f"base_sha={inventory['base_sha']}",
        "baseline="
        f"{inventory['baseline']['id']}@{inventory['baseline']['revision']}",
    ]
    for entry in ledger["entries"]:
        if entry["disposition"] == "card":
            target = f"card {entry['card_id']}"
        else:
            target = f"ruling {entry['ruling']['source_id']}"
        lines.append(f"{entry['requirement_id']} -> {target}")
    return {
        "passed": True,
        "phase": "coverage",
        "inventory_sha256": inventory["inventory_sha256"],
        "ledger_sha256": ledger["ledger_sha256"],
        "requirements": len(inventory["requirements"]),
        "cards": len({
            entry["card_id"] for entry in ledger["entries"]
            if entry["disposition"] == "card"
        }),
        "content": "\n".join(lines),
    }
