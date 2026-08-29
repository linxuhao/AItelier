"""Internal model names, and the sticky failover they enable.

The tests that matter here are the NEGATIVE ones: that a request-shaped error
never walks the candidate list (it would burn the quota we are trying to
conserve on a call that cannot succeed), and that a re-binding does not leave
the previous provider's credentials on the kwargs.
"""

import json

import litellm
import pytest

from core import model_routes
from core.ai_router import (AIGateway, endpoint_cooldowns,
                            reset_endpoint_cooldowns)


PROVIDERS = {
    "alpha": {"base_url": "https://alpha.example/v1", "api_key_env": "ALPHA_KEY"},
    "beta": {"base_url": "https://beta.example/v1", "api_key_env": "BETA_KEY"},
    "nokey": {"base_url": "https://nokey.example/v1"},
}


@pytest.fixture(autouse=True)
def _plain_transport(monkeypatch):
    """These tests pin ROUTING (binding, failover, cooldowns), not the
    transport: run them over the plain non-streaming call so the mocked
    litellm.completion can return plain _Resp objects. The streaming
    transport has its own suite (test_llm_stream_transport.py)."""
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    providers = tmp_path / "llm_providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps({
        "_comment": "ignored",
        "model_a": ["alpha/m-1", "beta/m-1"],
        "solo": ["alpha/m-1"],
    }), encoding="utf-8")
    monkeypatch.setenv("ALPHA_KEY", "alpha-secret")
    monkeypatch.setenv("BETA_KEY", "beta-secret")
    model_routes.reset_cache()
    # Endpoint cooldowns are process-wide by design; a quota test would
    # otherwise leak a parked endpoint into every test after it.
    reset_endpoint_cooldowns()
    yield str(providers), str(routes)
    model_routes.reset_cache()
    reset_endpoint_cooldowns()


def gw(wiring, model, **kw):
    providers, routes = wiring
    return AIGateway(model, config_path=providers, routes_path=routes, **kw)


# ── the table ────────────────────────────────────────────────────────

def test_unknown_name_passes_through(wiring):
    g = gw(wiring, "alpha/m-1")
    assert g._candidates == ["alpha/m-1"]
    assert g.active_model == "alpha/m-1"


def test_internal_name_binds_first_candidate(wiring):
    g = gw(wiring, "model_a")
    assert g._candidates == ["alpha/m-1", "beta/m-1"]
    assert g.active_model == "alpha/m-1"
    assert g.api_base == "https://alpha.example/v1"
    assert g.api_key == "alpha-secret"


def test_route_to_a_route_is_rejected(tmp_path):
    p = tmp_path / "model_routes.json"
    p.write_text(json.dumps({"a": ["b/m"], "b": ["alpha/m-1"]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="names another route"):
        model_routes.ModelRoutes(p)


def test_bare_candidate_without_provider_is_rejected(tmp_path):
    p = tmp_path / "model_routes.json"
    p.write_text(json.dumps({"a": ["just-a-model"]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="provider/model"):
        model_routes.ModelRoutes(p)


def test_missing_table_leaves_concrete_names_working(tmp_path):
    """No table = routing off, not a broken install."""
    t = model_routes.ModelRoutes(tmp_path / "nope.json")
    assert t.resolve("alpha/m-1") == ["alpha/m-1"]
    # …and a bare name is still an error, since it names nothing runnable.
    with pytest.raises(RuntimeError, match="Known routes"):
        t.resolve("flash")


def test_malformed_table_raises_rather_than_silently_disabling(tmp_path):
    p = tmp_path / "model_routes.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be parsed"):
        model_routes.ModelRoutes(p)


# ── failover ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": "ok", "tool_calls": None,
                                      "reasoning_content": ""})(),
            "finish_reason": "stop"})()]
        self.usage = None


