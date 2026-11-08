"""Compose `env_file` is the ONLY source of the deployed backend's write gate.

Since 2026-11-08 `api/main.py` no longer reads `<repo root>/.env` at import
(iss-f909af237ae944af): importing the app must not wear the deployment's write
gate. The deployed backend is a CONTAINER, and the one thing that still hands it
the gate configuration — AITELIER_CF_TEAM_DOMAIN / AITELIER_CF_AUD /
AITELIER_WRITERS / AITELIER_ADMIN_TOKEN, all read once by core/cf_access.py at
import — is the `aitelier` service's `env_file: - .env` (docker-compose.yml).
The CLI's own `load_repo_dotenv()` call runs on the HOST and cannot configure
the container.

So that one line is load-bearing, and nothing asserted it: deleting it left
every test in the repo green while the deployed backend started with the write
gate INACTIVE (`core/cf_access.py` treats "AITELIER_CF_AUD unset" as local dev,
so mutating requests stop requiring a credential — reads are open anyway, which
is why the loss is silent). This test fails if the service does not declare
`env_file`, if the entry does not name `.env`, or if a gate key is configured
through `environment:` instead (which would make `env_file` no longer the only
source).

It reads the compose file as data, never starts Docker: a disposable test
container has no docker binary, and the property under test is what the file
DECLARES.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

SERVICE = "aitelier"
ENV_FILE_KEY = "env_file"
ENV_FILE_ENTRY = ".env"

# The keys core/cf_access.py reads once at import to decide whether the write
# gate is active. Unset AITELIER_CF_AUD means "local dev: gate inactive".
GATE_KEYS = (
    "AITELIER_CF_TEAM_DOMAIN",
    "AITELIER_CF_AUD",
    "AITELIER_WRITERS",
    "AITELIER_ADMIN_TOKEN",
)


def _compose() -> dict:
    assert COMPOSE_PATH.is_file(), f"{COMPOSE_PATH} is missing"
    data = yaml.safe_load(COMPOSE_PATH.read_text())
    assert isinstance(data, dict), f"{COMPOSE_PATH} did not parse to a mapping"
    return data


def _service() -> dict:
    services = _compose().get("services") or {}
    assert SERVICE in services, (
        f"docker-compose.yml declares no `{SERVICE}` service — service "
        f"`{SERVICE}`, key `{ENV_FILE_KEY}`: the deployed backend gets its "
        f"write-gate configuration from this service's `{ENV_FILE_KEY}`")
    return services[SERVICE]


def _env_file_entries(value) -> list[str]:
    """Normalize compose's `env_file` shapes to the paths it names.

    Compose accepts a single path, a list of paths, and (v2.24+) a list of
    `{path: …, required: …}` mappings. All three naming `.env` is the same
    declaration; the test is about WHICH file is named, not which spelling.
    """
    if value is None:
        return []
    entries = value if isinstance(value, list) else [value]
    paths: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict) and "path" in entry:
            paths.append(str(entry["path"]))
        else:
            paths.append(repr(entry))
    return paths


def test_aitelier_service_declares_dotenv_as_its_env_file():
    """`services.aitelier.env_file` must name `.env` — it is the gate's source."""
    service = _service()

    assert ENV_FILE_KEY in service, (
        f"service `{SERVICE}` in docker-compose.yml does not declare "
        f"`{ENV_FILE_KEY}`: nothing else hands the container "
        f"AITELIER_CF_AUD / AITELIER_CF_TEAM_DOMAIN / AITELIER_WRITERS / "
        f"AITELIER_ADMIN_TOKEN — api/main.py stopped reading `.env` at import — "
        f"so the deployed backend would start with its write gate INACTIVE")

    paths = _env_file_entries(service[ENV_FILE_KEY])
    assert ENV_FILE_ENTRY in paths, (
        f"service `{SERVICE}` in docker-compose.yml declares `{ENV_FILE_KEY}` = "
        f"{paths!r}, which does not name `{ENV_FILE_ENTRY}`: that file's "
        f"AITELIER_CF_* keys are what turn on the deployed backend's write gate")


def test_write_gate_keys_are_not_configured_through_compose_environment():
    """`environment:` must not carry a gate key — `env_file` is the only source.

    If a gate key were also spelled out under `environment:`, `env_file` would
    no longer be the single thing the gate depends on, and the witness above
    would be asserting a redundancy instead of the source.
    """
    service = _service()
    environment = service.get("environment") or {}
    if isinstance(environment, list):                      # `- KEY=value` form
        pairs = []
        for entry in environment:
            key, _, value = str(entry).partition("=")
            pairs.append((key, value))
        environment = dict(pairs)
    declared = sorted(set(environment) & set(GATE_KEYS))
    assert not declared, (
        f"service `{SERVICE}` declares write-gate key(s) {declared} under "
        f"`environment:` — `{ENV_FILE_KEY}` is meant to be the only source of "
        f"the deployed backend's gate configuration")
