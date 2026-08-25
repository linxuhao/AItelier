"""A clean checkout must be able to start.

Both failures below were reproduced against a copy of docker-compose.yml with no
`.aitelier-secrets` and no pre-existing Docker network, and both stopped the
backend BEFORE any container ran, with an error naming a resource but not saying
what it was for or that it was optional:

    network vip-gateway_default declared as external, but could not be found
    invalid mount config for type "bind": bind source path does not exist:
        …/.aitelier-secrets/GITHUB_TOKEN

The first is why the cloudflared network is declared WITHOUT `external:` and
selected by name; the second is why the CLI provisions the secret files itself.

The first fix was originally an opt-in `-f docker-compose.edge.yml` overlay, and
that shape caused its own outage on 2026-08-25: a rebuild run as a plain
`docker compose up -d` recreated the container without the gateway network, the
container stayed healthy, 127.0.0.1:4444 kept answering 200, and the public path
was gone with nothing reporting it. A deployment property that a plain
`docker compose up -d` can silently drop is not a property. Hence the tests
below: the network is in the BASE file, and the service joins it there.
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
    """An external network makes compose refuse to start when it is absent.

    `edge` IS in this file — it just must not carry `external: true`. Naming a
    network compose may create keeps a clean checkout starting; declaring it
    external is what killed it before any container ran.
    """
    nets = _compose("docker-compose.yml").get("networks") or {}
    external = [n for n, cfg in nets.items() if (cfg or {}).get("external")]
    assert external == [], (
        f"{external} is external in the base file — a clean checkout cannot "
        f"`docker compose up`. Select the network by `name:` instead, so an "
        f"absent one is created rather than fatal.")


def test_the_service_joins_only_networks_the_base_file_defines():
    c = _compose("docker-compose.yml")
    defined = set((c.get("networks") or {}))
    joined = set(c["services"]["aitelier"].get("networks") or [])
    assert joined <= defined, f"undefined networks joined: {sorted(joined - defined)}"


def test_the_tunnel_network_is_in_the_base_file():
    """The public path must not depend on remembering an extra `-f`.

    This is the 2026-08-25 outage as a test: the service joins `edge` in the
    base file, so a plain `docker compose up -d` cannot produce a container
    that answers on localhost while the tunnel cannot reach it.
    """
    c = _compose("docker-compose.yml")
    assert "edge" in (c["services"]["aitelier"].get("networks") or []), (
        "the aitelier service does not join `edge` in the base compose file — "
        "a plain `docker compose up -d` would drop the public path silently")
    assert "edge" in (c.get("networks") or {})


def test_the_tunnel_network_is_selected_by_name_with_a_harmless_default():
    """Set → join the real network. Unset → compose creates a throwaway and a
    clean checkout still starts. Both branches come from one variable."""
    raw = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${AITELIER_EDGE_NETWORK:-" in raw, (
        "the edge network must be named by ${AITELIER_EDGE_NETWORK:-<fallback>} "
        "— a bare name would join the wrong network on every other host")


# ── One compose file, so the CLI and a hand-run compose cannot disagree ─────

def test_the_cli_passes_only_the_base_file(monkeypatch):
    """No overlay to opt into means no way for the CLI and a hand-run
    `docker compose` to be looking at different files."""
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "vip-gateway_default")
    with_net = srv._compose_files()
    monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
    assert srv._compose_files() == with_net, (
        "the file list still varies with the environment — that is the split "
        "that let a plain `up -d` drop the public path")
    assert with_net.count("-f") == 1


def test_no_compose_overlay_file_remains():
    """A leftover overlay would be a second, silently-diverging source of truth."""
    assert not (ROOT / "docker-compose.edge.yml").exists()


# ── Secret files ────────────────────────────────────────────────────────────

def test_every_mounted_secret_is_provisioned(tmp_path, monkeypatch, capsys):
    """Whatever docker-compose mounts, the CLI must create — or `up` dies on a
    bind mount. Read the names from the compose file so a NEW secret cannot be
    added there and silently left unprovisioned."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_host_dirs()
    declared = set(_compose("docker-compose.yml").get("secrets") or {})
    created = {p.name for p in (tmp_path / "s").iterdir()}
    assert declared <= created, f"never created: {sorted(declared - created)}"


def test_an_optional_secret_is_created_empty(tmp_path, monkeypatch):
    """Empty is the correct content for "I do not use this" — the git credential
    helper documents exactly that reading."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_host_dirs()
    assert (tmp_path / "s" / "GITHUB_TOKEN").read_text() == ""


def test_a_missing_llm_key_is_reported_with_the_fix(tmp_path, monkeypatch, capsys):
    """It is still created (so the container starts) but the user is told once,
    with the command — an empty key would otherwise surface as an auth error on
    the first model call, far from the cause."""
    from core.external_deps import required_llm_keys
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_host_dirs()
    out = capsys.readouterr().out
    assert "printf" in out
    for key in required_llm_keys():
        assert key in out


def test_the_key_it_names_is_the_one_the_configs_need(tmp_path, monkeypatch,
                                                      capsys):
    """Caught on a cold install: the CLI said "write DEEPSEEK_API_KEY" on the
    same machine where the README (correctly) said ARK_API_KEY, because this
    hard-coded a vendor into a provider-agnostic system. Derive it, or it goes
    stale the moment the agent_configs move."""
    from core.external_deps import required_llm_keys
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_host_dirs()
    out = capsys.readouterr().out
    needed = set(required_llm_keys())
    assert needed, "could not derive what the shipped configs need"
    told = {line.rsplit("/", 1)[-1].split()[0]
            for line in out.splitlines() if "printf" in line}
    assert told == needed, f"CLI says {sorted(told)}, configs need {sorted(needed)}"


def test_an_existing_key_is_never_overwritten(tmp_path, monkeypatch, capsys):
    from core.external_deps import required_llm_keys
    d = tmp_path / "s"
    d.mkdir()
    for key in required_llm_keys():
        (d / key).write_text("sk-real")
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(d))
    srv._ensure_host_dirs()
    for key in required_llm_keys():
        assert (d / key).read_text() == "sk-real"
    assert "printf" not in capsys.readouterr().out


def test_an_unwritable_home_does_not_stop_the_start(tmp_path, monkeypatch):
    """A user who mounts secrets some other way must not be blocked by this."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x")
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(blocked / "s"))
    srv._ensure_host_dirs()          # must not raise


