"""The fingerprint tool names a per-field change and never echoes an env value.

No docker and no network: every docker call goes through an injected runner, and
an autouse fixture replaces the module's ``subprocess`` handle with a tripwire so
a missing injection fails loudly instead of reaching a daemon.

Assertions are properties, not counts: the projection is bounded by the declared
whitelist, environment NAMES and their count survive, no environment VALUE is
printable, and the digests separate a restart from a configuration change.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy_config_fingerprint.py"
_spec = importlib.util.spec_from_file_location("deploy_config_fingerprint", SCRIPT)
fingerprint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fingerprint)

SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = "sha256:" + "1" * 64
INDEX_SHA = "b" * 64
SKILLFLOW_VERSION = "1.5.100"
# A bind source that must never be printed: the projection keeps target+mode.
HOST_ONLY_BIND_SOURCE = "/HOST-ONLY-BIND-SOURCE-not-for-output"


def sentinel(index):
    """A distinctive value that must be impossible to find in any output."""
    return f"SENTINEL-VALUE-{index}-NEVER-PRINT-9f3c"


# Every entry's value is a sentinel; the names are deliberately not sorted here.
ENV_PAIRS = [
    ("PATH", sentinel(0)),
    ("ARK_API_KEY", sentinel(1)),
    ("AITELIER_ALLOW_EXTERNAL", sentinel(2)),
    ("AITELIER_WRITERS", sentinel(3)),
    ("EMPTY_VALUE", ""),
]

# The assignment's whitelist, as projected paths.  Nothing else may be emitted.
WHITELIST = {
    "Image",
    "State.StartedAt",
    "RestartCount",
    "Config.Entrypoint",
    "Config.Cmd",
    "Config.WorkingDir",
    "Config.User",
    "Config.Env.names",
    "Config.Env.count",
    "Config.Labels.keys",
    "Config.Healthcheck.sha256",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.NetworkMode",
    "HostConfig.Binds",
    "HostConfig.PortBindings.keys",
    "HostConfig.CapAdd",
    "HostConfig.CapDrop",
    "HostConfig.SecurityOpt",
    "HostConfig.ShmSize",
    "HostConfig.ExtraHosts.count",
}
EXEC_PATHS = {
    "Exec.index_html.sha256",
    "Exec.index_html.reason",
    "Exec.skillflow_version",
    "Exec.skillflow_version.reason",
}
REQUIRED_PATHS = {
    "Image",
    "State.StartedAt",
    "RestartCount",
    "Config.Entrypoint",
    "Config.Cmd",
    "Config.WorkingDir",
    "Config.User",
    "Config.Env.names",
    "Config.Env.count",
    "Config.Labels.keys",
    "Config.Healthcheck.sha256",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.NetworkMode",
    "HostConfig.Binds",
    "HostConfig.PortBindings.keys",
    "HostConfig.CapAdd",
    "HostConfig.CapDrop",
    "HostConfig.SecurityOpt",
    "HostConfig.ShmSize",
    "HostConfig.ExtraHosts.count",
    "Exec.index_html.sha256",
    "Exec.skillflow_version",
}


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch):
    """Any real process start from this module is a bug in the injection."""

    class Tripwire:
        @staticmethod
        def run(*args, **kwargs):
            raise AssertionError(
                "deploy_config_fingerprint started a real process; "
                "every docker call must go through the injected runner"
            )

    monkeypatch.setattr(fingerprint, "subprocess", Tripwire())


def inspect_document(
    env=None,
    *,
    started_at="2026-11-07T09:00:00.000000000Z",
    restart_count=0,
    working_dir="/app",
    user="1000:1000",
    entrypoint=("/usr/local/bin/python",),
    cmd=("-m", "uvicorn", "api.main:app"),
    network_mode="edge",
    binds=(
        HOST_ONLY_BIND_SOURCE + ":/home/linxuhao/.AItelier:rw",
        "/run/aitelier-secrets:/run/aitelier-secrets:ro",
    ),
    labels=None,
    healthcheck=None,
    cap_add=("NET_ADMIN",),
    cap_drop=("ALL",),
    security_opt=("no-new-privileges:true",),
    shm_size=67108864,
    extra_hosts=("host.docker.internal:host-gateway",),
    restart_policy="unless-stopped",
    port_bindings=None,
):
    """A synthetic ``docker inspect`` document; no daemon involved."""
    return {
        "Image": IMAGE_ID,
        "State": {"StartedAt": started_at, "Running": True},
        "RestartCount": restart_count,
        "Config": {
            "Entrypoint": list(entrypoint),
            "Cmd": list(cmd),
            "WorkingDir": working_dir,
            "User": user,
            "Env": [f"{name}={value}" for name, value in (env or ENV_PAIRS)],
            "Labels": (
                {"com.example.owner": sentinel("label-value"), "com.example.tier": "a"}
                if labels is None
                else labels
            ),
            "Healthcheck": (
                {
                    "Test": ["CMD-SHELL", "curl -fsS http://127.0.0.1:4444/health || exit 1"],
                    "Interval": 30000000000,
                }
                if healthcheck is None
                else healthcheck
            ),
        },
        "HostConfig": {
            "RestartPolicy": {"Name": restart_policy, "MaximumRetryCount": 0},
            "NetworkMode": network_mode,
            "Binds": list(binds),
            "PortBindings": (
                {"4444/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4444"}]}
                if port_bindings is None
                else port_bindings
            ),
            "CapAdd": list(cap_add),
            "CapDrop": list(cap_drop),
            "SecurityOpt": list(security_opt),
            "ShmSize": shm_size,
            "ExtraHosts": list(extra_hosts),
        },
    }


def fake_runner(
    document,
    *,
    index_sha=INDEX_SHA,
    version=SKILLFLOW_VERSION,
    fail_index=False,
    fail_version=False,
    inspect_rc=0,
    inspect_stdout=None,
):
    """A runner over an in-memory document; records the argv it was handed."""
    calls = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        assert argv[0] == "docker", f"unexpected binary in {argv!r}"
        if argv[1] == "inspect":
            if inspect_rc:
                return inspect_rc, "", "Error: No such object: " + argv[2]
            if inspect_stdout is not None:
                return 0, inspect_stdout, ""
            return 0, json.dumps([document]), ""
        if argv[1] == "exec":
            if "sha256sum" in argv:
                if fail_index:
                    return 1, "", "sha256sum: /srv/web_dist/index.html: No such file or directory"
                return 0, index_sha + "  /srv/web_dist/index.html\n", ""
            if fail_version:
                return 1, "", "ModuleNotFoundError: No module named 'skillflow'"
            return 0, "skillflow-version=" + version + "\n", ""
        raise AssertionError(f"unexpected docker command {argv!r}")

    return run, calls


def capture_file(tmp_path, name, document, **runner_kwargs):
    """Run ``capture`` and return (path, parsed document, all visible text)."""
    runner, calls = fake_runner(document, **runner_kwargs)
    out = tmp_path / name
    assert fingerprint.main(["capture", "aitelier", str(out)], runner=runner) == 0
    parsed = json.loads(out.read_text(encoding="utf-8"))
    return out, parsed, out.read_text(encoding="utf-8"), calls


def run_diff(a_path, b_path, capsys):
    code = fingerprint.main(["diff", str(a_path), str(b_path)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def changed_lines(output):
    return [line for line in output.splitlines() if " -> " in line]


def split_change(line):
    """``path: A -> B`` -> (path, A, B); the A side must not be empty."""
    path, _, rest = line.partition(": ")
    left, arrow, right = rest.partition(" -> ")
    assert arrow == " -> ", line
    assert left, f"a changed field must print its A value too: {line!r}"
    assert right, f"a changed field must print its B value too: {line!r}"
    return path, left, right


def test_capture_keeps_names_and_count_but_no_value(tmp_path, capsys):
    doc = inspect_document()
    _, parsed, text, calls = capture_file(tmp_path, "a.json", doc)
    visible = text + "".join(capsys.readouterr())
    assert calls[0][:3] == ["docker", "inspect", "aitelier"]
    assert any(argv[:3] == ["docker", "exec", "aitelier"] for argv in calls[1:])

    # The entire point: not one environment value is printable.
    printed = [value for _, value in ENV_PAIRS if value]
    assert printed, "the fixture must carry non-empty sentinel values"
    for value in printed:
        assert value not in visible
    assert sentinel("label-value") not in visible
    assert HOST_ONLY_BIND_SOURCE not in visible

    projection = parsed["projection"]
    names = sorted(name for name, _ in ENV_PAIRS)
    assert projection["Config.Env.names"] == names
    assert projection["Config.Env.count"] == len(names)
    assert names, "the fixture must exercise more than an empty environment"

    # Only whitelisted paths, and every required path present.
    assert set(projection) <= WHITELIST | EXEC_PATHS
    assert REQUIRED_PATHS <= set(projection)
    # No aggregate dump smuggled in: every value is a scalar, a list of
    # strings/numbers, or unavailable text.
    for path, value in projection.items():
        assert not isinstance(value, dict), f"{path} is an unbounded dict"

    assert projection["Image"] == IMAGE_ID
    assert projection["Config.Entrypoint"] == ["/usr/local/bin/python"]
    assert projection["Config.Cmd"] == ["-m", "uvicorn", "api.main:app"]
    assert projection["Config.WorkingDir"] == "/app"
    assert projection["Config.User"] == "1000:1000"
    assert projection["Config.Labels.keys"] == ["com.example.owner", "com.example.tier"]
    assert SHA256.match(projection["Config.Healthcheck.sha256"])
    assert projection["HostConfig.RestartPolicy.Name"] == "unless-stopped"
    assert projection["HostConfig.NetworkMode"] == "edge"
    assert projection["HostConfig.Binds"] == [
        "/home/linxuhao/.AItelier:rw",
        "/run/aitelier-secrets:ro",
    ]
    assert projection["HostConfig.PortBindings.keys"] == ["4444/tcp"]
    assert projection["HostConfig.CapAdd"] == ["NET_ADMIN"]
    assert projection["HostConfig.CapDrop"] == ["ALL"]
    assert projection["HostConfig.SecurityOpt"] == ["no-new-privileges:true"]
    assert projection["HostConfig.ShmSize"] == 67108864
    assert projection["HostConfig.ExtraHosts.count"] == 1
    assert projection["Exec.index_html.sha256"] == INDEX_SHA
    assert projection["Exec.skillflow_version"] == SKILLFLOW_VERSION

    digests = parsed["digests"]
    assert SHA256.match(digests["structural_digest"])
    assert SHA256.match(digests["full_digest"])


def test_env_entries_without_equals_are_named_not_dropped(tmp_path):
    doc = inspect_document(env=[("PLAIN_NAME", ""), ("A", sentinel(4))])
    out, parsed, text, _ = capture_file(tmp_path, "e.json", doc)
    projection = parsed["projection"]
    assert projection["Config.Env.names"] == ["A", "PLAIN_NAME"]
    assert projection["Config.Env.count"] == 2
    assert sentinel(4) not in text


def test_failed_exec_is_recorded_as_unavailable_not_guessed(tmp_path):
    doc = inspect_document()
    out, parsed, text, _ = capture_file(
        tmp_path, "a.json", doc, fail_index=True, fail_version=True
    )
    visible = text
    for _, value in ENV_PAIRS:
        if value:
            assert value not in visible
    projection = parsed["projection"]
    assert projection["Exec.index_html.sha256"] == "unavailable"
    assert "Exec.index_html.reason" in projection
    assert "No such file" in projection["Exec.index_html.reason"]
    assert projection["Exec.skillflow_version"] == "unavailable"
    assert "Exec.skillflow_version.reason" in projection
    assert "ModuleNotFoundError" in projection["Exec.skillflow_version.reason"]
    # Nothing that looks like a digest or a version was invented.
    assert not SHA256.match(str(projection["Exec.index_html.sha256"]))
    assert projection["Exec.skillflow_version"] != SKILLFLOW_VERSION


def test_diff_of_identical_captures_is_quiet_and_zero(tmp_path, capsys):
    doc = inspect_document()
    a_path, a_doc, _, _ = capture_file(tmp_path, "a.json", doc)
    b_path, b_doc, _, _ = capture_file(tmp_path, "b.json", doc)
    code, output = run_diff(a_path, b_path, capsys)
    assert code == 0
    assert changed_lines(output) == []
    assert "differences: 0" in output
    assert "added" not in output and "removed" not in output
    assert a_doc["digests"] == b_doc["digests"]
    for label in ("structural_digest", "full_digest"):
        assert output.count(f"{label} A: ") == 1
        assert output.count(f"{label} B: ") == 1
        assert a_doc["digests"][label] in output


def test_one_changed_field_is_named_with_both_values_and_fails(tmp_path, capsys):
    a_path, a_doc, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    b_path, b_doc, _, _ = capture_file(
        tmp_path, "b.json", inspect_document(working_dir="/srv/app")
    )
    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    lines = changed_lines(output)
    assert len(lines) == 1, output
    path, left, right = split_change(lines[0])
    assert path == "Config.WorkingDir"
    assert left == "/app"
    assert right == "/srv/app"
    assert "differences: 1" in output
    assert a_doc["digests"]["structural_digest"] != b_doc["digests"]["structural_digest"]
    assert a_doc["digests"]["full_digest"] != b_doc["digests"]["full_digest"]


def test_restart_only_moves_full_digest_not_structural(tmp_path, capsys):
    a_path, a_doc, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    b_path, b_doc, _, _ = capture_file(
        tmp_path,
        "b.json",
        inspect_document(started_at="2026-11-07T11:22:33.444444444Z", restart_count=3),
    )
    assert a_doc["digests"]["structural_digest"] == b_doc["digests"]["structural_digest"]
    assert a_doc["digests"]["full_digest"] != b_doc["digests"]["full_digest"]

    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    named = [line.split(":", 1)[0] for line in changed_lines(output)]
    assert sorted(named) == ["RestartCount", "State.StartedAt"]
    assert "Config." not in output
    assert f"structural_digest A: {a_doc['digests']['structural_digest']}" in output
    assert f"structural_digest B: {b_doc['digests']['structural_digest']}" in output
    assert f"full_digest A: {a_doc['digests']['full_digest']}" in output
    assert f"full_digest B: {b_doc['digests']['full_digest']}" in output


def test_one_sided_fields_are_added_or_removed_not_a_change(tmp_path, capsys):
    a_path, _, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    b_path, b_doc, b_text, _ = capture_file(
        tmp_path, "b.json", inspect_document(), fail_index=True, fail_version=True
    )
    assert "Exec.index_html.reason" in b_doc["projection"]
    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    one_sided = [
        line for line in output.splitlines() if "added" in line or "removed" in line
    ]
    assert one_sided, output
    added = [line for line in one_sided if "added (only in B:" in line]
    assert added
    for line in added:
        assert " -> " not in line
        assert line.split(":", 1)[0] in EXEC_PATHS
    assert " -> " not in b_text

    # The values that DID change are still reported as a change, both sides.
    changed = set()
    for line in changed_lines(output):
        path, left, right = split_change(line)
        assert left != right, line
        changed.add(path)
    assert "Exec.index_html.sha256" in changed
    assert "Exec.skillflow_version" in changed

    # The other direction makes the newly-present keys ``removed`` (only in A).
    code, reversed_output = run_diff(b_path, a_path, capsys)
    assert code == 1
    removed = [line for line in reversed_output.splitlines() if "removed (only in A:" in line]
    assert removed, reversed_output
    for line in removed:
        assert " -> " not in line
        assert line.split(":", 1)[0] in EXEC_PATHS
    assert not [line for line in reversed_output.splitlines() if "added" in line]


def test_capture_failure_does_not_echo_the_document_or_a_value(tmp_path, capsys):
    broken = '{"Config": {"Env": ["ARK_API_KEY=' + sentinel(5) + '"]}'
    runner, _ = fake_runner(inspect_document(), inspect_stdout=broken)
    out = tmp_path / "never.json"
    code = fingerprint.main(["capture", "aitelier", str(out)], runner=runner)
    captured = capsys.readouterr()
    assert code != 0
    assert sentinel(5) not in captured.out + captured.err
    assert not out.exists()
    assert "capture failed" in captured.err

    # A missing container is a failure too, and docker's stderr is all it quotes.
    runner, _ = fake_runner(inspect_document(), inspect_rc=1)
    code = fingerprint.main(["capture", "aitelier", str(out)], runner=runner)
    captured = capsys.readouterr()
    assert code != 0
    assert not out.exists()
    for _, value in ENV_PAIRS:
        if value:
            assert value not in captured.out + captured.err


def test_unreadable_capture_file_is_a_usage_failure(tmp_path, capsys):
    good, _, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    assert fingerprint.main(["diff", str(good), str(tmp_path / "missing.json")]) == 2
    captured = capsys.readouterr()
    assert "diff failed" in captured.err
    assert changed_lines(captured.out) == []

    not_a_capture = tmp_path / "other.json"
    not_a_capture.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert fingerprint.main(["diff", str(good), str(not_a_capture)]) == 2
    assert "not a capture document" in capsys.readouterr().err