def test_endpoint_error_advances_to_next_candidate(wiring, monkeypatch):
    g = gw(wiring, "model_a")
    seen = []

    def fake(**kwargs):
        seen.append((kwargs["model"], kwargs.get("api_key")))
        if len(seen) == 1:
            raise litellm.exceptions.RateLimitError(
                "quota exhausted", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])

    assert len(seen) == 2
    assert g.active_model == "beta/m-1"
    # The second attempt must carry beta's credentials, not alpha's.
    assert seen[1][1] == "beta-secret"
    assert seen[0][1] == "alpha-secret"


def test_request_error_never_walks_the_list(wiring, monkeypatch):
    g = gw(wiring, "model_a")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["model"])
        raise litellm.exceptions.ContextWindowExceededError(
            "too long", model="m-1", llm_provider="alpha")

    monkeypatch.setattr(litellm, "completion", fake)
    with pytest.raises(Exception):
        g.generate_native([{"role": "user", "content": "hi"}])

    assert len(calls) == 1, "a doomed request must not be replayed at every provider"
    assert g.active_model == "alpha/m-1"


def test_context_overflow_walks_to_a_bigger_window(wiring, monkeypatch, tmp_path):
    """A SMALL endpoint's overflow is an endpoint property, not a request one.

    The local llama.cpp endpoint serves 131k where the cloud candidates behind
    it take far more, and 1.35% of measured flash calls are longer than that.
    Refusing to walk would hard-fail those steps on the one endpoint that
    cannot serve them, with usable candidates still queued.
    """
    import json as _json
    cfg = _json.loads(pathlib.Path(wiring["providers"]).read_text()) \
        if isinstance(wiring, dict) and "providers" in wiring else None
    g = gw(wiring, "model_a")
    # alpha declares a small window; beta declares none (= unbounded).
    provs = _json.loads(open(g._config_path).read())
    provs["alpha"]["max_input_tokens"] = 1000
    open(g._config_path, "w").write(_json.dumps(provs))

    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        if len(seen) == 1:
            raise litellm.exceptions.ContextWindowExceededError(
                "too long", model="m-1", llm_provider="alpha")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])
    assert len(seen) == 2, "overflow on a small endpoint must reach a bigger one"
    assert g.active_model == "beta/m-1"


def test_binding_is_sticky_across_turns(wiring, monkeypatch):
    """Once failed over, later turns of the SAME step stay on the new endpoint.

    This is what protects the provider prefix cache; a gateway that re-tried
    the preferred endpoint each turn would alternate and halve the hit rate.
    """
    g = gw(wiring, "model_a")
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        if len(seen) == 1:
            raise litellm.exceptions.ServiceUnavailableError(
                "down", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    for _ in range(3):
        g.generate_native([{"role": "user", "content": "hi"}])

    assert seen[1:] == [seen[1]] * 3, "must not drift back to the first candidate"
    assert g.active_model == "beta/m-1"


def test_exhausted_candidates_reraise_the_provider_error(wiring, monkeypatch):
    g = gw(wiring, "solo")

    def fake(**kwargs):
        raise litellm.exceptions.AuthenticationError(
            "bad key", llm_provider="alpha", model="m-1")

    monkeypatch.setattr(litellm, "completion", fake)
    with pytest.raises(Exception, match="bad key"):
        g.generate_native([{"role": "user", "content": "hi"}])
    assert g._candidate_ix == 0


def test_quota_exhaustion_fails_over_instead_of_parking_the_scheduler(
        wiring, monkeypatch):
    """A spent plan on ONE endpoint must not stop the pipeline.

    core/scheduler.py parks every tick when `is_quota_exhausted(e)` sees a
    RateLimitError escape a step — correct when there is nowhere else to go,
    and far too blunt when a second endpoint serves the same model. Failover
    swallows the error, so the scheduler only ever sees it once the whole
    candidate list is spent.
    """
    from core.ai_router import is_quota_exhausted

    quota_err = litellm.exceptions.RateLimitError(
        "You have exceeded the 5-hour usage quota. It will reset at "
        "2026-08-26 09:18:28 +0800 CST.", llm_provider="alpha", model="m-1")
    assert is_quota_exhausted(quota_err), "fixture must be a real quota error"

    g = gw(wiring, "model_a")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise quota_err
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])   # must not raise
    assert g.active_model == "beta/m-1"


