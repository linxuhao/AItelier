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


# ── The CLI loads compose's default file pair, not just the base ─────────────

def test_the_cli_passes_the_default_compose_file_set(tmp_path, monkeypatch):
    """A hand-run compose loads base + host-local override; so must the CLI.

    On 2026-11-25 the CLI passed only `-f docker-compose.yml` while the operator's
    hand-run `docker compose up -d` also loaded docker-compose.override.yml. The
    CLI's container silently lost the tailnet publish, MCP allowed hosts and the
    loop-stack watchdog variable. Both shapes are checked in a tmp project, so a
    gitignored override in the real checkout cannot flip this test's verdict.
    """
    project = (tmp_path / "project").resolve()
    project.mkdir()
    base = project / "docker-compose.yml"
    base.write_text("services: {}\n", encoding="utf-8")
    override = project / "docker-compose.override.yml"
    base_only = ["--project-directory", str(project), "-f", str(base)]

    for net in ("vip-gateway_default", None):
        if net is None:
            monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
        else:
            monkeypatch.setenv("AITELIER_EDGE_NETWORK", net)
        assert srv._compose_files(project) == base_only, (
            "without an override, the base alone — and no dependence on the "
            "edge network variable — is compose's default")

    override.write_text("services: {}\n", encoding="utf-8")
    pair = base_only + ["-f", str(override)]
    for net in ("vip-gateway_default", "another-network", None):
        if net is None:
            monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
        else:
            monkeypatch.setenv("AITELIER_EDGE_NETWORK", net)
        assert srv._compose_files(project) == pair, (
            "the override must follow the base in the same order compose uses, "
            "with no help from the edge network variable")


def test_the_module_default_names_the_repositorys_compose_files(monkeypatch):
    """With no explicit root, the project directory is _PROJECT_ROOT. Assert
    the same default pair against the tree the module actually imports from;
    never create or delete the gitignored override to make the test pass."""
    expected = ["-f", str(srv._COMPOSE_FILE)]
    if (ROOT / "docker-compose.override.yml").is_file():
        expected += ["-f", str(ROOT / "docker-compose.override.yml")]

    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "vip-gateway_default")
    assert srv._compose_files() == expected
    monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
    assert srv._compose_files() == expected


def test_no_compose_overlay_file_remains():
    """A leftover overlay would be a second, silently-diverging source of truth."""
    assert not (ROOT / "docker-compose.edge.yml").exists()


# ── Secret files ────────────────────────────────────────────────────────────

def test_compose_declares_no_per_key_secrets(tmp_path, monkeypatch, capsys):
    """The whole secrets dir is mounted; a per-key `secrets:` enumeration coming
    back would resurrect the compose/README drift class (QWEN_API_KEY,
    2026-08-27) and the bind-refusal on missing files with it."""
    assert not _compose("docker-compose.yml").get("secrets")


