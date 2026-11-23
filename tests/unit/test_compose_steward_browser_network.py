"""The steward's own browser network: the backend joins it, nothing else in compose does.

Step 24.3 (design-steward 7.8.13), director review 695: steward-pwmcp listens on 0.0.0.0
with no auth; --allowed-hosts checks only the Host header. On aitelier_default every
container there (aitelier-zg, aitelier-godot, ...) could drive the browser and skip the
gateway's owner-only audience. So deploy/pwmcp/run-pwmcp.sh puts it on its own network
(STEWARD_BROWSER_NETWORK, default steward-browser), and docker-compose.yml attaches the
`aitelier` service - and only it - to that same network, so a backend recreate keeps it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SCRIPT = (ROOT / "deploy" / "pwmcp" / "run-pwmcp.sh").read_text(encoding="utf-8")
KEY = "steward-browser"


def _joined(service: dict) -> set[str]:
    nets = service.get("networks") or ["default"]
    return set(nets) if isinstance(nets, list) else set(nets)


def test_only_the_backend_joins_the_browser_network():
    services = COMPOSE["services"]
    on_it = {name for name, service in services.items() if KEY in _joined(service)}
    assert on_it == {"aitelier"}, on_it
    for name in ("zvec-grep", "godot-builder"):
        assert KEY not in _joined(services[name]), name
        assert services[name].get("network_mode") is None, name  # no backdoor via a shared namespace


def test_no_service_shares_the_backends_network_namespace():
    for name, service in COMPOSE["services"].items():
        assert not str(service.get("network_mode") or "").startswith(("service:", "container:")), name


def test_the_network_has_the_scripts_name_and_reaches_the_web():
    config = COMPOSE["networks"][KEY]
    assert config["name"] == "${STEWARD_BROWSER_NETWORK:-steward-browser}"
    assert not config.get("internal") and not config.get("external")
    assert set(config) == {"name"}  # a plain bridge, like the one `docker network create` makes
    default = re.search(r"NET=\$\{STEWARD_BROWSER_NETWORK:-([^}]*)\}", SCRIPT)
    assert default and default.group(1) == "steward-browser"