def test_quota_exhaustion_still_escapes_when_no_candidate_is_left(
        wiring, monkeypatch):
    """…and the park must still happen when it IS the last endpoint."""
    from core.ai_router import is_quota_exhausted

    g = gw(wiring, "solo")

    def fake(**kwargs):
        raise litellm.exceptions.RateLimitError(
            "You have exceeded the 5-hour usage quota. It will reset at "
            "2026-08-26 09:18:28 +0800 CST.", llm_provider="alpha", model="m-1")

    monkeypatch.setattr(litellm, "completion", fake)
    with pytest.raises(Exception) as ei:
        g.generate_native([{"role": "user", "content": "hi"}])
    assert is_quota_exhausted(ei.value), (
        "the scheduler recognises the hold by the escaping error; failover must "
        "not mask or rewrap it out of recognition")


def test_failover_is_recorded_for_attribution(wiring, monkeypatch):
    g = gw(wiring, "model_a")
    calls = []

    def fake(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise litellm.exceptions.InternalServerError(
                "boom", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g._failovers and g._failovers[0][0] == "alpha/m-1"
    assert "InternalServerError" in g._failovers[0][1]


def test_typo_in_an_internal_name_is_rejected_loudly(wiring):
    """A bare unknown name must not pass through to litellm.

    Before agent_configs carried internal names every model string was
    provider-qualified, so a passthrough was harmless. Now a typo would reach
    litellm as a bare model and return "LLM Provider NOT provided" — an error
    that names neither the offending role nor model_routes.json.
    """
    with pytest.raises(RuntimeError, match="Known routes"):
        gw(wiring, "modle_a")


def test_every_shipped_agent_config_model_resolves():
    """The repo's own role files must only name routes that exist."""
    import pathlib
    import yaml

    root = pathlib.Path(__file__).resolve().parents[2]
    model_routes.reset_cache()
    table = model_routes.ModelRoutes(model_routes.config_or_example("model_routes.json"))
    sentinels = {"host", "default", ""}
    checked = 0
    for f in sorted((root / "agent_configs").glob("*.yaml")):
        cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for role, rc in cfg.items():
            if not isinstance(rc, dict) or "model" not in rc:
                continue
            name = rc["model"]
            if name in sentinels:
                continue
            checked += 1
            assert table.resolve(name), f"{f.name}:{role} -> {name}"
    assert checked > 30, "expected the real role files, not an empty scan"
    model_routes.reset_cache()


def test_host_sentinel_default_is_resolvable():
    import pathlib

    from core.agents import HOST_AGENT_MODEL

    root = pathlib.Path(__file__).resolve().parents[2]
    model_routes.reset_cache()
    table = model_routes.ModelRoutes(model_routes.config_or_example("model_routes.json"))
    assert table.resolve(HOST_AGENT_MODEL)
    model_routes.reset_cache()


def test_failover_keys_are_recommended_not_required():
    """A second endpoint's key must never gate startup.

    The CLI creates an empty secret file and prints "write your key here" for
    every name `required_llm_keys()` returns. Counting failover keys there would
    tell an Ark-only user to go make a DeepSeek key for an install that runs
    fine without one — the same false alarm, pointed the other way, that
    `required_llm_keys` was written to stop.
    """
    from core.external_deps import failover_llm_keys, required_llm_keys

    req, spare = set(required_llm_keys()), set(failover_llm_keys())
    assert req, "the shipped configs must need at least one key"
    assert not (req & spare), "a key cannot be both required and merely advisable"
    # The shipped table has a second candidate for flash/pro, so there IS
    # something to recommend — if this ever empties, failover became a no-op.
    assert spare, "no failover endpoint configured; model_routes.json is single-homed"


# ── spent-window cooldown ────────────────────────────────────────────

QUOTA_MSG = ("You have exceeded the 5-hour usage quota. It will reset at "
             "2099-01-01 00:00:00 +0000.")


def _quota(provider="alpha"):
    return litellm.exceptions.RateLimitError(
        QUOTA_MSG, llm_provider=provider, model="m-1")


def test_a_spent_window_takes_that_endpoint_out_of_rotation(wiring, monkeypatch):
    """The NEXT gateway must not re-discover the same exhausted plan.

    Without this, a spent 5-hour window costs one doomed call per step for
    hours — the failover works every time and every time it pays for the
    lesson again.
    """
    g = gw(wiring, "model_a")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs.get("api_key"))
        if kwargs.get("api_key") == "alpha-secret":
            raise _quota()
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g.active_model == "beta/m-1"
    assert "alpha/m-1" in endpoint_cooldowns()

    # A brand-new gateway — a later step — skips the parked endpoint outright.
    g2 = gw(wiring, "model_a")
    assert g2.active_model == "beta/m-1"
    assert g2._failovers == [], "it should not have had to fail over at all"


def test_burst_throttling_does_not_park_the_endpoint(wiring, monkeypatch):
    """A plain 429 clears in seconds; retiring the endpoint for it would give
    up the preferred provider over a momentary spike."""
    g = gw(wiring, "model_a")

    # Both providers are OpenAI-compatible, so `_bind` rewrites either to
    # `openai/m-1` — the api_key is what distinguishes them on the wire.
    def fake(**kwargs):
        if kwargs.get("api_key") == "alpha-secret":
            raise litellm.exceptions.RateLimitError(
                "Too many requests, slow down", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g.active_model == "beta/m-1"
    assert endpoint_cooldowns() == {}, "a burst 429 must not park anything"
    assert gw(wiring, "model_a").active_model == "alpha/m-1", (
        "the next step must still prefer the preferred endpoint")


def test_all_candidates_parked_still_attempts_rather_than_refusing(
        wiring, monkeypatch):
    """A mis-parsed reset timestamp must not be able to brick the system."""
    from core.ai_router import _note_endpoint_spent

    _note_endpoint_spent("alpha/m-1", _quota())
    _note_endpoint_spent("beta/m-1", _quota("beta"))
    assert len(endpoint_cooldowns()) == 2

    monkeypatch.setattr(litellm, "completion", lambda **kw: _Resp())
    g = gw(wiring, "model_a")
    assert g.active_model == "alpha/m-1", "falls back to the preferred candidate"
    g.generate_native([{"role": "user", "content": "hi"}])  # must not raise


def test_cooldown_is_capped(wiring):
    """One hostile or mis-parsed report cannot park an endpoint indefinitely."""
    import time

    from core.ai_router import _COOLDOWN_MAX_S, _note_endpoint_spent

    until = _note_endpoint_spent("alpha/m-1", _quota())   # says year 2099
    assert until <= time.time() + _COOLDOWN_MAX_S + 1


def test_a_reset_already_in_the_past_parks_nothing(wiring):
    from core.ai_router import _note_endpoint_spent

    err = litellm.exceptions.RateLimitError(
        "quota exceeded. It will reset at 2020-01-01 00:00:00 +0000.",
        llm_provider="alpha", model="m-1")
    assert _note_endpoint_spent("alpha/m-1", err) == 0.0
    assert endpoint_cooldowns() == {}


# ── attribution ──────────────────────────────────────────────────────

class _UsageResp(_Resp):
    def __init__(self):
        super().__init__()
        self.usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 10,
                                    "prompt_cache_hit_tokens": 80,
                                    "prompt_cache_miss_tokens": 20,
                                    "completion_tokens_details": None})()