def test_the_state_root_is_created_by_us_not_by_docker(tmp_path, monkeypatch):
    """Docker creates a missing bind-mount source as ROOT. The container runs as
    the host uid, so an auto-created `~/.AItelier` is unwritable and the app dies
    on `sqlite3.OperationalError: unable to open database file` and crash-loops —
    a message that names neither permissions nor the directory.

    Reproduced on a second machine with a virgin HOME; invisible on any host that
    has had `~/.AItelier` for months, which is every developer's."""
    state = tmp_path / "state" / ".AItelier"
    monkeypatch.setenv("AITELIER_STATE_DIR", str(state))
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    assert not state.exists()
    srv._ensure_host_dirs()
    assert state.is_dir(), "compose would bind-mount a path Docker creates as root"


def test_everything_compose_bind_mounts_is_provisioned(tmp_path, monkeypatch):
    """Read the mount targets from compose itself: a new bind mount added there
    must not be left for Docker to create as root."""
    import re
    compose = _compose("docker-compose.yml")
    vols = compose["services"]["aitelier"].get("volumes") or []
    homed = [v for v in vols if isinstance(v, str) and v.startswith("${HOME}")]
    assert homed, "expected at least one ${HOME}-rooted bind mount"
    # Every one of them must be a path the CLI creates. Today that is .AItelier.
    for v in homed:
        src = v.split(":")[0]
        name = re.sub(r"^\$\{HOME\}/", "", src)
        assert name == ".AItelier", (
            f"{src} is bind-mounted but nothing creates it — Docker will, as "
            f"root, and the container (host uid) will not be able to write it.")


# ── The image can be older than the dependencies it must satisfy ────────────

def test_a_fresh_image_is_not_called_stale(monkeypatch, tmp_path):
    """Rebuilding on every start would cost ~50s each time for nothing."""
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "2999-01-01T00:00:00.000000000Z"})())
    assert srv._image_deps_are_stale() is False


def test_an_image_older_than_pyproject_is_stale(monkeypatch):
    """The repo is bind-mounted, so the container runs current SOURCE against
    the image's packages. `docker compose up -d` reuses an existing
    `aitelier:latest`, so an image predating a new dependency gives

        ModuleNotFoundError: No module named 'mcp'

    Reproduced on a clean machine whose image predated `mcp` by five days.
    Invisible to anyone who habitually runs `up -d --build` — i.e. every
    developer, and no new user."""
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "2000-01-01T00:00:00.000000000Z"})())
    assert srv._image_deps_are_stale() is True


def test_docker_nanosecond_precision_parses(monkeypatch):
    """Docker stamps 9 fractional digits; fromisoformat takes 6. A crash here
    would be swallowed and read as "fresh", which is the failing direction."""
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "2000-01-01T00:00:00.123456789Z"})())
    assert srv._image_deps_are_stale() is True


def test_a_docker_failure_never_blocks_the_start(monkeypatch):
    """A freshness heuristic must not be able to stop the server starting."""
    def boom(*a, **k):
        raise OSError("docker gone")
    monkeypatch.setattr(srv.subprocess, "run", boom)
    assert srv._image_deps_are_stale() is False


# ── One entry point must not bypass what the others enforce ────────────────

def test_aitelier_server_starts_the_container_not_a_host_process(monkeypatch):
    """`ensure_server_running` refuses a host process on purpose: a pipeline's
    git commits would carry the host developer's identity instead of the
    image's. `aitelier server` ran uvicorn directly and bypassed that — one
    entry point enforcing an invariant while another ignores it is the
    invariant not existing.

    Found by an agent installing from scratch: `aitelier server` bound :4444 on
    the host, and its `docker compose up` then died with "address already in
    use"."""
    import cli.app as app
    called = {}
    monkeypatch.setattr("cli.server.ensure_server_running",
                        lambda *a, **k: called.setdefault("docker", True))
    monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                        type("M", (), {"run": lambda *a, **k: called.setdefault("host", True)}))
    app.server(host="0.0.0.0", port=4444, no_docker=False)
    assert called == {"docker": True}, "server must start the container"


def test_no_docker_is_available_but_says_what_it_costs(monkeypatch, capsys):
    """The escape hatch stays — debugging outside a container is legitimate —
    but silently changing commit authorship is not."""
    import cli.app as app
    ran = {}
    monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                        type("M", (), {"run": lambda *a, **k: ran.setdefault("host", True)}))
    monkeypatch.setattr("cli.server.ensure_server_running",
                        lambda *a, **k: ran.setdefault("docker", True))
    app.server(host="127.0.0.1", port=4444, no_docker=True)
    assert ran == {"host": True}
    out = capsys.readouterr().out
    assert "git identity" in out or "git commits" in out