def test_an_optional_secret_is_not_manufactured(tmp_path, monkeypatch):
    """With the whole dir mounted, ABSENCE is the correct spelling of "I do not
    use this" — a manufactured empty file would only re-suggest the old
    enumeration contract. The dir itself must exist, as the user, 0700."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    srv._ensure_host_dirs()
    assert not (tmp_path / "s" / "GITHUB_TOKEN").exists()
    assert (tmp_path / "s").is_dir()


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
        assert name in (".AItelier", ".aitelier-secrets",
                        ".local/share/dsh-director/media",
                        ".local/share/aitelier/media-staging"), (
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


# ── The by-name edge network trades a loud failure for a silent one ────────

def test_an_unset_edge_network_says_nothing(monkeypatch, capsys):
    """Unset is the normal case — compose makes a throwaway and that is fine."""
    monkeypatch.delenv("AITELIER_EDGE_NETWORK", raising=False)
    srv._warn_if_edge_network_is_alone()
    assert capsys.readouterr().out == ""


def test_a_network_with_the_connector_on_it_says_nothing(monkeypatch, capsys):
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "cloudflare_edge")
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "aitelier cloudflared "})())
    srv._warn_if_edge_network_is_alone()
    assert capsys.readouterr().out == ""


def test_being_alone_on_the_named_network_is_reported(monkeypatch, capsys):
    """The typo signature. `external: true` used to refuse outright; dropping it
    is what lets a clean checkout start, but it also means a name matching
    nothing is CREATED — healthy container, localhost 200, public path dark,
    nothing said. For the tunnel to reach us the connector must be on that
    network, so being alone on it is checkable."""
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "clouflare_edge")   # typo
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": "aitelier "})())
    srv._warn_if_edge_network_is_alone()
    out = capsys.readouterr().out
    assert "clouflare_edge" in out and "docker network ls" in out


def test_a_missing_network_is_not_reported(monkeypatch, capsys):
    """Before the first `up` it does not exist yet; compose is about to make it."""
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "cloudflare_edge")
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stdout": ""})())
    srv._warn_if_edge_network_is_alone()
    assert capsys.readouterr().out == ""


def test_a_docker_failure_never_breaks_the_start(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("docker gone")
    monkeypatch.setenv("AITELIER_EDGE_NETWORK", "cloudflare_edge")
    monkeypatch.setattr(srv.subprocess, "run", boom)
    srv._warn_if_edge_network_is_alone()          # must not raise


def test_container_side_secrets_dir_is_scrubbed_from_the_environment(
        tmp_path, monkeypatch, capsys):
    """Setting the env to the CONTAINER-side path must be neutralised in
    os.environ itself — the first guard fixed only a local variable while
    _compose_env() passed the poisoned value to compose, whose mount source
    (${AITELIER_SECRETS_DIR:-…}) followed it: silently empty keys, with a
    message claiming the value was ignored."""
    monkeypatch.setenv("AITELIER_SECRETS_DIR", "/run/aitelier-secrets")
    srv._ensure_host_dirs()
    import os as _os
    assert "AITELIER_SECRETS_DIR" not in _os.environ
    assert "CONTAINER-side" in capsys.readouterr().out


# ── The search sidecar is optional (overfit review 2026-11-24, 阻-1) ────────

def test_a_default_up_does_not_need_the_pinned_search_image():
    """zvec-grep's image is a local digest with `pull_policy: never` and no
    build stanza (test_compose_semantic_worker_pin.py); it exists only where it
    was built. A default `docker compose up -d` must therefore not include it:
    the service sits behind a profile, and the backend's dependency on it is not
    required, so a clean checkout starts the backend without it."""
    c = _compose("docker-compose.yml")
    assert "zvec-grep" in (c["services"]["zvec-grep"].get("profiles") or []), (
        "zvec-grep must be opt-in (profile `zvec-grep`)")
    dep = c["services"]["aitelier"]["depends_on"]["zvec-grep"]
    assert dep.get("condition") == "service_healthy"
    assert dep.get("required") is False, (
        "a required dependency on a profiled service makes compose refuse the "
        "project: service \"aitelier\" depends on undefined service \"zvec-grep\"")


def test_the_index_lifecycle_is_off_unless_the_sidecar_is_opted_in():
    """With the lifecycle on and no sidecar, a run's first semantic_search waits
    the whole preparation budget for an index nobody builds."""
    env = _compose("docker-compose.yml")["services"]["aitelier"]["environment"]
    assert env["AITELIER_ZVEC_LIFECYCLE"] == "${AITELIER_ZVEC_LIFECYCLE:-0}"


def test_the_sidecar_files_carry_no_built_in_home():
    """The sidecar's home comes from the invoking user's HOME (compose) or a
    build argument, never from a home path written into the repository."""
    for rel in ("docker/zvec-grep.Dockerfile", "docker/zvec-grep-sdk.Dockerfile",
                "docker/zvec-grep-entrypoint.sh"):
        assert "/home/" not in (ROOT / rel).read_text(encoding="utf-8"), rel
    env = _compose("docker-compose.yml")["services"]["zvec-grep"]["environment"]
    assert env["HOME"] == env["ZVEC_GREP_HOME"] == "${HOME}/.AItelier/zvec-grep-home"


def test_the_published_backend_port_follows_the_cli_port_variable():
    """The CLI connects to AITELIER_PORT; the published host port is the same
    variable, so setting it moves both."""
    ports = _compose("docker-compose.yml")["services"]["aitelier"]["ports"]
    assert "127.0.0.1:${AITELIER_PORT:-4444}:4444" in ports, ports


def test_the_steward_secrets_bind_source_is_provisioned(tmp_path, monkeypatch):
    """docker-compose.yml bind-mounts the steward's connection secrets dir (rw,
    default under ~/.local/share/aitelier). Left to Docker it is created as
    root and the backend cannot store a connection's secret."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("AITELIER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AITELIER_STEWARD_SECRETS_HOST_DIR", raising=False)
    srv._ensure_host_dirs()
    default = tmp_path / "home" / ".local/share/aitelier/steward-mcp-secrets"
    assert default.is_dir()
    moved = tmp_path / "elsewhere" / "steward"
    monkeypatch.setenv("AITELIER_STEWARD_SECRETS_HOST_DIR", str(moved))
    srv._ensure_host_dirs()
    assert moved.is_dir()