def test_usage_records_which_endpoint_served_the_turn(wiring, monkeypatch):
    """`last_usage` is traced per turn; after a failover an unstamped record is
    indistinguishable from one the preferred endpoint served, so a run's spend
    cannot be attributed to a plan."""
    g = gw(wiring, "model_a")
    calls = []

    def fake(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise litellm.exceptions.ServiceUnavailableError(
                "down", llm_provider="alpha", model="m-1")
        return _UsageResp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])

    u = g.last_usage
    assert u["served_by"] == "beta/m-1"
    assert u["model_route"] == "model_a"
    assert u["failed_over_from"] == ["alpha/m-1"]
    assert u["prompt_tokens"] == 100, "the existing token fields must survive"


def test_usage_of_a_concrete_model_carries_no_route_noise(wiring, monkeypatch):
    g = gw(wiring, "alpha/m-1")
    monkeypatch.setattr(litellm, "completion", lambda **kw: _UsageResp())
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g.last_usage["served_by"] == "alpha/m-1"
    assert "model_route" not in g.last_usage
    assert "failed_over_from" not in g.last_usage


def test_every_llm_entry_point_understands_internal_names():
    """Both resolvers, not just the one everybody remembers.

    `AIGateway` is the obvious LLM entry point, but `core/meta_agent.py` has a
    SECOND one: the butler and the condenser stream, so they build their own
    litellm kwargs through `_resolve_provider` and never touch the gateway.
    When agent_configs were swept to internal names that resolver still only
    understood a `provider/` prefix, so it returned ("pro", None, None) and
    every butler turn went out as `litellm.acompletion(model="pro")` with no
    base URL and no key. The condenser failed identically but silently, because
    `_summarize_chunk` swallows its exception — a long coding session would
    just stop compacting.

    Shipped to main and caught in review, not by this suite. So: every `model:`
    the repo declares must resolve through EVERY entry point.
    """
    import pathlib

    import yaml

    from core.meta_agent import _resolve_provider

    root = pathlib.Path(__file__).resolve().parents[2]
    model_routes.reset_cache()
    sentinels = {"host", "default", ""}
    checked = 0
    for f in sorted((root / "agent_configs").glob("*.yaml")):
        for role, rc in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            if not isinstance(rc, dict) or rc.get("model") in sentinels:
                continue
            name = rc.get("model")
            if name is None:
                continue
            checked += 1
            litellm_model, api_base, api_key = _resolve_provider(
                name, config_path=model_routes.config_or_example("llm_providers.json"))
            assert "/" in litellm_model, (
                f"{f.name}:{role} model={name!r} -> {litellm_model!r}: the "
                f"butler would send this to litellm unqualified")
            assert api_base, (
                f"{f.name}:{role} model={name!r} resolved with no api_base — "
                f"the butler would call it with no endpoint")
    assert checked > 30, "expected the real role files, not an empty scan"
    model_routes.reset_cache()


