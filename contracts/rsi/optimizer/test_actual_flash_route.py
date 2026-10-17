"""PROOF on the ACTUAL flash route: passive identity reads and one frozen binding.

Runs only in the authorized isolated SDK1.5.94 container. The world is the
accepted native round world with its single-endpoint fixture route REPLACED by
the deployment's actual provider registry and flash route (nonsecret fields of
the operator files; placeholder keys, no network): a three-entry rotate pool
with differing per-endpoint effort and a pay-as-you-go fallback. The previous
proofs used a one-member pool, which ``resolve(rotate=True)`` cannot rotate, so
they could not observe that every identity read consumed a scheduling
selection. Everything below goes through the real AgentFactory -> AIGateway ->
litellm/openai SDK -> admission path with the deterministic transport FIXTURE
as the provider. Evidence: $OPTIMIZER_EVIDENCE.
"""
import copy
import json
import os
import traceback
from pathlib import Path

import pytest

from .conformance import ROLES, SCENARIOS, run_scenario
from .native_fixture import NativeRoundFactory

# Literal copy of the nonsecret projection retained by the zero-model-call
# preparation (rsi-pilot-native-preparation-sol-20261017/live-runtime-config-safe.json,
# which records the operator files' own hashes); this test never reads the
# operator files. Only the flash route is served here, so the served route
# table's digest is this table's, not the full operator file's.
ACTUAL_PROVIDERS = {
    "ollamacloud": {"base_url": "https://ollama.com/v1", "api_key_env": "OLLAMA_API_KEY"},
    "ark": {"base_url": "https://ark.cn-beijing.volces.com/api/coding/v3", "api_key_env": "ARK_API_KEY"},
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANT_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/", "api_key_env": "DEEPSEEK_API_KEY"},
}
ACTUAL_FLASH = {
    "rotate": ["ollamacloud/deepseek-v4.1-flash", "ark/glm-5.3-flash", "anthropic/claude-haiku-5-5"],
    "fallback": ["deepseek/deepseek-flash"],
    "effort": {"localqwen/qwen3": "low", "ollamacloud/deepseek-v4.1-flash": "low",
               "ark/glm-5.3-flash": "low", "deepseek/deepseek-flash": "low",
               "anthropic/claude-haiku-5-5": "high"},
}
ENDPOINTS = {"ollama-primary": "ollamacloud/deepseek-v4.1-flash", "ark-primary": "ark/glm-5.3-flash",
             "anthropic-primary": "anthropic/claude-haiku-5-5",
             "deepseek-fallback": "deepseek/deepseek-flash"}
OLLAMA = "ollamacloud/deepseek-v4.1-flash"


def evidence(name, value):
    target = os.getenv("OPTIMIZER_EVIDENCE")
    if target:
        Path(target, name + ".json").write_text(json.dumps(value, indent=1, default=repr) + "\n")


def apply_actual(f, label):
    """Serve the actual registry/route under this world's frozen paths and bind
    the host's one concrete endpoint ``label`` (host-owned configuration)."""
    from core import rsi_admission
    from core.model_routes import reset_cache
    runtime = f.root / "runtime"
    (runtime / "llm_providers.json").write_text(json.dumps(ACTUAL_PROVIDERS, indent=1), encoding="utf-8")
    (runtime / "model_routes.json").write_text(json.dumps({"flash": ACTUAL_FLASH}, indent=1),
                                               encoding="utf-8")
    for p in ACTUAL_PROVIDERS.values():
        (f.world.secrets / p["api_key_env"]).write_text("offline-placeholder-not-a-key\n")
    reset_cache()
    host = f.evaluator.host
    host.endpoints = {lab: {"provider": c.split("/", 1)[0], "model": c.split("/", 1)[1],
                            "base_url": ACTUAL_PROVIDERS[c.split("/", 1)[0]]["base_url"]}
                      for lab, c in ENDPOINTS.items()}
    host.endpoint_label = label
    doc = copy.deepcopy(host.pricing.document)
    doc["endpoints"] = {f"{c}@{lab}": {"input": 2_000_000, "output": 8_000_000}
                        for lab, c in ENDPOINTS.items()}
    host.pricing = rsi_admission.Pricing(doc)


class ActualRouteFactory(NativeRoundFactory):
    endpoint = "ollama-primary"

    def __call__(self):
        f = super().__call__()
        apply_actual(f, self.endpoint)
        return f


