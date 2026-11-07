"""The fingerprint tool names a per-field change and never echoes an env value.

No docker and no network: every docker call goes through an injected runner, and
an autouse fixture replaces the module's ``subprocess`` handle with a tripwire so
a missing injection fails loudly instead of reaching a daemon.

Assertions are properties, not counts: the projection is exactly the declared
whitelist, environment NAMES and their count survive (an entry with no '=' is
skipped and still counted), no environment value is printable, a failing exec
probe yields a closed ``unavailable:`` token and never container-side text, every
field kind the criterion enumerates has its own single-field diff witness, and
the digests separate a restart from a configuration change.
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
OTHER_IMAGE_ID = "sha256:" + "2" * 64
INDEX_SHA = "b" * 64
SKILLFLOW_VERSION = "1.5.100"
# A bind source that must never be printed: the projection keeps target+mode.
HOST_ONLY_BIND_SOURCE = "/HOST-ONLY-BIND-SOURCE-not-for-output"
SECRETS_TARGET = "/run/aitelier-secrets"


def sentinel(index):
    """A distinctive value that must be impossible to find in any output."""
    return f"SENTINEL-VALUE-{index}-NEVER-PRINT-9f3c"


# Every entry's value is a distinctive sentinel; the names are deliberately not
# sorted here.  (An empty value cannot be a sentinel — "" is a substring of
# everything — so the empty-value case has its own explicit witness below.)
ENV_PAIRS = [
    ("PATH", sentinel(0)),
    ("ARK_API_KEY", sentinel(1)),
    ("AITELIER_ALLOW_EXTERNAL", sentinel(2)),
    ("AITELIER_WRITERS", sentinel(3)),
    ("HOME", sentinel(4)),
]
ENV_NAMES = sorted(name for name, _ in ENV_PAIRS)

# The assignment's whitelist, as projected paths.  EXEC_PATHS are the two
# "additionally, only when the container can be exec'd" fields.  Nothing else
# may be emitted, and no path here is conditional: the projection is closed.
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
EXEC_PATHS = {"Exec.index_html.sha256", "Exec.skillflow_version"}


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
    image=IMAGE_ID,
    started_at="2026-11-07T09:00:00.000000000Z",
    restart_count=0,
    working_dir="/app",
    user="1000:1000",
    entrypoint=("/usr/local/bin/python",),
    cmd=("-m", "uvicorn", "api.main:app"),
    network_mode="edge",
    binds=(
        HOST_ONLY_BIND_SOURCE + ":/home/linxuhao/.AItelier:rw",
        SECRETS_TARGET + ":" + SECRETS_TARGET + ":ro",
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
        "Image": image,
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
    index_rc=0,
    version_rc=0,
    index_stderr="",
    version_stderr="",
    version_stdout=None,
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
                if index_rc:
                    return index_rc, "", index_stderr or "sha256sum: permission denied"
                return 0, index_sha + "  /srv/web_dist/index.html\n", ""
            if version_rc:
                return version_rc, "", version_stderr or "ModuleNotFoundError: skillflow"
            if version_stdout is not None:
                return 0, version_stdout, ""
            return 0, "skillflow-version=" + version + "\n", ""
        raise AssertionError(f"unexpected docker command {argv!r}")

    return run, calls


def capture_file(tmp_path, name, document, **runner_kwargs):
    """Run ``capture`` and return (path, parsed document, file text, calls)."""
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


def one_sided_lines(output, marker):
    return [line for line in output.splitlines() if marker in line]


def split_change(line):
    """``path: A -> B`` -> (path, A, B); neither side may be empty."""
    path, _, rest = line.partition(": ")
    left, arrow, right = rest.partition(" -> ")
    assert arrow == " -> ", line
    assert left, f"a changed field must print its A value too: {line!r}"
    assert right, f"a changed field must print its B value too: {line!r}"
    return path, left, right


def rendered_matches(rendered, expected):
    """Compare a rendered diff side to the expected value."""
    if isinstance(expected, (list, dict)):
        return json.loads(rendered) == expected
    return rendered == expected


def test_capture_keeps_names_and_count_but_no_value(tmp_path, capsys):
    doc = inspect_document()
    _, parsed, text, calls = capture_file(tmp_path, "a.json", doc)
    visible = text + "".join(capsys.readouterr())
    assert calls[0][:3] == ["docker", "inspect", "aitelier"]
    assert any(argv[:3] == ["docker", "exec", "aitelier"] for argv in calls[1:])

    # The entire point: not one environment value is printable.
    assert all(value for _, value in ENV_PAIRS), "every value is a sentinel"
    for _, value in ENV_PAIRS:
        assert value not in visible
    assert sentinel("label-value") not in visible
    assert HOST_ONLY_BIND_SOURCE not in visible

    projection = parsed["projection"]
    assert projection["Config.Env.names"] == ENV_NAMES
    assert projection["Config.Env.count"] == len(ENV_PAIRS)

    # The projection is EXACTLY the declared whitelist: nothing conditional,
    # nothing extra (a free-text ``.reason`` field would fail right here), and
    # nothing missing.
    assert set(fingerprint._WHITELIST) == WHITELIST | EXEC_PATHS
    assert set(projection) == WHITELIST | EXEC_PATHS
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
        SECRETS_TARGET + ":ro",
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


def test_env_entries_without_equals_are_skipped_and_counted(tmp_path, capsys):
    bare = "BARE-ENTRY-" + sentinel("no-equals")
    doc = inspect_document()
    doc["Config"]["Env"].append(bare)          # a string with no '=' at all
    doc["Config"]["Env"].append([bare, "x"])   # not even a string
    _, parsed, text, _ = capture_file(tmp_path, "e.json", doc)
    visible = text + "".join(capsys.readouterr())
    projection = parsed["projection"]

    # Skipped, never kept whole: the entry's content is nowhere in the output.
    assert bare not in visible
    assert projection["Config.Env.names"] == ENV_NAMES
    # Counted: the two skipped entries still make the count larger.
    assert projection["Config.Env.count"] == len(ENV_PAIRS) + 2
    assert projection["Config.Env.count"] - len(projection["Config.Env.names"]) == 2


def test_an_empty_env_value_keeps_only_the_name(tmp_path):
    """An empty value cannot be a sentinel, so the exemption is asserted here."""
    doc = inspect_document(env=[("SHADOW_EMPTY", ""), ("HAS_VALUE", sentinel(5))])
    _, parsed, text, _ = capture_file(tmp_path, "e.json", doc)
    projection = parsed["projection"]
    assert projection["Config.Env.names"] == ["HAS_VALUE", "SHADOW_EMPTY"]
    assert projection["Config.Env.count"] == 2
    assert sentinel(5) not in text
    # The name survives while the ``NAME=`` entry form appears nowhere.
    assert "SHADOW_EMPTY" in text
    assert "SHADOW_EMPTY=" not in text


def test_failed_exec_is_recorded_as_unavailable_not_guessed(tmp_path, capsys):
    _, parsed, text, _ = capture_file(
        tmp_path, "a.json", inspect_document(), index_rc=1, version_rc=1
    )
    visible = text + "".join(capsys.readouterr())
    for _, value in ENV_PAIRS:
        assert value not in visible
    projection = parsed["projection"]
    assert projection["Exec.index_html.sha256"] == "unavailable:exec-exit-1"
    assert projection["Exec.skillflow_version"] == "unavailable:exec-exit-1"
    # No conditional free-text field was invented for the failure.
    assert set(projection) == WHITELIST | EXEC_PATHS
    assert not [path for path in projection if "reason" in path]
    assert not SHA256.match(projection["Exec.index_html.sha256"])
    assert projection["Exec.skillflow_version"] != SKILLFLOW_VERSION


@pytest.mark.parametrize(
    "code, expected",
    [
        (1, "unavailable:exec-exit-1"),
        (127, "unavailable:exec-exit-127"),
        (124, "unavailable:timeout"),
    ],
)
def test_probe_failure_is_a_closed_token(code, expected, tmp_path):
    _, parsed, _, _ = capture_file(
        tmp_path, "a.json", inspect_document(), index_rc=code, version_rc=code
    )
    projection = parsed["projection"]
    assert projection["Exec.index_html.sha256"] == expected
    assert projection["Exec.skillflow_version"] == expected


def test_probe_unparsable_output_is_unavailable_not_guessed(tmp_path):
    _, parsed, _, _ = capture_file(
        tmp_path,
        "a.json",
        inspect_document(),
        index_sha="not-a-digest",
        version_stdout="no marker here\n",
    )
    projection = parsed["projection"]
    assert projection["Exec.index_html.sha256"] == "unavailable:unexpected-output"
    assert projection["Exec.skillflow_version"] == "unavailable:unexpected-output"


def test_a_failing_probe_leaks_no_container_text(tmp_path, capsys):
    poison = sentinel("exec-stderr")
    a_path, a_doc, a_text, _ = capture_file(
        tmp_path,
        "a.json",
        inspect_document(),
        index_rc=1,
        index_stderr="sha256sum: permission denied: " + poison,
        version_rc=1,
        version_stderr="Traceback (most recent call last): " + poison,
    )
    assert poison not in a_text + "".join(capsys.readouterr())
    projection = a_doc["projection"]
    assert projection["Exec.index_html.sha256"] == "unavailable:exec-exit-1"
    assert projection["Exec.skillflow_version"] == "unavailable:exec-exit-1"
    assert set(projection) == WHITELIST | EXEC_PATHS

    # The same exit with entirely different stderr text is the SAME fingerprint:
    # no container text reaches the projection, so none can churn a digest.
    c_path, c_doc, c_text, _ = capture_file(
        tmp_path,
        "c.json",
        inspect_document(),
        index_rc=1,
        index_stderr="a completely different message",
        version_rc=1,
        version_stderr=poison + "-again",
    )
    assert poison not in c_text
    assert c_doc["digests"] == a_doc["digests"]
    code, output = run_diff(a_path, c_path, capsys)
    assert code == 0
    assert changed_lines(output) == []

    # Against a healthy capture the failure is named, and the text still absent.
    b_path, _, _, _ = capture_file(tmp_path, "b.json", inspect_document())
    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    assert poison not in output
    named = {split_change(line)[0] for line in changed_lines(output)}
    assert named == EXEC_PATHS


# Each enumerated field kind gets its own single-field witness: the "image id",
# the "restart policy", "a bind's rw->ro mode", the "port-binding keys" and the
# "sorted environment field-name list" of the criterion.
ENUMERATED_KINDS = [
    pytest.param(
        "Image", dict(image=OTHER_IMAGE_ID), IMAGE_ID, OTHER_IMAGE_ID, id="image-id"
    ),
    pytest.param(
        "HostConfig.RestartPolicy.Name",
        dict(restart_policy="no"),
        "unless-stopped",
        "no",
        id="restart-policy",
    ),
    pytest.param(
        "HostConfig.Binds",
        dict(
            binds=(
                HOST_ONLY_BIND_SOURCE + ":/home/linxuhao/.AItelier:rw",
                SECRETS_TARGET + ":" + SECRETS_TARGET + ":rw",
            )
        ),
        ["/home/linxuhao/.AItelier:rw", SECRETS_TARGET + ":ro"],
        ["/home/linxuhao/.AItelier:rw", SECRETS_TARGET + ":rw"],
        id="bind-mode-ro-to-rw",
    ),
    pytest.param(
        "HostConfig.PortBindings.keys",
        dict(
            port_bindings={
                "4444/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4444"}],
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}],
            }
        ),
        ["4444/tcp"],
        ["4444/tcp", "8000/tcp"],
        id="port-binding-keys",
    ),
    pytest.param(
        "Config.Env.names",
        dict(
            env=[("ARK_CLOUD_KEY" if name == "ARK_API_KEY" else name, value)
                 for name, value in ENV_PAIRS]
        ),
        ENV_NAMES,
        sorted("ARK_CLOUD_KEY" if name == "ARK_API_KEY" else name for name in ENV_NAMES),
        id="env-name-list",
    ),
]


@pytest.mark.parametrize("path, override, left_expected, right_expected", ENUMERATED_KINDS)
def test_each_enumerated_field_kind_is_named_alone(
    path, override, left_expected, right_expected, tmp_path, capsys
):
    a_path, _, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    b_path, _, _, _ = capture_file(tmp_path, "b.json", inspect_document(**override))
    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    lines = changed_lines(output)
    assert len(lines) == 1, output
    named, left, right = split_change(lines[0])
    assert named == path
    assert rendered_matches(left, left_expected), left
    assert rendered_matches(right, right_expected), right
    assert left != right
    assert "differences: 1" in output
    for _, value in ENV_PAIRS:
        assert value not in output


def test_a_two_part_bind_means_rw_not_ro(tmp_path):
    """Docker's own default for ``src:dst`` is rw; only an explicit ro flips it."""
    assert fingerprint.bind_targets(["/host:/target"]) == ["/target:rw"]
    assert fingerprint.bind_targets(["/host:/target:ro"]) == ["/target:ro"]
    assert fingerprint.bind_targets(["/host:/target:ro,Z"]) == ["/target:ro"]
    assert fingerprint.bind_targets(["named-volume:/target:rw"]) == ["/target:rw"]

    doc = inspect_document(
        binds=(
            HOST_ONLY_BIND_SOURCE + "/plain:/plain-target",
            HOST_ONLY_BIND_SOURCE + "/ro:/ro-target:ro",
        )
    )
    _, parsed, text, _ = capture_file(tmp_path, "b.json", doc)
    assert parsed["projection"]["HostConfig.Binds"] == [
        "/plain-target:rw",
        "/ro-target:ro",
    ]
    assert HOST_ONLY_BIND_SOURCE not in text


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
    """A capture written by an older/other tool may lack or gain a path."""
    a_path, a_doc, _, _ = capture_file(tmp_path, "a.json", inspect_document())
    b_path, b_doc, _, _ = capture_file(tmp_path, "b.json", inspect_document())
    dropped, gained = "HostConfig.ShmSize", "Exec.skillflow_version.probe"
    del b_doc["projection"][dropped]
    b_doc["projection"][gained] = "sha256sum"
    b_path.write_text(json.dumps(b_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    code, output = run_diff(a_path, b_path, capsys)
    assert code == 1
    assert changed_lines(output) == [], output
    removed = one_sided_lines(output, "removed (only in A:")
    added = one_sided_lines(output, "added (only in B:")
    assert [line.split(":", 1)[0] for line in removed] == [dropped]
    assert [line.split(":", 1)[0] for line in added] == [gained]
    for line in removed + added:
        assert " -> " not in line
    assert "differences: 2" in output

    # The other direction swaps the vocabulary, never the arrow form.
    code, reversed_output = run_diff(b_path, a_path, capsys)
    assert code == 1
    assert changed_lines(reversed_output) == [], reversed_output
    assert [line.split(":", 1)[0]
            for line in one_sided_lines(reversed_output, "removed (only in A:")] == [gained]
    assert [line.split(":", 1)[0]
            for line in one_sided_lines(reversed_output, "added (only in B:")] == [dropped]


def test_capture_failure_does_not_echo_the_document_or_a_value(tmp_path, capsys):
    broken = '{"Config": {"Env": ["ARK_API_KEY=' + sentinel(6) + '"]}'
    runner, _ = fake_runner(inspect_document(), inspect_stdout=broken)
    out = tmp_path / "never.json"
    code = fingerprint.main(["capture", "aitelier", str(out)], runner=runner)
    captured = capsys.readouterr()
    assert code != 0
    assert sentinel(6) not in captured.out + captured.err
    assert not out.exists()
    assert "capture failed" in captured.err

    # A missing container is a failure too, and docker's stderr is all it quotes.
    runner, _ = fake_runner(inspect_document(), inspect_rc=1)
    code = fingerprint.main(["capture", "aitelier", str(out)], runner=runner)
    captured = capsys.readouterr()
    assert code != 0
    assert not out.exists()
    for _, value in ENV_PAIRS:
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