def test_the_route_table_is_found_from_any_working_directory(tmp_path, monkeypatch):
    """CWD-relative was survivable while models were concrete `provider/model`
    strings — a missing table degraded to a passthrough. With internal names a
    miss raises and every agent dies, so the path must not depend on where the
    process was launched."""
    from core.ai_router import AIGateway

    monkeypatch.chdir(tmp_path)          # nowhere near the repo root
    model_routes.reset_cache()
    try:
        g = AIGateway("flash")
        assert g._candidates and "/" in g._candidates[0]
    finally:
        model_routes.reset_cache()


def test_an_auth_failure_names_the_route_that_chose_the_endpoint(wiring):
    """"provider 'localqwen'" leaves the operator hunting: nothing in
    agent_configs says `localqwen` — it says `vision`, and the candidate list
    is what put them there. The error has to name the line they would edit."""
    g = gw(wiring, "model_a")
    g.missing_key_env = "ALPHA_KEY"
    err = g._explain_auth(litellm.exceptions.AuthenticationError(
        "bad key", llm_provider="alpha", model="m-1"))
    msg = str(err)
    assert "ALPHA_KEY" in msg                       # the secret to create
    assert "model_a" in msg                         # the internal name
    assert "alpha/m-1" in msg and "beta/m-1" in msg  # the candidate list
    assert "1 of 2" in msg                          # where in the list it is


