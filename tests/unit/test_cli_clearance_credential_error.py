"""A broken credential is the caller's sentence, not a failed quiescence measurement.

MEASURED defect (iss-386b6fc49b4845d4, main 13b31da43d): with a driver id exported
and its token file missing, ``aitelier --oneshot hi`` exited 1 and printed
"Could not start server: refusing redeploy: ... (deployment quiescence measurement
could not start: CredentialError: driver token file ... is not readable)" on
stdout. ``_require_deployment_clearance`` folds every exception from
``_live_runtime_observation`` into ``dq.failed_observation``, so the caller's own
unusable credential was reported as a measurement failure and the CLI funnel
(``cli/app.py::_entrypoint``) never got the CredentialError it prints as one
sentence on stderr with exit 2.

The witness here drives the real ``_require_deployment_clearance``; only the live
observation, the cutover fence and ``dq.authorize`` are stubbed, because this is
about what the handler does with each exception class, not about measuring a host.
"""
from __future__ import annotations

import json

import pytest

from cli import server
from core import deployment_quiescence as dq
from core.driver_credentials import CredentialError


@pytest.fixture
def fenced(monkeypatch):
    """Record the fence handover instead of touching the operator's fence file."""
    released: list[str] = []
    monkeypatch.setattr(dq, "acquire_cutover_fence", lambda: "fence-test")
    monkeypatch.setattr(dq, "release_cutover_fence", released.append)
    return released


def test_a_credential_error_from_the_live_observation_is_not_a_failed_measurement(
    fenced, monkeypatch,
):
    def refuse(base_url, **kwargs):
        raise CredentialError("driver token file /nowhere.token is not readable (FileNotFoundError)")

    monkeypatch.setattr(server, "_live_runtime_observation", refuse)
    authorized = []
    monkeypatch.setattr(dq, "authorize", lambda *a, **k: authorized.append(k) or {"ok": True})

    with pytest.raises(CredentialError) as raised:
        server._require_deployment_clearance("redeploy", backend_service=True)

    # The sentence is the funnel's: the exception arrives unchanged, and no
    # observation was fabricated for it to be authorized against.
    assert "driver token file /nowhere.token is not readable" in str(raised.value)
    assert authorized == []
    # The fence taken before the observation is released on the way out.
    assert fenced == ["fence-test"]


def test_a_plain_failure_is_still_a_failed_measurement(fenced, monkeypatch):
    """Control: the old folding is untouched for everything that is not a credential error."""
    def unavailable(base_url, **kwargs):
        raise RuntimeError("docker daemon unreachable")

    monkeypatch.setattr(server, "_live_runtime_observation", unavailable)
    monkeypatch.setattr(dq, "load_override", lambda path: None)
    seen = {}

    def authorize(action, observation, **kwargs):
        seen["action"] = action
        seen["observation"] = observation
        return {"ok": True}

    monkeypatch.setattr(dq, "authorize", authorize)

    clearance = server._require_deployment_clearance("redeploy", backend_service=True)

    assert clearance["ok"] is True
    # The fence now belongs to the returned clearance, exactly as before.
    assert clearance["_cutover_fence"] == "fence-test"
    assert fenced == []
    assert seen["action"] == "redeploy"
    folded = json.dumps(seen["observation"], default=str)
    assert "deployment quiescence measurement could not start" in folded
    assert "RuntimeError: docker daemon unreachable" in folded
