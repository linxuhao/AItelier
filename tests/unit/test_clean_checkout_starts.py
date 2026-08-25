"""A clean checkout must be able to start.

Both failures below were reproduced against a copy of docker-compose.yml with no
`.aitelier-secrets` and no pre-existing Docker network, and both stopped the
backend BEFORE any container ran, with an error naming a resource but not saying
what it was for or that it was optional:

    network vip-gateway_default declared as external, but could not be found
    invalid mount config for type "bind": bind source path does not exist:
        …/.aitelier-secrets/GITHUB_TOKEN

The first is why the cloudflared network moved to an opt-in overlay; the second
is why the CLI provisions the secret files itself.
"""
import os
from pathlib import Path

import pytest
import yaml

import cli.server as srv

ROOT = Path(__file__).resolve().parents[2]


# ── The base compose file is self-contained ─────────────────────────────────

def _compose(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_the_base_compose_declares_no_external_network():
    """An external network makes compose refuse to start when it is absent."""
    nets = _compose("docker-compose.yml").get("networks") or {}
    external = [n for n, cfg in nets.items() if (cfg or {}).get("external")]
    assert external == [], (
        f"{external} is external in the base file — a clean checkout cannot "
        f"`docker compose up`. Put it in docker-compose.edge.yml.")


def test_the_service_joins_only_networks_the_base_file_defines():
    c = _compose("docker-compose.yml")
    defined = set((c.get("networks") or {}))
    joined = set(c["services"]["aitelier"].get("networks") or [])
    assert joined <= defined, f"undefined networks joined: {sorted(joined - defined)}"


def test_the_edge_overlay_still_provides_the_tunnel_network():
    """Moving it out must not delete it — the tunnel deployment still needs it."""
    c = _compose("docker-compose.edge.yml")
    assert (c["networks"]["edge"] or {}).get("external") is True
    assert "edge" in c["services"]["aitelier"]["networks"]


# ── The CLI only opts in to the overlay deliberately ────────────────────────

def test_the_overlay_is_absent_by_default(monkeypatch):
    monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
    assert "docker-compose.edge.yml" not in " ".join(srv._compose_files())


def test_naming_the_network_opts_the_overlay_in(monkeypatch):
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "vip-gateway_default")
    assert "docker-compose.edge.yml" in " ".join(srv._compose_files())


def test_a_blank_value_does_not_opt_in(monkeypatch):
    """`AITELIER_EDGE_NETWORK=` in a .env is "unset", not "use the default"."""
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "   ")
    assert "docker-compose.edge.yml" not in " ".join(srv._compose_files())


# ── Secret files ────────────────────────────────────────────────────────────

def test_every_mounted_secret_is_provisioned(tmp_path, monkeypatch, capsys):
    """Whatever docker-compose mounts, the CLI must create — or `up` dies on a
    bind mount. Read the names from the compose file so a NEW secret cannot be
    added there and silently left unprovisioned."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_secret_files()
    declared = set(_compose("docker-compose.yml").get("secrets") or {})
    created = {p.name for p in (tmp_path / "s").iterdir()}
    assert declared <= created, f"never created: {sorted(declared - created)}"


def test_an_optional_secret_is_created_empty(tmp_path, monkeypatch):
    """Empty is the correct content for "I do not use this" — the git credential
    helper documents exactly that reading."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_secret_files()
    assert (tmp_path / "s" / "GITHUB_TOKEN").read_text() == ""


def test_a_missing_llm_key_is_reported_with_the_fix(tmp_path, monkeypatch, capsys):
    """It is still created (so the container starts) but the user is told once,
    with the command — an empty key would otherwise surface as an auth error on
    the first model call, far from the cause."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_secret_files()
    out = capsys.readouterr().out
    assert "DEEPSEEK_API_KEY" in out and "printf" in out


def test_an_existing_key_is_never_overwritten(tmp_path, monkeypatch, capsys):
    d = tmp_path / "s"
    d.mkdir()
    (d / "DEEPSEEK_API_KEY").write_text("sk-real")
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(d))
    srv._ensure_secret_files()
    assert (d / "DEEPSEEK_API_KEY").read_text() == "sk-real"
    assert "printf" not in capsys.readouterr().out


def test_an_unwritable_home_does_not_stop_the_start(tmp_path, monkeypatch):
    """A user who mounts secrets some other way must not be blocked by this."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x")
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(blocked / "s"))
    srv._ensure_secret_files()          # must not raise