def test_a_concrete_model_gets_no_routing_noise(wiring):
    """Nothing was routed, so there is no route to name."""
    g = gw(wiring, "alpha/m-1")
    g.missing_key_env = "ALPHA_KEY"
    msg = str(g._explain_auth(litellm.exceptions.AuthenticationError(
        "bad key", llm_provider="alpha", model="m-1")))
    assert "model_routes.json" not in msg


def test_a_write_that_creates_the_real_table_is_visible_without_a_restart(
        tmp_path, monkeypatch):
    """The route path must be resolved per call, never captured at import.

    It was a module constant, so on a checkout holding only the example the
    first API write created the real file, `reset_cache()` dropped the cache,
    and `get_routes(None)` still keyed on the EXAMPLE path — re-read the
    example, and the new model did not exist. `add_model` returned success and
    `AIGateway("brandnew")` raised "is neither a 'provider/model' nor a route"
    until the container was recreated. A write that reports success and changes
    nothing is the worst of the three outcomes.
    """
    import json

    from core import model_registry as reg

    (tmp_path / "model_routes.example.json").write_text(
        json.dumps({"seed": ["alpha/m-1"]}), encoding="utf-8")
    (tmp_path / "llm_providers.json").write_text(
        json.dumps({"alpha": {"base_url": "https://a.test/v1"}}), encoding="utf-8")
    monkeypatch.setattr(model_routes, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(reg, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(reg, "config_or_example",
                        lambda n: model_routes.config_or_example(n))
    model_routes.reset_cache()
    try:
        assert model_routes.default_routes_file().endswith("example.json")
        reg.add_model("brandnew", ["alpha/m-2"])
        assert (tmp_path / "model_routes.json").is_file()
        assert "brandnew" in model_routes.get_routes().names(), (
            "the write landed on disk but the running process still reads the "
            "example — the API said success and nothing changed")
    finally:
        model_routes.reset_cache()


def test_a_burst_tolerance_that_never_resets_is_a_lifetime_counter(
        wiring, monkeypatch):
    """Two unrelated blips must each get their in-place retry.

    `_burst_hits` was set once in __init__ and only incremented, so the second
    momentary 429 in a 24-turn step failed over with zero retries — abandoning
    the prefix cache, and on glm/smart/vision silently switching model, for a
    throttle that clears in seconds.
    """
    g = gw(wiring, "model_a")
    seen = []

    def fake(**kwargs):
        seen.append(kwargs.get("api_key"))
        # Blip on the 1st and 4th calls; everything else answers.
        if len(seen) in (1, 4):
            raise litellm.exceptions.RateLimitError(
                "Too many requests, slow down", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    for _ in range(4):
        g.generate_native([{"role": "user", "content": "hi"}])

    assert g.active_model == "alpha/m-1", (
        "both blips were separated by a success, so neither should have cost "
        "the preferred endpoint")
    assert all(k == "alpha-secret" for k in seen)


def test_consecutive_bursts_still_fail_over(wiring, monkeypatch):
    """…while a throttle that actually persists must still move on."""
    g = gw(wiring, "model_a")
    n = {"i": 0}

    def fake(**kwargs):
        n["i"] += 1
        if kwargs.get("api_key") == "alpha-secret":
            raise litellm.exceptions.RateLimitError(
                "Too many requests, slow down", llm_provider="alpha", model="m-1")
        return _Resp()

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "hi"}])
    assert g.active_model == "beta/m-1"


def test_an_exhausted_model_names_its_own_endpoints_on_the_error(
        wiring, monkeypatch):
    """The scheduler cannot know which endpoints have to reopen from an
    exception alone, and taking the minimum over every parked endpoint in the
    process let an unrelated model's short window cut a long hold."""
    g = gw(wiring, "solo")

    def fake(**kwargs):
        raise litellm.exceptions.RateLimitError(
            "quota exceeded. It will reset at 2099-01-01 00:00:00 +0000.",
            llm_provider="alpha", model="m-1")

    monkeypatch.setattr(litellm, "completion", fake)
    with pytest.raises(Exception) as ei:
        g.generate_native([{"role": "user", "content": "hi"}])
    assert getattr(ei.value, "_aitelier_candidates", None) == ["alpha/m-1"]