@pytest.fixture
def factory(tmp_path_factory):
    base = Path(os.getenv("RSI_WORK") or tmp_path_factory.mktemp("worlds"))
    made = ActualRouteFactory(base)
    yield made
    made.close()


def counters():
    from core import model_routes
    return dict(model_routes._rot_counters)


def profile(evaluator, role):
    gateway, cfg = evaluator._gateway_profile(role)
    return {"role": role, "internal": gateway.internal_model, "active": gateway.active_model,
            "litellm_model": gateway.litellm_model,
            "effort": gateway._binding_effort() if gateway.enable_thinking else "disabled",
            "temperature": cfg.get("temperature"), "max_output_tokens": gateway.max_output_tokens,
            "max_tool_turns": cfg.get("max_tool_turns")}


def without_role(p):
    return {k: v for k, v in p.items() if k != "role"}


def test_actual_configuration_literal_matches_retained_operator_projection():
    path = os.getenv("RSI_LIVE_CONFIG_SAFE")
    if not path:
        pytest.skip("retained operator projection not mounted")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert {n: {k: v[k] for k in ("base_url", "api_key_env")} for n, v in raw["providers"].items()} \
        == ACTUAL_PROVIDERS
    assert raw["flash_route"] == ACTUAL_FLASH


def test_previous_single_route_fixture_could_not_expose_rotation(tmp_path):
    """Measured gap: the accepted fixture's flash pool has one member, so
    ordinary rotation is a no-op there; the actual pool yields three heads."""
    from core.model_routes import ModelRoutes
    f = NativeRoundFactory(tmp_path)()
    try:
        fixture_doc = json.loads((f.root / "runtime" / "model_routes.json").read_text())
        fixture_heads = [ModelRoutes(f.root / "runtime" / "model_routes.json").resolve(
            "flash", rotate=True)[0] for _ in range(4)]
        apply_actual(f, "ollama-primary")
        actual_heads = [ModelRoutes(f.root / "runtime" / "model_routes.json").resolve(
            "flash", rotate=True)[0] for _ in range(4)]
    finally:
        f.close()
    evidence("actual-route-fixture-gap", {"fixture_flash_route": fixture_doc["flash"],
                                          "fixture_heads": fixture_heads, "actual_heads": actual_heads})
    assert len(fixture_doc["flash"]["rotate"]) == 1 and len(set(fixture_heads)) == 1
    assert len(set(actual_heads[:3])) == 3 and actual_heads[3] == actual_heads[0]


def test_identity_reads_are_passive_order_independent_and_bind_real_effort(factory):
    """Repeated evaluator identity and builtin/shadow equivalence reads, in both
    orders, consume no scheduling selection and agree on the host's frozen
    concrete endpoint with its real route-declared effort."""
    f = factory()
    mapping = f.world.install_target("builtin")
    ev, shadow = f.evaluator, mapping["roles"]["investigator"]
    out = {"counters_before": counters()}
    try:
        out["forward"] = [profile(ev, r) for r in ("investigator", shadow) * 2]
        out["reverse"] = [profile(ev, r) for r in (shadow, "investigator") * 2]
        out["equivalence"] = [f.round._builtin_equivalence() for _ in range(2)]
        compiled = {"reference": {"role": "investigator"}, "candidate": {"role": shadow}}
        out["served"] = {}
        for label in ("ollama-primary", "ark-primary", "anthropic-primary"):
            ev.host.endpoint_label = label
            out["served"][label] = ev.served_runtime(compiled, "sha256:" + "1" * 64).model_dump(mode="json")
        ev.host.endpoint_label = "ollama-primary"
        out["served_again"] = ev.served_runtime(compiled, "sha256:" + "1" * 64).model_dump(mode="json")
        out["counters_after"] = counters()
    except BaseException:                                           # noqa: BLE001
        out["error"] = traceback.format_exc()
        raise
    finally:
        evidence("actual-route-passive-identity", out)
    reads = out["forward"] + out["reverse"]
    assert out["counters_after"] == out["counters_before"]          # no selection consumed
    assert all(without_role(p) == without_role(reads[0]) for p in reads)
    assert reads[0]["active"] == OLLAMA and reads[0]["internal"] == "flash" and reads[0]["effort"] == "low"
    eq = out["equivalence"]
    assert eq[0]["equivalent"] and eq[1]["equivalent"], eq[0].get("differences")
    assert eq[0]["effective_profiles"] == eq[1]["effective_profiles"]
    pair = eq[0]["effective_profiles"]["investigator"]
    assert pair["builtin"] == pair["shadow"] and pair["builtin"]["active_model"] == OLLAMA
    served = out["served"]
    assert {lab: (s["endpoint"]["provider"] + "/" + s["endpoint"]["model"], s["effort"])
            for lab, s in served.items()} == {
        "ollama-primary": (OLLAMA, "low"), "ark-primary": ("ark/glm-5.3-flash", "low"),
        "anthropic-primary": ("anthropic/claude-haiku-5-5", "high")}
    assert out["served_again"] == served["ollama-primary"]
    assert all(s["requested_model"] == "flash" for s in served.values())
    import hashlib
    routes = (f.root / "runtime" / "model_routes.json").read_bytes()
    assert served["ollama-primary"]["routes"] == "sha256:" + hashlib.sha256(routes).hexdigest()


