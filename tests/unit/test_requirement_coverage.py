"""Executable contract tests for requirement inventory and coverage ledger."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from aitelier.tools.requirement_coverage.impl import (
    hash_document,
    requirement_coverage,
)

ROOT = Path(__file__).resolve().parents[2]
BASE_SHA = "f" * 40
RULING_SHA = "a" * 64
REGRESSION_RUN_ID = "4a2d71bf-2d29-40ee-9e85-43a91a4f3537"


def _inventory(*, c7_status: str = "withdrawn", authority: str = "owner") -> dict:
    c7 = {
        "id": "brief-3-action-pick",
        "source_locator": "project_brief.md#3",
        "status": c7_status,
    }
    if c7_status != "active":
        c7["ruling"] = {
            "authority": authority,
            "source_id": (
                f"aitelier-run:{REGRESSION_RUN_ID}/trace/607"
            ),
            "revision": "2026-09-12(c)",
            "baseline_id": "r8",
            "baseline_revision": 8,
            "decision": c7_status,
            "content_sha256": RULING_SHA,
        }
    doc = {
        "document_type": "requirement_inventory",
        "schema_version": 1,
        "inventory_version": 1,
        "base_sha": BASE_SHA,
        "baseline": {"id": "r8", "revision": 8},
        "requirements": [
            {
                "id": "brief-1-mainline-green",
                "source_locator": "project_brief.md#1",
                "status": "active",
            },
            c7,
        ],
    }
    doc["inventory_sha256"] = hash_document(doc)
    return doc


def _ledger(inventory: dict, *, c7_disposition: str = "ruling") -> dict:
    entries = [
        {
            "requirement_id": "brief-1-mainline-green",
            "disposition": "card",
            "card_id": "fix_mainline",
        }
    ]
    if c7_disposition == "ruling":
        entries.append({
            "requirement_id": "brief-3-action-pick",
            "disposition": "ruling",
            "ruling": copy.deepcopy(inventory["requirements"][1]["ruling"]),
        })
    elif c7_disposition == "card":
        entries.append({
            "requirement_id": "brief-3-action-pick",
            "disposition": "card",
            "card_id": "record_action_pick_block_absent",
        })
    doc = {
        "document_type": "coverage_ledger",
        "schema_version": 1,
        "ledger_version": 1,
        "inventory_sha256": inventory["inventory_sha256"],
        "base_sha": inventory["base_sha"],
        "baseline": copy.deepcopy(inventory["baseline"]),
        "entries": entries,
    }
    doc["ledger_sha256"] = hash_document(doc)
    return doc


def _tree(tmp_path: Path, inventory: dict, ledger: dict, *,
          cards: tuple[str, ...] = ("fix_mainline",)) -> Path:
    graph = tmp_path / "dpe_default_v2"
    step2 = graph / "2"
    step3 = graph / "3.tmp"
    (step3 / "tasks").mkdir(parents=True)
    step2.mkdir(parents=True)
    (step2 / "requirement_inventory.json").write_text(
        json.dumps(inventory), encoding="utf-8")
    (step3 / "requirements_coverage.json").write_text(
        json.dumps(ledger), encoding="utf-8")
    (step3 / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": [list(cards)]}), encoding="utf-8")
    for card in cards:
        (step3 / "tasks" / f"{card}.json").write_text(
            json.dumps({"id": card, "description": "real implementation"}),
            encoding="utf-8")
    return step3


def test_c7_owner_withdrawal_is_covered_without_hollow_card(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    step3 = _tree(tmp_path, inventory, ledger)

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is True
    assert result["inventory_sha256"] == inventory["inventory_sha256"]
    assert result["ledger_sha256"] == ledger["ledger_sha256"]
    assert (
        "brief-3-action-pick -> ruling "
        f"aitelier-run:{REGRESSION_RUN_ID}/trace/607"
    ) in result["content"]
    assert "record_action_pick_block_absent" not in result["content"]
    assert "review_verdict.json" not in result["content"]


def test_4a2d_c7_fixed_regression_uses_one_compact_ledger_for_pm_and_review(
        tmp_path):
    fixture = json.loads((
        ROOT / "tests/fixtures/context_ruling_propagation_4a2d71bf_c7.json"
    ).read_text(encoding="utf-8"))
    assert fixture["run_id"] == REGRESSION_RUN_ID

    inventory = _inventory()
    ruling = inventory["requirements"][1]["ruling"]
    ruling["source_id"] = fixture["captured_source"]["ruling_source_id"]
    ruling["content_sha256"] = hash_document({
        "owner_ruling_excerpt":
            fixture["captured_source"]["owner_ruling_excerpt"],
    })
    inventory["inventory_sha256"] = hash_document(inventory)
    ledger = _ledger(inventory)
    step3 = _tree(tmp_path, inventory, ledger)

    planner_view = requirement_coverage(workspace_root=str(step3))
    promoted = step3.parent / "3"
    step3.rename(promoted)
    reviewer_view = requirement_coverage(
        workspace_root=str(tmp_path),
        config_name=step3.parent.name,
    )

    assert planner_view["passed"] is True
    assert reviewer_view["passed"] is True
    assert planner_view["ledger_sha256"] == reviewer_view["ledger_sha256"]
    assert planner_view["content"] == reviewer_view["content"]
    assert fixture["review_input"]["brief_section_3_excerpt"] not in (
        reviewer_view["content"])
    assert fixture["review_output"]["summary"] not in reviewer_view["content"]
    assert fixture["invalid_retry_output"]["card_id"] not in (
        reviewer_view["content"])
    assert all(
        fixture["invalid_retry_output"]["card_id"] not in wave
        for wave in fixture["planning_output"]["execution_order"]
    )


def test_4a2d_c7_stop_record_retry_is_rejected(tmp_path):
    fixture = json.loads((
        ROOT / "tests/fixtures/context_ruling_propagation_4a2d71bf_c7.json"
    ).read_text(encoding="utf-8"))
    inventory = _inventory()
    ledger = _ledger(inventory, c7_disposition="card")
    hollow = fixture["invalid_retry_output"]["card_id"]
    ledger["entries"][1]["card_id"] = hollow
    ledger["ledger_sha256"] = hash_document(ledger)
    step3 = _tree(tmp_path, inventory, ledger, cards=("fix_mainline", hollow))

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "withdrawn requirement cannot create a card" in result["error"]


def test_real_active_requirement_without_card_is_rejected(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    ledger["entries"][0]["card_id"] = "missing"
    ledger["ledger_sha256"] = hash_document(ledger)
    step3 = _tree(tmp_path, inventory, ledger)

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "manifest card" in result["error"]
    assert "missing" in result["error"]


def test_withdrawn_requirement_cannot_be_a_stop_or_hollow_card(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory, c7_disposition="card")
    step3 = _tree(
        tmp_path, inventory, ledger,
        cards=("fix_mainline", "record_action_pick_block_absent"))

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "withdrawn requirement" in result["error"]


@pytest.mark.parametrize("authority", ["model", "report", "reviewer"])
def test_non_authoritative_withdrawal_is_rejected(tmp_path, authority):
    inventory = _inventory(authority=authority)
    step2 = tmp_path / "2"
    step2.mkdir()
    (step2 / "requirement_inventory.json").write_text(
        json.dumps(inventory), encoding="utf-8")

    result = requirement_coverage(workspace_root=str(step2))

    assert result["passed"] is False
    assert "authority" in result["error"]


def test_baseline_or_base_conflict_is_rejected(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    ledger["baseline"]["revision"] = 7
    ledger["base_sha"] = "e" * 40
    ledger["ledger_sha256"] = hash_document(ledger)
    step3 = _tree(tmp_path, inventory, ledger)

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "base_sha" in result["error"]
    assert "baseline" in result["error"]


def test_missing_duplicate_and_unknown_requirements_are_rejected(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    ledger["entries"] = [
        ledger["entries"][0],
        copy.deepcopy(ledger["entries"][0]),
        {"requirement_id": "invented", "disposition": "card",
         "card_id": "fix_mainline"},
    ]
    ledger["ledger_sha256"] = hash_document(ledger)
    step3 = _tree(tmp_path, inventory, ledger)

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "duplicate" in result["error"]
    assert "missing coverage" in result["error"]
    assert "unknown" in result["error"]


def test_hashes_are_content_addressed_and_mismatch_is_rejected(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    step3 = _tree(tmp_path, inventory, ledger)
    inventory["requirements"][0]["source_locator"] = "changed"
    (step3.parent / "2" / "requirement_inventory.json").write_text(
        json.dumps(inventory), encoding="utf-8")

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert "inventory_sha256" in result["error"]


def test_hash_service_strips_only_the_document_hash_field():
    inventory = _inventory()
    expected = inventory["inventory_sha256"]
    assert requirement_coverage(document=inventory) == {
        "passed": True,
        "document_type": "requirement_inventory",
        "hash_field": "inventory_sha256",
        "sha256": expected,
    }


def test_context_source_finds_composed_graph_and_reuses_exact_ledger(tmp_path):
    inventory = _inventory()
    ledger = _ledger(inventory)
    step3 = _tree(tmp_path, inventory, ledger)
    graph = step3.parent
    step3.rename(graph / "3")
    graph.rename(tmp_path / "dpe_game_trace")

    result = requirement_coverage(workspace_root=str(tmp_path))

    assert result["passed"] is True
    assert result["ledger_sha256"] == ledger["ledger_sha256"]


def test_context_source_uses_injected_graph_when_project_has_history(tmp_path):
    old_inventory = _inventory()
    old_ledger = _ledger(old_inventory)
    old_step3 = _tree(tmp_path, old_inventory, old_ledger)
    old_step3.rename(old_step3.parent / "3")
    old_step3.parent.rename(tmp_path / "dpe_old")

    current_inventory = _inventory(c7_status="active")
    current_ledger = _ledger(current_inventory, c7_disposition="card")
    current_step3 = _tree(
        tmp_path, current_inventory, current_ledger,
        cards=("fix_mainline", "record_action_pick_block_absent"))
    current_step3.rename(current_step3.parent / "3")
    current_step3.parent.rename(tmp_path / "dpe_game_trace")

    result = requirement_coverage(
        workspace_root=str(tmp_path), config_name="dpe_game_trace")

    assert result["passed"] is True
    assert result["ledger_sha256"] == current_ledger["ledger_sha256"]


def test_dpe_wires_inventory_ledger_validation_before_review():
    data = yaml.safe_load((ROOT / "configs" / "dpe_default.yaml").read_text())
    steps = {step["id"]: step for step in data["steps"]}
    step2 = steps["2"]
    step3 = steps["3"]
    reviewer = steps["3_review"]

    assert step2["output"]["fixed"]["requirement_inventory"]["file"] == (
        "requirement_inventory.json")
    assert any(v.get("tool") == "requirement_coverage"
               for v in step2["validation"])
    assert step3["output"]["fixed"]["coverage_ledger"]["file"] == (
        "requirements_coverage.json")
    assert any(v.get("tool") == "requirement_coverage"
               for v in step3["validation"])
    assert {"source": {"feedback_of": "2"}} in step2["context"]
    assert {"source": {"tool": "requirement_coverage"}} in step3["context"]
    assert {"source": {"feedback_of": "2"}} not in step3["context"]
    assert {"source": {"feedback_of": "1"}} not in step3["context"]
    assert {"source": {"tool": "requirement_coverage"}} in reviewer["context"]
    assert any(t.get("to") == "3_budget" for t in step3["transitions"])


@pytest.mark.parametrize("field", ["source_id", "revision"])
@pytest.mark.parametrize("bad", [None, "", "   ", True, 1, {}, []])
def test_ruling_source_and_revision_require_real_strings(tmp_path, field, bad):
    inventory = _inventory()
    inventory["requirements"][1]["ruling"][field] = bad
    inventory["inventory_sha256"] = hash_document(inventory)
    ledger = _ledger(inventory)
    step3 = _tree(tmp_path, inventory, ledger)

    result = requirement_coverage(workspace_root=str(step3))

    assert result["passed"] is False
    assert f"ruling.{field} must be non-empty" in result["error"]


def _git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Authority test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "authority@test.invalid"], cwd=repo, check=True)
    (repo / "one").write_text("one\n")
    subprocess.run(["git", "add", "one"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                           capture_output=True, text=True).stdout.strip()
    (repo / "two").write_text("two\n")
    subprocess.run(["git", "add", "two"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    return repo, first, head


def _state_seed(context: dict) -> str:
    return "# State goal attempt\n\n" + json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def test_inventory_base_must_equal_head_not_merely_be_an_ancestor(tmp_path):
    repo, ancestor, _ = _git_repo(tmp_path)
    inventory = _inventory()
    inventory["base_sha"] = ancestor
    inventory["inventory_sha256"] = hash_document(inventory)
    step2 = tmp_path / "graph" / "2.tmp"
    step2.mkdir(parents=True)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))

    result = requirement_coverage(
        workspace_root=str(step2), project_root=str(repo))

    assert result["passed"] is False
    assert "does not equal the current checkout" in result["error"]


def test_state_contract_and_design_baseline_equal_frozen_seed(tmp_path):
    repo, _, head = _git_repo(tmp_path)
    state = {
        "state_project_id": "state-project",
        "node_key": "feature.node",
        "revision": 3,
        "contract_hash": "b" * 64,
        "design_context": {"baseline_id": "design-b9", "manifest_hash": "c" * 64},
    }
    graph = tmp_path / "graph"
    step2 = graph / "2.tmp"
    step2.mkdir(parents=True)
    (graph / "project_brief.md").write_text(_state_seed(state))
    inventory = _inventory()
    inventory["base_sha"] = head
    inventory["state_contract"] = {
        "project_id": "state-project", "node_key": "feature.node",
        "revision": 3, "contract_hash": "b" * 64,
    }
    inventory["baseline"] = {"id": "design-b9", "revision": "c" * 64}
    inventory["requirements"][1]["ruling"]["baseline_id"] = "design-b9"
    inventory["requirements"][1]["ruling"]["baseline_revision"] = "c" * 64
    inventory["inventory_sha256"] = hash_document(inventory)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))

    assert requirement_coverage(
        workspace_root=str(step2), project_root=str(repo))["passed"] is True

    for mutation, expected in (
        (lambda d: d.__setitem__("state_contract", None), "frozen State"),
        (lambda d: d.__setitem__("state_contract", {
            **inventory["state_contract"], "node_key": "other.node"}), "frozen State"),
        (lambda d: d.__setitem__("baseline", {
            "id": "old-baseline", "revision": "c" * 64}), "frozen authority baseline"),
    ):
        broken = copy.deepcopy(inventory)
        mutation(broken)
        broken["inventory_sha256"] = hash_document(broken)
        (step2 / "requirement_inventory.json").write_text(json.dumps(broken))
        result = requirement_coverage(workspace_root=str(step2), project_root=str(repo))
        assert result["passed"] is False
        assert expected in result["error"]


def test_state_without_design_context_uses_contract_as_frozen_baseline(tmp_path):
    repo, _, head = _git_repo(tmp_path)
    state = {
        "state_project_id": "state-project",
        "node_key": "feature.node",
        "revision": 3,
        "contract_hash": "b" * 64,
    }
    graph = tmp_path / "graph"
    step2 = graph / "2.tmp"
    step2.mkdir(parents=True)
    (graph / "project_brief.md").write_text(_state_seed(state))

    authority = requirement_coverage(
        workspace_root=str(graph), project_root=str(repo))
    assert authority["passed"] is True
    assert authority["state_contract"] == {
        "project_id": "state-project", "node_key": "feature.node",
        "revision": 3, "contract_hash": "b" * 64,
    }
    assert 'baseline={"id":"state_contract","revision":"' in (
        authority["content"])

    inventory = _inventory()
    inventory["base_sha"] = head
    inventory["state_contract"] = authority["state_contract"]
    inventory["baseline"] = {"id": "state_contract", "revision": "b" * 64}
    inventory["requirements"][1]["ruling"]["baseline_id"] = "state_contract"
    inventory["requirements"][1]["ruling"]["baseline_revision"] = "b" * 64
    inventory["inventory_sha256"] = hash_document(inventory)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))
    assert requirement_coverage(
        workspace_root=str(step2), project_root=str(repo))["passed"] is True


def test_non_state_baseline_is_bound_to_frozen_goals_file(tmp_path):
    repo, _, head = _git_repo(tmp_path)
    goals = tmp_path / "meta_conversation" / "finalize" / "step1_goals.json"
    goals.parent.mkdir(parents=True)
    goals.write_text('{"mvp_goals":["A"]}')
    graph = tmp_path / "graph"
    graph.mkdir()

    authority = requirement_coverage(
        workspace_root=str(graph), project_root=str(repo))
    expected = {
        "id": "meta_conversation/finalize/step1_goals.json",
        "revision": __import__("hashlib").sha256(goals.read_bytes()).hexdigest(),
    }
    assert authority["passed"] is True
    assert json.dumps(expected, sort_keys=True, separators=(",", ":")) in (
        authority["content"])

    step2 = graph / "2.tmp"
    step2.mkdir()
    inventory = _inventory()
    inventory["base_sha"] = head
    inventory["baseline"] = expected
    inventory["requirements"][1]["ruling"]["baseline_id"] = expected["id"]
    inventory["requirements"][1]["ruling"]["baseline_revision"] = expected["revision"]
    inventory["inventory_sha256"] = hash_document(inventory)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))
    assert requirement_coverage(
        workspace_root=str(step2), project_root=str(repo))["passed"] is True

    inventory["baseline"] = {"id": "self-asserted", "revision": 1}
    inventory["requirements"][1]["ruling"]["baseline_id"] = "self-asserted"
    inventory["requirements"][1]["ruling"]["baseline_revision"] = 1
    inventory["inventory_sha256"] = hash_document(inventory)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))
    result = requirement_coverage(
        workspace_root=str(step2), project_root=str(repo))
    assert result["passed"] is False
    assert "frozen authority baseline" in result["error"]


def test_non_state_run_rejects_self_asserted_state_contract(tmp_path):
    inventory = _inventory()
    inventory["state_contract"] = {
        "project_id": "invented", "node_key": "invented.node",
        "revision": 1, "contract_hash": "d" * 64,
    }
    inventory["inventory_sha256"] = hash_document(inventory)
    step2 = tmp_path / "graph" / "2"
    step2.mkdir(parents=True)
    (step2 / "requirement_inventory.json").write_text(json.dumps(inventory))

    result = requirement_coverage(workspace_root=str(step2))

    assert result["passed"] is False
    assert "must be null outside a State attempt" in result["error"]
