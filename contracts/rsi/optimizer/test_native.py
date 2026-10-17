"""PROOF: the SAME literal round scenarios on the genuine host binding.

Runs only in the authorized isolated SDK1.5.94 container. Every scenario uses a
fresh native world (``native_fixture``): the round is an ordinary engine run of
``round.graph.json`` whose tool steps are the NEW ports; every measured cell is
the accepted NativeEvaluator (ordinary launch/claim/runner, real gateway sends
admitted by the real transport boundary, deterministic transport FIXTURE as the
provider, trusted checker, BenchmarkStore rows). Evidence: $OPTIMIZER_EVIDENCE.
"""
import hashlib
import importlib
import importlib.metadata
import json
import os
import traceback
from pathlib import Path

import pytest

from .conformance import SCENARIOS, run_scenario
from .contract import OptimizerError
from .native_fixture import NativeRoundFactory


def evidence(name, value):
    target = os.getenv("OPTIMIZER_EVIDENCE")
    if target:
        Path(target, name + ".json").write_text(json.dumps(value, indent=1, default=repr) + "\n")


@pytest.fixture
def factory(tmp_path_factory):
    base = Path(os.getenv("RSI_WORK") or tmp_path_factory.mktemp("worlds"))
    made = NativeRoundFactory(base)
    yield made
    made.close()


@pytest.mark.parametrize("vector", SCENARIOS, ids=lambda v: v["name"])
def test_same_shared_round_scenarios_native(vector, factory):
    result, error = None, None
    try:
        result = run_scenario(factory, vector)
    except BaseException as exc:                                    # noqa: BLE001
        error = traceback.format_exc()
        raise
    finally:
        native = factory.made[0].native_evidence() if factory.made else None
        evidence("native-round-" + vector["name"], {"vector": vector, "result": result,
                                                    "error": error, "native": native})
    sdk, view = native["sdk"], result["observations"]["view"]
    # Every successful invocation is exactly one engine-dispatched, completed
    # tool step of this round's run, in the same order.
    done = sorted((s for s in sdk.get("steps") or () if s["status"] == "completed"),
                  key=lambda s: s["completion_seq"])
    assert [s["step_id"] for s in done] == result["ports"]
    approved = [e["checkpoint_approved"] for e in result["observations"]["events"]
                if "checkpoint_approved" in e]
    assert [a["step_id"] for a in sdk.get("checkpoint_approvals") or ()] == approved
    # Physical accounting: every provider-side request was admitted by the
    # evaluator's transport boundary and is in the round's known/unknown totals.
    assert native["trace_physical_sends"] == native["provider_sends"]
    assert view["known"]["requests"] + view["retained_unknown"]["requests"] == native["provider_sends"]
    if vector["name"] == "cross_call_round_budget_is_residual":
        assert native["provider_sends_by_split_arm"] == {"train/reference": 36, "train/candidate": 4}
    if vector["name"] == "original_deadline_does_not_restart":
        assert native["provider_sends_by_split_arm"] == {"train/reference": 36}
    if not result["ports"]:
        assert native["provider_sends"] == native["evaluator_cell_launches"] == 0


def test_engine_dispatch_without_owning_invocation_does_nothing(factory):
    """A driver advancing the round's run directly reaches the port tool with no
    owning invocation: no evaluator work, and the graph's error branch."""
    fixture = factory()
    request = fixture.prepare()
    fixture.actor = "coordinator"
    fixture.invoke("freeze", request.model_dump(mode="json"))
    sf, run_id = fixture.world.sf, fixture.round.run_id
    sf.advance_run(run_id)                                   # foreign driver
    run = sf.get_run(run_id)
    step = [s for s in sf.get_steps(run_id, include_payloads=True)
            if s["step_id"] == "train_reference"][-1]
    native = fixture.native_evidence()
    evidence("native-foreign-dispatch", {"run": {k: run[k] for k in ("status", "current_node")},
                                         "step": {k: step[k] for k in ("status", "outputs_json")},
                                         "native": native})
    assert run["current_node"] == "stop" and step["status"] == "completed"
    assert "AUTHORITY" in json.loads(step["outputs_json"])["error"]
    assert native["provider_sends"] == native["evaluator_cell_launches"] == 0
    with pytest.raises(OptimizerError) as exc:
        fixture.invoke("train_reference", {})
    assert exc.value.code == "ORDER"


def test_import_identities():
    mods = {}
    for name in ["contracts.rsi.optimizer.contract", "contracts.rsi.optimizer.conformance",
                 "contracts.rsi.optimizer.fake", "contracts.rsi.optimizer.native_binding",
                 "contracts.rsi.optimizer.native_fixture", "contracts.rsi.optimizer.native_child",
                 "contracts.rsi.evaluator.contract", "contracts.rsi.evaluator.native_fixture",
                 "contracts.rsi.evaluator.native_transport", "contracts.rsi.pipeline_versioning.contract",
                 "contracts.rsi.pipeline_versioning.native_provider", "core.pipeline_versions",
                 "core.pipeline_frozen_bundle", "core.pipeline_optimization", "core.pipeline_benchmark",
                 "core.rsi_evaluator", "core.rsi_admission", "core.rsi_policy", "core.rsi_checker",
                 "core.skillflow_host", "core.ai_router", "core.agents", "core.run_driver",
                 "core.run_launcher", "skillflow", "skillflow.core", "skillflow.graph",
                 "skillflow.tool_loader", "litellm", "openai", "httpx"]:
        m = importlib.import_module(name)
        p = Path(m.__file__)
        mods[name] = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    graph = Path(__file__).with_name("round.graph.json")
    identity = {"sdk": importlib.metadata.version("skillflow-py"),
                "litellm": importlib.metadata.version("litellm"),
                "openai": importlib.metadata.version("openai"),
                "httpx": importlib.metadata.version("httpx"),
                "pythonpath": os.getenv("PYTHONPATH"), "source_sha": os.getenv("RSI_SOURCE_SHA"),
                "image": os.getenv("RSI_IMAGE_ID"),
                "round_graph_sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
                "modules": mods}
    evidence("import-identities", identity)
    assert identity["sdk"] == "1.5.94"
    assert all(v["path"].startswith("/source/") for k, v in mods.items()
               if k.startswith(("contracts.", "core.")))