def test_useful_builtin_round_on_actual_route_sends_only_the_frozen_endpoint(factory):
    """The literal builtin round (reference and candidate, train and holdout)
    under the actual route: every cell scored through accepted admission, every
    physical send carries the frozen endpoint and its real effort."""
    vector = next(v for v in SCENARIOS if v["name"] == "builtin_source_patch_only")
    before, result, error = counters(), None, None
    try:
        result = run_scenario(factory, vector)
    except BaseException:                                           # noqa: BLE001
        error = traceback.format_exc()
        raise
    finally:
        f = factory.made[0] if factory.made else None
        native = f.native_evidence() if f else None
        sends = f.provider.received() if f else []
        evidence("actual-route-useful-builtin-round", {
            "result": result, "error": error, "native": native, "counters_before": before,
            "counters_after": counters(),
            "send_identities": sorted({json.dumps({"url": s["url"], **{k: s["body"].get(k) for k in (
                "model", "reasoning_effort", "thinking", "temperature", "max_tokens")}}, sort_keys=True)
                for s in sends})})
    rt = native["request"]["runtime"]
    assert (rt["endpoint"]["provider"], rt["endpoint"]["model"], rt["endpoint"]["endpoint"]) == (
        "ollamacloud", "deepseek-v4.1-flash", "ollama-primary")
    assert rt["requested_model"] == "flash" and rt["effort"] == "low"
    assert native["builtin_equivalence"]["equivalent"]
    assert counters() == before                                     # cells consumed no selection
    assert sends and native["trace_physical_sends"] == native["provider_sends"] == len(sends)
    assert native["view"]["known"]["requests"] == len(sends)
    assert all(s["url"] == "https://ollama.com/v1/chat/completions" for s in sends)
    assert all(s["body"]["model"] == "deepseek-v4.1-flash" and s["body"].get("reasoning_effort") == "low"
               and s["body"].get("thinking") == {"type": "enabled"} for s in sends)
    points = native["checker_points"]
    assert points and all(p["disposition"] == "scored" for p in points)
    assert {p["arm"] for p in points} == {"reference", "candidate"}
    assert {p["evaluation_id"].split(".", 1)[1].split("-", 1)[0] for p in points} == {"train", "holdout"}
    assert native["trace_denials"] == []


def test_ordinary_rotation_is_unchanged_outside_the_experiment(factory):
    """Ordinary gateways (no evaluator scope, no frozen endpoint) still take one
    scheduling selection each; interleaved evaluator reads take none."""
    from core.agents import AgentFactory
    f = factory()
    f.world.install_target("builtin")
    ordinary = AgentFactory(registry=f.world.sf.agent_registry)
    ordinary.gateway_paths = (str(f.root / "runtime" / "llm_providers.json"),
                              str(f.root / "runtime" / "model_routes.json"))
    pool = ACTUAL_FLASH["rotate"]
    heads, steps = [], []
    for _ in range(4):
        g = ordinary._build_gateway("investigator")
        heads.append((g.active_model, g._binding_effort()))
        steps.append(counters()["flash"])
        profile(f.evaluator, "investigator")                        # passive, interleaved
    evidence("actual-route-ordinary-rotation", {"heads": heads, "counter_after_each_build": steps})
    # Each ordinary build takes exactly the NEXT selection: no interleaved read took one.
    assert all(b == (a + 1) % 3 for a, b in zip(steps, steps[1:])), steps
    assert all(pool.index(b[0]) == (pool.index(a[0]) + 1) % 3 for a, b in zip(heads, heads[1:])), heads
    assert {h for h, _ in heads} == set(pool)
    assert dict(heads) == {c: ACTUAL_FLASH["effort"][c] for c in pool}


