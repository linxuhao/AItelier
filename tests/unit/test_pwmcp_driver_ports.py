"""The operator tool for per-driver PWMCP browser ports.

One Playwright MCP instance holds one client session, so a shared URL is not a
shared browser - the second driver's `initialize` evicts the first, whose next
call answers `-32001 Session not found` with nothing in the service log. These
tests cover the three things the tool must be trusted for: the allocator is
deterministic and stays in its documented range, the rendered drop-in reads back
through `core.dsh_driver.pwmcp_driver_ports()` unchanged and rewrites nothing,
and `status` inspects the host without touching it.

Every host probe is monkeypatched: these tests never run systemctl or docker,
and `run_command` - the module's only route to a process - is replaced with a
function that fails the test if something reaches it.
"""
import hashlib
from pathlib import Path

import pytest

from core.dsh_driver import PWMCP_DRIVER_PORTS_ENV, pwmcp_driver_ports
from deploy import pwmcp_driver_ports as tool


def run(capsys, *argv):
    """Invoke the command line in-process; return (exit code, stdout, stderr)."""
    code = tool.main([str(argument) for argument in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def derived(driver_id):
    """The allocation rule the module docstring documents, recomputed here.

    Written out rather than imported: the docstring promises a stable sha256
    fold into ALLOCATABLE, and this is that promise, independently of the code
    that implements it.
    """
    span = len(tool.ALLOCATABLE)
    digest = hashlib.sha256(driver_id.encode("utf-8")).digest()
    return tool.ALLOCATABLE[int.from_bytes(digest[:8], "big") % span]


def no_process(monkeypatch):
    """Fail the test if any command line is started."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"this path must not run a command: {args!r}")
    monkeypatch.setattr(tool, "run_command", refuse)


def profile_text(url):
    """A generated driver profile, in the shape `core/dsh_driver.py` dumps.

    `yaml.safe_dump(..., sort_keys=False)` keeps the keys in insertion order,
    which is why `serverName` precedes `url` inside each entry.
    """
    return (
        "- id: hmr\n"
        "  disabled: true\n"
        "- id: atelier-driver\n"
        "  name: /srv/checkout/integrations/dsh-driver/runtime.mjs\n"
        "- id: mcp-aitelier\n"
        "  name: '@deepseek-ai/dsh-mcp-client'\n"
        "  config:\n"
        "    serverName: aitelier\n"
        "    url: http://127.0.0.1:4444/mcp\n"
        "- id: mcp-pwmcp\n"
        "  name: '@deepseek-ai/dsh-mcp-client'\n"
        "  config:\n"
        "    serverName: pwmcp\n"
        "    transport: streamable-http\n"
        f"    url: {url}\n"
        "    failOnStartupError: false\n"
        "    toolCallTimeoutMs: 960000\n")


def write_profile(root, driver_id, url):
    path = Path(root) / driver_id / tool.PROFILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile_text(url))
    return path


# ── allocate ─────────────────────────────────────────────────────────────────


def test_allocate_prints_exactly_one_port_inside_the_range(capsys):
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", "")
    assert (code, err) == (0, "")
    assert out.count("\n") == 1, f"stdout must be the port and nothing else: {out!r}"
    assert int(out) in tool.ALLOCATABLE


def test_allocate_is_deterministic(capsys):
    first = run(capsys, "allocate", "dsh-1a2b", "--mapping", "")
    second = run(capsys, "allocate", "dsh-1a2b", "--mapping", "")
    assert first == second
    # ... and identical regardless of the other entries or their order: the
    # derived port only moves when a DIFFERENT driver already holds it.
    assert run(capsys, "allocate", "dsh-5e6f", "--mapping", "dsh-1a2b=9201")[1] == \
        run(capsys, "allocate", "dsh-5e6f", "--mapping", "")[1]


def test_allocate_follows_the_documented_rule(capsys):
    port = int(run(capsys, "allocate", "dsh-1a2b", "--mapping", "")[1])
    assert port == derived("dsh-1a2b")
    assert port in tool.ALLOCATABLE


def test_allocate_defaults_to_the_environment_variable(capsys, monkeypatch):
    monkeypatch.delenv(PWMCP_DRIVER_PORTS_ENV, raising=False)
    without = run(capsys, "allocate", "dsh-1a2b")
    monkeypatch.setenv(PWMCP_DRIVER_PORTS_ENV, "")
    empty = run(capsys, "allocate", "dsh-1a2b")
    assert without == empty
    monkeypatch.setenv(PWMCP_DRIVER_PORTS_ENV, "dsh-1a2b=9400")
    assert run(capsys, "allocate", "dsh-1a2b")[1] == "9400\n", (
        "an id already in the mapping keeps its port instead of being re-derived")


def test_two_different_ids_get_different_ports_against_an_empty_mapping(capsys):
    ids = ["dsh-1a2b", "dsh-3c4d", "dsh-5e6f", "claude", "grok"]
    ports = {driver_id: int(run(capsys, "allocate", driver_id, "--mapping", "")[1])
             for driver_id in ids}
    assert len(set(ports.values())) == len(ids), (
        f"two drivers on one port take turns evicting each other: {ports}")


def test_allocate_keeps_an_existing_entry_unchanged(capsys):
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", "dsh-1a2b=9500,claude=9501")
    assert (code, err, out) == (0, "", "9500\n")


def test_allocate_skips_a_port_the_mapping_gives_another_driver(capsys):
    taken = int(run(capsys, "allocate", "dsh-1a2b", "--mapping", "")[1])
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", f"claude={taken}")
    assert (code, err) == (0, "")
    assert int(out) in tool.ALLOCATABLE and int(out) != taken, (
        "a probe that walks past a taken port, rather than sharing it")


def test_allocate_refuses_a_mapping_that_cannot_be_parsed(capsys):
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", "claude=9200,broken")
    assert code == 1 and out == ""
    assert "broken" in err


# ── two bounds: ACCEPTED for what exists, ALLOCATABLE for what is invented ──


def test_the_tool_accepts_and_refuses_exactly_what_the_reader_does():
    """ACCEPTED is the reader's bound: the tool must never refuse what the driver
    accepts, nor accept what the driver refuses. Checked at both edges, read
    from the reader itself rather than from a copied number."""
    reader_floor, reader_ceiling = tool.ACCEPTED.start, tool.ACCEPTED.stop - 1
    assert pwmcp_driver_ports(f"dsh-1a2b={reader_floor}") == {"dsh-1a2b": reader_floor}
    assert pwmcp_driver_ports(f"dsh-1a2b={reader_ceiling}") == {"dsh-1a2b": reader_ceiling}
    for port in (reader_floor - 1, reader_ceiling + 1, 80, 70000):
        value = f"dsh-1a2b={port}"
        with pytest.raises(ValueError):
            pwmcp_driver_ports(value)
        with pytest.raises(ValueError):
            tool.parse_mapping(value)
    for value in ("dsh-1a2b=9400", "dsh-1a2b=8933", "dsh-1a2b=1024,dsh-3c4d=65535"):
        assert tool.parse_mapping(value) == pwmcp_driver_ports(value)


def test_a_legacy_port_below_the_allocation_range_renders_and_round_trips(tmp_path, capsys):
    dropin = tmp_path / "dropins"
    code, out, err = run(capsys, "render", "dsh-1a2b=8933", "--dropin", dropin)
    assert (code, err) == (0, "")
    text = (dropin / tool.DROPIN_FILENAME).read_text()
    assert pwmcp_driver_ports(tool.environment_value(text)) == {"dsh-1a2b": 8933}


def test_allocate_returns_an_existing_legacy_port_verbatim_with_one_note(capsys):
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", "dsh-1a2b=8933")
    assert (code, out) == (0, "8933\n")
    assert err.count("\n") == 1, f"exactly one informational line on stderr: {err!r}"
    assert "dsh-1a2b" in err and "8933" in err and "outside the allocation range" in err
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", "dsh-1a2b=9400")
    assert (code, out, err) == (0, "9400\n", ""), "an in-range existing port gets no note"


def test_an_invented_port_stays_in_the_allocation_range_beside_a_legacy_entry(capsys):
    code, out, err = run(capsys, "allocate", "dsh-3c4d", "--mapping", "dsh-1a2b=8933")
    assert (code, err) == (0, "")
    assert int(out) in tool.ALLOCATABLE and int(out) != 8933


def test_status_reports_a_legacy_port_the_host_already_runs(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(
        tmp_path, monkeypatch, live="dsh-1a2b=8933", listeners=[8933])
    write_profile(profiles, "dsh-1a2b", "http://127.0.0.1:8933/mcp")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    assert rows_after_table(out)["dsh-1a2b"] == [
        "8933", "http://127.0.0.1:8933/mcp", "yes"]


# ── refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mapping, entry", [
    ("dsh-1a2b=9200,Bad=9201", "Bad=9201"),          # driver id with upper case
    ("dsh-1a2b=9200,dsh_b=9201", "dsh_b=9201"),      # underscore is not allowed
    ("dsh-1a2b=9200,-dsh=9201", "-dsh=9201"),        # must start with an alnum
    ("dsh-1a2b=9200,dsh-=", "dsh-="),                # no port at all
    ("dsh-1a2b=9200,noport", "noport"),              # no "=" at all
    ("dsh-1a2b=9200,dsh-abc=80", "dsh-abc=80"),      # below ACCEPTED (1024)
    ("dsh-1a2b=9200,dsh-abc=70000", "dsh-abc=70000"),  # above ACCEPTED (65535)
    ("dsh-1a2b=9200,dsh-1a2b=9201", "dsh-1a2b=9201"),  # the same id twice
    ("dsh-1a2b=9200,dsh-abc=9200", "dsh-abc=9200"),  # two ids on one port
])
def test_a_mapping_the_tool_cannot_honour_is_refused(tmp_path, capsys, mapping, entry):
    """Refused, never silently repaired: a shared or re-derived port recreates
    exactly the eviction this mapping exists to prevent, with no symptom but
    `-32001 Session not found` at the driver."""
    dropin = tmp_path / "dropins"
    code, out, err = run(capsys, "render", mapping, "--dropin", dropin)
    assert code == 1 and out == ""
    assert entry in err, "the refusal must quote the offending entry verbatim"
    assert not dropin.exists(), "a refused mapping must write nothing"
    code, out, err = run(capsys, "allocate", "dsh-1a2b", "--mapping", mapping)
    assert code == 1 and out == ""
    assert entry in err


@pytest.mark.parametrize("driver_id", ["Bad", "dsh_b", "", "dsh a", "Dsh-1a2b"])
def test_allocate_refuses_a_driver_id_outside_the_pattern(capsys, driver_id):
    code, out, err = run(capsys, "allocate", driver_id, "--mapping", "")
    assert code == 1 and out == ""
    assert repr(driver_id) in err


# ── render ───────────────────────────────────────────────────────────────────


def test_rendered_dropin_round_trips_through_the_reader(tmp_path, capsys):
    """The whole point of the file: `core/dsh_driver.py` reads it back as asked."""
    dropin = tmp_path / "dropins"
    mapping = " dsh-3c4d=9402 , dsh-1a2b=9401 "
    code, out, err = run(capsys, "render", mapping, "--dropin", dropin)
    assert (code, err) == (0, "")
    text = (dropin / tool.DROPIN_FILENAME).read_text()
    value = tool.environment_value(text)
    assert value == "dsh-1a2b=9401,dsh-3c4d=9402"
    assert text.count("Environment=") == 1, "one assignment, not one per entry"
    assert "[Service]" in text
    assert pwmcp_driver_ports(value) == {"dsh-1a2b": 9401, "dsh-3c4d": 9402}
    assert pwmcp_driver_ports(value) == tool.parse_mapping(mapping)
    # Entries are sorted by driver id, and the value is quoted: an unquoted
    # systemd Environment= would split it on those very commas.
    assert text.count('Environment="' + PWMCP_DRIVER_PORTS_ENV + "=") == 1


def test_a_second_render_is_byte_identical(tmp_path, capsys):
    dropin = tmp_path / "dropins"
    mapping = "dsh-1a2b=9401,dsh-3c4d=9402"
    assert run(capsys, "render", mapping, "--dropin", dropin)[0] == 0
    path = dropin / tool.DROPIN_FILENAME
    first = path.read_bytes()
    before = path.stat()
    assert run(capsys, "render", mapping, "--dropin", dropin)[0] == 0
    assert path.read_bytes() == first
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns), (
        "an unchanged mapping must leave the file untouched, not rewrite equal bytes")
    # The same mapping typed in another order is the same statement.
    assert run(capsys, "render", "dsh-3c4d=9402,dsh-1a2b=9401", "--dropin", dropin)[0] == 0
    assert path.read_bytes() == first


def test_an_unrelated_dropin_keeps_its_bytes(tmp_path, capsys):
    dropin = tmp_path / "dropins"
    dropin.mkdir()
    neighbour = dropin / "20-other-service.conf"
    original = b'[Service]\nEnvironment="SOMETHING_ELSE=1"\n'
    neighbour.write_bytes(original)
    code, out, err = run(capsys, "render", "dsh-1a2b=9401", "--dropin", dropin)
    assert (code, err) == (0, "")
    assert neighbour.read_bytes() == original
    assert sorted(path.name for path in dropin.iterdir()) == [
        tool.DROPIN_FILENAME, "20-other-service.conf"], (
        "rendering must add one file and leave no temporary behind")


def test_render_prints_the_restart_it_does_not_run(tmp_path, capsys, monkeypatch):
    no_process(monkeypatch)
    dropin = tmp_path / "dropins"
    code, out, err = run(capsys, "render", "dsh-1a2b=9401", "--dropin", dropin,
                         "--unit", "fixture-host.service")
    assert (code, err) == (0, "")
    assert "systemctl --user daemon-reload && systemctl --user restart " \
           "fixture-host.service" in out
    assert "fixture-host.service" in out


def test_render_is_idempotent_on_an_empty_mapping(tmp_path, capsys):
    """An empty mapping is a statement too - "no per-driver ports" - and the
    reader already spells it that way."""
    dropin = tmp_path / "dropins"
    assert run(capsys, "render", "", "--dropin", dropin)[0] == 0
    path = dropin / tool.DROPIN_FILENAME
    first = path.read_bytes()
    assert pwmcp_driver_ports(tool.environment_value(path.read_text())) == {}
    assert run(capsys, "render", "", "--dropin", dropin)[0] == 0
    assert path.read_bytes() == first


# ── status ───────────────────────────────────────────────────────────────────


def status_fixture(tmp_path, monkeypatch, *, live, listeners=()):
    """Isolate status from the host: only the probes are replaced."""
    dropin = tmp_path / "dropins"
    profiles = tmp_path / "profiles"
    read_profiles = tool.probe_profiles
    monkeypatch.setattr(tool, "probe_unit_mapping", lambda unit: live)
    monkeypatch.setattr(tool, "probe_profiles", lambda root: read_profiles(profiles))
    monkeypatch.setattr(tool, "probe_listening", lambda *args, **kwargs: set(listeners))
    return dropin, profiles


def rows_after_table(out):
    """The table's data rows, keyed by driver id (columns are space-padded)."""
    lines = out.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("driver "))
    rows = {}
    for line in lines[start + 1:]:
        if not line or line.startswith("note:"):
            break
        fields = line.split()
        rows[fields[0]] = fields[1:]
    return rows


def test_status_reports_port_url_and_listener_per_driver(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(
        tmp_path, monkeypatch, live="dsh-1a2b=9401,dsh-3c4d=9402", listeners=[9401])
    write_profile(profiles, "dsh-1a2b", "http://127.0.0.1:9401/mcp")
    write_profile(profiles, "dsh-3c4d", "http://127.0.0.1:9402/mcp")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    rows = rows_after_table(out)
    assert rows == {"dsh-1a2b": ["9401", "http://127.0.0.1:9401/mcp", "yes"],
                    "dsh-3c4d": ["9402", "http://127.0.0.1:9402/mcp", "no"]}


def test_status_finds_drivers_only_a_profile_knows(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(tmp_path, monkeypatch, live="dsh-1a2b=9401")
    write_profile(profiles, "dsh-eeee", "http://127.0.0.1:9405/mcp")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    rows = rows_after_table(out)
    assert rows["dsh-eeee"] == ["9405", "http://127.0.0.1:9405/mcp", "no"], (
        "a driver with a profile but no mapping entry still gets a port, taken from its URL")
    assert rows["dsh-1a2b"] == ["9401", "(none)", "no"]


def test_status_flags_a_profile_whose_port_moved(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(tmp_path, monkeypatch, live="dsh-1a2b=9401")
    write_profile(profiles, "dsh-1a2b", "http://127.0.0.1:9402/mcp")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    assert "note: dsh-1a2b" in out and "9402" in out and "9401" in out


def test_status_reports_an_absent_or_stale_dropin(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(tmp_path, monkeypatch, live=None)
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    assert "unit not running" in out and "(no drop-in)" in out
    assert "no per-driver mapping and no generated profiles found" in out
    # A rendered drop-in is readable even when the unit is not running: it is
    # what the next start would apply.
    assert run(capsys, "render", "dsh-1a2b=9401", "--dropin", dropin)[0] == 0
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    assert rows_after_table(out)["dsh-1a2b"][0] == "9401"


def test_status_writes_nothing_at_all(tmp_path, capsys, monkeypatch):
    """Read-only means the report leaves every byte and timestamp where it was."""
    dropin, profiles = status_fixture(
        tmp_path, monkeypatch, live="dsh-1a2b=9401", listeners=[9401])
    write_profile(profiles, "dsh-1a2b", "http://127.0.0.1:9401/mcp")
    assert run(capsys, "render", "dsh-1a2b=9401", "--dropin", dropin)[0] == 0
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    no_process(monkeypatch)
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
             for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    assert after == before, "status must not create, change or remove a file"


def test_status_never_prints_the_environment(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(tmp_path, monkeypatch, live="dsh-1a2b=9401")
    monkeypatch.setenv("AITELIER_FIXTURE_SECRET", "must-not-be-printed")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert (code, err) == (0, "")
    assert "must-not-be-printed" not in out and "AITELIER_FIXTURE_SECRET" not in out
    assert out.count(PWMCP_DRIVER_PORTS_ENV) == 0, (
        "even the mapping variable is reported by its value, not by dumping a line of env")


def test_status_refuses_a_live_mapping_the_driver_would_refuse(tmp_path, capsys, monkeypatch):
    dropin, profiles = status_fixture(tmp_path, monkeypatch, live="dsh-1a2b=80")
    code, out, err = run(capsys, "status", "--unit", "fixture-host.service",
                         "--dropin", dropin, "--profiles", profiles)
    assert code == 1 and out == ""
    assert "dsh-1a2b=80" in err


def test_probe_unit_mapping_reads_only_the_one_variable(tmp_path, monkeypatch):
    """The live environment is read for one variable and nothing else is kept:
    that environment holds driver tokens, so it is never printed or returned."""
    environ = tmp_path / "4242" / "environ"
    environ.parent.mkdir()
    environ.write_bytes(b"PATH=/usr/bin\0" + PWMCP_DRIVER_PORTS_ENV.encode() +
                        b"=dsh-1a2b=9401\0AITELIER_DRIVER_TOKEN=not-yours\0")
    monkeypatch.setattr(tool, "PROC_ROOT", tmp_path)
    monkeypatch.setattr(tool, "probe_unit_main_pid", lambda unit: 4242)
    assert tool.probe_unit_mapping("fixture-host.service") == "dsh-1a2b=9401"
    monkeypatch.setattr(tool, "probe_unit_main_pid", lambda unit: 0)
    assert tool.probe_unit_mapping("fixture-host.service") is None, (
        "a unit that is not running has no live environment")


# ── the probes themselves, on fixture data ───────────────────────────────────


def test_profile_url_finds_only_the_pwmcp_entry():
    text = profile_text("http://127.0.0.1:9401/mcp")
    assert tool.profile_url(text) == "http://127.0.0.1:9401/mcp"
    assert tool.profile_url(profile_text("http://127.0.0.1:9401/mcp")
                            .replace("- id: mcp-pwmcp", "- id: mcp-other")
                            .replace("serverName: pwmcp", "serverName: other")) is None
    assert tool.profile_url("- id: mcp-pwmcp\n  config:\n    serverName: pwmcp\n") is None


def test_probe_profiles_lists_driver_directories_in_order(tmp_path):
    write_profile(tmp_path, "dsh-3c4d", "http://127.0.0.1:9402/mcp")
    write_profile(tmp_path, "dsh-1a2b", "http://127.0.0.1:9401/mcp")
    (tmp_path / "not-a-driver").mkdir()
    (tmp_path / "stray-file").write_text("ignored")
    profiles = tool.probe_profiles(tmp_path)
    assert [profile.driver_id for profile in profiles] == ["dsh-1a2b", "dsh-3c4d"]
    assert [profile.url for profile in profiles] == [
        "http://127.0.0.1:9401/mcp", "http://127.0.0.1:9402/mcp"]
    assert tool.probe_profiles(tmp_path / "missing") == []


def test_probe_listening_reads_listen_sockets_only(tmp_path):
    table = tmp_path / "tcp"
    table.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
        "   0: 0100007F:24C1 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000\n"
        "   1: 0100007F:24C2 0100007F:9000 01 00000000:00000000 00:00000000 00000000  1000\n"
        "   2: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000\n")
    assert tool.probe_listening(paths=(table,)) == {9409, 8080}


def test_the_usage_text_prints_both_documented_bounds(capsys):
    with pytest.raises(SystemExit) as exited:
        tool.main(["--help"])
    assert exited.value.code == 0
    out = capsys.readouterr().out
    assert f"{tool.ACCEPTED.start}..{tool.ACCEPTED.stop - 1}" in out
    assert f"{tool.ALLOCATABLE.start}..{tool.ALLOCATABLE.stop - 1}" in out
    assert PWMCP_DRIVER_PORTS_ENV in out