# ── per-endpoint reasoning effort ────────────────────────────────────────
#
# One internal model now spans endpoints whose effort vocabularies do not
# overlap: DeepSeek takes low/high/max, Qwen3.8's chat template takes
# low/medium/xhigh and RAISES on anything else — `max` came back HTTP 500
# ("Unexpected reasoning effort max", measured on localqwen/qwen3 2026-08-29).
# So the string cannot live only on the role; the route states what each
# endpoint accepts.


def _effort_wiring(tmp_path, monkeypatch, effort_map):
    providers = tmp_path / "llm_providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    routes = tmp_path / "model_routes.json"
    # rotate pool of ONE so the binding is deterministic: the rotation counter
    # is process-wide, so a two-endpoint pool makes each test's binding depend
    # on how many tests ran before it.
    routes.write_text(json.dumps({
        "model_a": {"rotate": ["alpha/m-1"], "fallback": ["beta/m-1"],
                    "effort": effort_map},
    }), encoding="utf-8")
    monkeypatch.setenv("ALPHA_KEY", "a")
    monkeypatch.setenv("BETA_KEY", "b")
    model_routes.reset_cache()
    return str(providers), str(routes)


def test_route_effort_overrides_the_role(tmp_path, monkeypatch):
    w = _effort_wiring(tmp_path, monkeypatch, {"alpha/m-1": "xhigh"})
    g = gw(w, "model_a", enable_thinking=True, thinking_effort="max")
    kwargs = g._apply_binding({})
    assert kwargs["extra_body"]["reasoning_effort"] == "xhigh", (
        "the role names one string for a model that spans vocabularies; the "
        "endpoint's own is the only one it is guaranteed to accept")


def test_role_effort_stands_where_the_route_is_silent(tmp_path, monkeypatch):
    """Absent key = the behaviour every route had before it existed. Routes
    whose endpoints share a vocabulary must not have to declare anything."""
    w = _effort_wiring(tmp_path, monkeypatch, {"beta/m-1": "medium"})
    g = gw(w, "model_a", enable_thinking=True, thinking_effort="max")
    assert g._apply_binding({})["extra_body"]["reasoning_effort"] == "max"


def test_effort_follows_a_failover(tmp_path, monkeypatch):
    """Resolved at binding time, not construction: a failover rebinds mid-step
    and the new endpoint may want a different string for the same intent."""
    w = _effort_wiring(tmp_path, monkeypatch,
                       {"alpha/m-1": "xhigh", "beta/m-1": "max"})
    g = gw(w, "model_a", enable_thinking=True, thinking_effort="low")
    assert g._apply_binding({})["extra_body"]["reasoning_effort"] == "xhigh"
    assert g._failover(litellm.exceptions.ServiceUnavailableError(
        "down", model="m-1", llm_provider="alpha"))
    assert g._apply_binding({})["extra_body"]["reasoning_effort"] == "max"