def _frozen(f):
    request = f.prepare("builtin")
    f.actor = "coordinator"
    f.invoke("freeze", request.model_dump(mode="json"))
    return request


def _train_reference(f):
    f.actor = ROLES["train_reference"]
    view = f.invoke("train_reference", {})
    return view, f.native_evidence()


def _edit_routes(f, mutate):
    from core.model_routes import reset_cache
    path = f.root / "runtime" / "model_routes.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc["flash"])
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    reset_cache()


def test_changed_route_effort_provider_and_fallback_are_refused_before_send(factory):
    """After freeze: a changed endpoint effort, a route that no longer offers
    the frozen endpoint, or a changed provider endpoint refuses before launch;
    a fallback member is never a frozen binding; an in-cell failover is refused
    before its own send and the known accounting stays truthful."""
    from contracts.rsi.pipeline_versioning import contract as vc
    out = {}
    cases = (
        ("effort_changed", lambda f: _edit_routes(f, lambda r: r["effort"].__setitem__(OLLAMA, "high")),
         "RUNTIME_DRIFT"),
        ("route_changed", lambda f: _edit_routes(f, lambda r: r.__setitem__(
            "rotate", ["ark/glm-5.3-flash", "anthropic/claude-haiku-5-5"])), "RUNTIME_DRIFT"),
        # The frozen endpoint and its effort are untouched: only the whole-route
        # identity (another endpoint's effort, the fallback tail) changed.
        ("route_identity_changed", lambda f: _edit_routes(f, lambda r: (
            r["effort"].__setitem__("ark/glm-5.3-flash", "high"),
            r.__setitem__("fallback", ["deepseek/deepseek-chat"]))), "RUNTIME_DRIFT"),
        ("provider_changed", lambda f: (f.root / "runtime" / "llm_providers.json").write_text(json.dumps(
            {**ACTUAL_PROVIDERS, "ollamacloud": {**ACTUAL_PROVIDERS["ollamacloud"],
                                                 "base_url": "https://ollama.invalid/v1"}})),
         "PROVIDER_CHANGED"))
    try:
        for name, change, reason in cases:
            f = factory()
            _frozen(f)
            change(f)
            view, native = _train_reference(f)
            details = [json.loads(p.read_text())["detail"] for p in sorted(
                (f.root / "private" / "evaluator" / "refusals").glob("*.json"))]
            out[name] = {"stop": view.stop_reason, "refusals": native["refusals"], "details": details,
                         "provider_sends": native["provider_sends"],
                         "launches": native["evaluator_cell_launches"]}
            assert native["provider_sends"] == 0 and native["evaluator_cell_launches"] == 0
            assert native["refusals"][0]["refusal"]["reason"] == reason, native["refusals"]
            assert view.stop_reason == "invalid"
            if name == "route_identity_changed":
                assert details == ["served runtime differs in ['routes']"], details
        factory.endpoint = "deepseek-fallback"                    # fallback as the binding
        f = factory()
        with pytest.raises(vc.UnsupportedContent) as refused:
            f.prepare("builtin")
        out["fallback_binding"] = {"refused": str(refused.value)[:800],
                                   "provider_sends": len(f.provider.received())}
        assert "fallback" in str(refused.value) and f.provider.received() == []
        factory.endpoint = "ollama-primary"                       # in-cell failover
        f = factory()
        _frozen(f)
        f.provider.first_cell = ("train-01", "reference")
        f.provider.fault = "fallback"                             # frozen endpoint answers 503
        view, native = _train_reference(f)
        sends = f.provider.received()
        out["in_cell_failover"] = {"stop": view.stop_reason, "denials": native["trace_denials"],
                                   "sends": [s["url"] for s in sends],
                                   "known": view.known.model_dump(),
                                   "retained_unknown": view.retained_unknown.model_dump(),
                                   "evaluations": native["evaluations"]}
        assert [s["url"] for s in sends] == ["https://ollama.com/v1/chat/completions"]
        drift = [d for d in native["trace_denials"] if d.get("reason") == "RUNTIME_DRIFT"]
        assert drift and drift[0]["observed_endpoint"] == "ark-primary"
        assert view.known.requests == 1 and view.retained_unknown.requests == 0
        assert "RUNTIME_DRIFT" in native["evaluations"][0]["reasons"]
        assert view.stop_reason == "invalid"
    finally:
        evidence("actual-route-refusals", out)