def test_an_unknown_route_key_is_still_rejected(tmp_path):
    """Adding `effort` must not turn the dict form into a place typos hide."""
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps(
        {"model_a": {"rotate": ["alpha/m-1"], "effrot": {}}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unknown key"):
        model_routes.ModelRoutes(str(routes))


def test_effort_must_map_endpoint_to_string(tmp_path):
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps(
        {"model_a": {"rotate": ["alpha/m-1"], "effort": {"alpha/m-1": 3}}}),
        encoding="utf-8")
    with pytest.raises(RuntimeError, match="'effort' must map"):
        model_routes.ModelRoutes(str(routes))


# ── context headroom ─────────────────────────────────────────────────────
#
# The wall a small endpoint puts up is not the overflow, it is the asymptote.
# llama.cpp counts only the PROMPT against its context, so a prompt that fits
# is accepted and the generation is silently clamped to whatever room is left.
# Measured on a t_impl step: turn 17 came back at prompt 130,940 of a 131,072
# window and produced 131 tokens, the next attempt got 51, no error ever fired,
# and the step was re-queued after 846s of discarded work. 22.1% of measured
# t_impl steps reach a peak prompt leaving less than their 32,768-token output
# budget; only 9.1% actually exceed the window, so waiting for
# ContextWindowExceededError misses the larger half.


def _resp_with_prompt(prompt_tokens):
    r = _Resp()
    r.usage = type("U", (), {
        "prompt_tokens": prompt_tokens, "completion_tokens": 4,
        "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None,
        "prompt_tokens_details": None, "completion_tokens_details": None})()
    return r


def _window_wiring(tmp_path, monkeypatch, alpha_window):
    providers = tmp_path / "llm_providers.json"
    provs = json.loads(json.dumps(PROVIDERS))
    provs["alpha"]["max_input_tokens"] = alpha_window     # beta: undeclared
    providers.write_text(json.dumps(provs), encoding="utf-8")
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps(
        {"model_a": {"rotate": ["alpha/m-1"], "fallback": ["beta/m-1"]}}),
        encoding="utf-8")
    monkeypatch.setenv("ALPHA_KEY", "a")
    monkeypatch.setenv("BETA_KEY", "b")
    model_routes.reset_cache()
    return str(providers), str(routes)


def test_a_squeezed_turn_rebinds_before_the_next_one(tmp_path, monkeypatch):
    w = _window_wiring(tmp_path, monkeypatch, alpha_window=1000)
    g = gw(w, "model_a", max_output_tokens=200)
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        return _resp_with_prompt(900)      # 100 left, budget is 200

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "x"}])

    assert seen == ["openai/m-1"], "the call that succeeded must not be retried"
    assert g.active_model == "beta/m-1", (
        "the next turn's prompt is this one plus the answer plus a tool "
        "result, so 'it fit this time' says nothing about the next")


def test_ample_headroom_keeps_the_binding(tmp_path, monkeypatch):
    """Rebinding costs the per-provider prefix cache; only do it when the
    endpoint has actually run out of room."""
    w = _window_wiring(tmp_path, monkeypatch, alpha_window=1000)
    g = gw(w, "model_a", max_output_tokens=200)
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: _resp_with_prompt(500))   # 500 left
    g.generate_native([{"role": "user", "content": "x"}])
    assert g.active_model == "alpha/m-1"


def test_an_undeclared_window_never_rebinds(tmp_path, monkeypatch):
    """No declared ceiling = nothing to be out of. Guessing one would move
    every long step off its bound endpoint for no stated reason."""
    providers = tmp_path / "llm_providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")   # no windows
    routes = tmp_path / "model_routes.json"
    routes.write_text(json.dumps(
        {"model_a": {"rotate": ["alpha/m-1"], "fallback": ["beta/m-1"]}}),
        encoding="utf-8")
    monkeypatch.setenv("ALPHA_KEY", "a")
    monkeypatch.setenv("BETA_KEY", "b")
    model_routes.reset_cache()
    g = gw((str(providers), str(routes)), "model_a", max_output_tokens=200)
    monkeypatch.setattr(litellm, "completion",
                        lambda **k: _resp_with_prompt(10 ** 9))
    g.generate_native([{"role": "user", "content": "x"}])
    assert g.active_model == "alpha/m-1"


def test_the_next_turn_actually_goes_to_the_bigger_endpoint(tmp_path, monkeypatch):
    """The rebind is only worth anything if _build_kwargs picks it up."""
    w = _window_wiring(tmp_path, monkeypatch, alpha_window=1000)
    g = gw(w, "model_a", max_output_tokens=200)
    keys = []

    def fake(**kwargs):
        keys.append(kwargs.get("api_key"))
        return _resp_with_prompt(900)

    monkeypatch.setattr(litellm, "completion", fake)
    g.generate_native([{"role": "user", "content": "x"}])
    g.generate_native([{"role": "user", "content": "y"}])
    assert keys == ["a", "b"], (
        "first turn on alpha, second on beta — one claim, not an `or` over "
        f"two: got {keys}")
