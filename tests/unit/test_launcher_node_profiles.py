# tests/unit/test_launcher_node_profiles.py
# The two closed Node profiles on the existing launcher: the run binding is
# unchanged, the container gets the run's own `node-` ticket and nothing else,
# runs the operator's node executor image (never the pytest image), and the
# gate's own npm flags close lifecycle scripts, git and URL dependencies in the
# one launch that has a network.
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading

import pytest

from core import repository_executor as executor, repository_host_launcher as host

NODE_IMAGE = "sha256:" + "b" * 64


@pytest.fixture
def node(tmp_path):
    roots = tmp_path / "projects"
    repo = roots / "a"
    home = tmp_path / "home"
    for p in (repo, home):
        p.mkdir(parents=True)
    db = home / "aitelier.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE run_isolation(run_id TEXT,mode TEXT,worktree_path TEXT,"
                     "source_repo TEXT,project_id TEXT)")
        conn.execute("INSERT INTO run_isolation VALUES('run-a','worktree',?,?,'a')",
                     (str(repo), str(repo)))
    report_root = home / "gate-reports" / hashlib.sha256(b"run-a").hexdigest()
    ticket = report_root / "node-abc"
    work = ticket / "src" / "web"
    work.mkdir(parents=True)
    (ticket / "cache").mkdir()
    (ticket / "npmrc").write_text(executor.NODE_USERCONFIG_TEXT)
    (work / "package.json").write_text(json.dumps({"name": "x", "dependencies": {"a": "file:../lib"}}))
    (work / ".npmrc").write_text("git=./repo-git.sh\nallow-git=all\nignore-scripts=false\n")
    cfg = {"allowed_roots": [str(roots)], "aitelier_home": str(home), "state_db": str(db),
           "executor_entry": str(Path(executor.__file__).with_name("repository_executor_entry.py")),
           "test_image": "sha256:" + "a" * 64, "allowed_uids": [os.getuid()],
           "node_fetch_network": "aitelier-node-fetch", "node_executor_image": NODE_IMAGE}
    request = {"op": "launch", "run_id": "run-a", "repo": str(repo), "timeout": 30,
               "args": ["npm", "ci", "--offline"], "writable_dirs": [str(ticket)],
               "profile": "node_run", "workdir": str(work), "npm_cache": str(ticket / "cache")}
    return cfg, request, repo, ticket, work


def fetch(req, **kw):
    return dict(req, profile="node_fetch", args=["npm", "ci"], **kw)


def _refused(cfg, req, match):
    with pytest.raises(host.LaunchRefused, match=match):
        host.validate_request(cfg, req)


def test_run_profile_is_bound_to_the_owned_run(node):
    cfg, req, repo, ticket, work = node
    fields = host.validate_request(cfg, req)
    assert fields["network"] == "none" and fields["image"] == NODE_IMAGE
    assert fields["repo"] == str(repo) and fields["project_id"] == "a"
    assert fields["writable_dirs"] == [str(ticket)] and fields["workdir"] == str(work)


def test_docker_argv_for_node_profiles(node, monkeypatch):
    cfg, req, repo, ticket, work = node
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/bin/docker")
    run = host.docker_command(cfg, host.validate_request(cfg, req), "aitelier-cpu-x")
    assert run[run.index("--network") + 1] == "none"
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
    assert mounts[0] == f"{ticket}:{ticket}:rw" and str(repo) not in " ".join(run)
    assert run[run.index("-w") + 1] == str(work)
    assert run[run.index("--entrypoint") + 2] == NODE_IMAGE != cfg["test_image"]
    for flag, value in (("--cpus", "2"), ("--memory", "2g"), ("--pids-limit", "512")):
        assert run[run.index(flag) + 1] == value
    assert "--cap-drop" in run and "no-new-privileges" in run and "--rm" in run
    payload = json.loads(run[-1])
    assert payload["repo"] == str(work) and payload["npm_cache"] == str(ticket / "cache")
    assert payload["expect_npm"] == executor.NODE_NPM_VERSION == "12.2.0"
    assert payload["node_bin"] == executor.NODE_BIN_DIR
    assert payload["args"][:3] == ["npm", "ci", "--offline"]
    assert "--dangerously-allow-all-scripts" in payload["args"]
    assert f"--userconfig={ticket}/npmrc" in payload["args"]
    assert "--globalconfig=/dev/null" in payload["args"]
    build = host.docker_command(cfg, host.validate_request(cfg, dict(req, args=["npm", "run", "build"])), "y")
    assert json.loads(build[-1])["args"] == ["npm", "run", "build"]

    argv = host.docker_command(cfg, host.validate_request(cfg, fetch(req)), "aitelier-cpu-y")
    assert argv[argv.index("--network") + 1] == "aitelier-node-fetch"
    args = json.loads(argv[-1])["args"]
    assert args[:2] == ["npm", "ci"]
    for flag in ("--ignore-scripts", "--allow-git=none", "--git=/bin/false", "--allow-remote=none",
                 f"--userconfig={ticket}/npmrc", "--globalconfig=/dev/null"):
        assert flag in args
    assert "--dangerously-allow-all-scripts" not in args


@pytest.mark.parametrize("args", [["bash", "x.sh"], ["python3", "-m", "pytest"],
                                  ["npm", "run", "lint"], ["npm", "exec", "x"],
                                  ["npm", "ci"], ["npm", "install", "--offline", "--foo"]])
def test_run_commands_outside_the_closed_table_are_refused(node, args):
    cfg, req, *_ = node
    _refused(cfg, dict(req, args=args), "not allow-listed")


@pytest.mark.parametrize("args", [["npm", "ci", "--registry=http://x"], ["npm", "test"],
                                  ["npm", "ci", "--legacy-peer-deps"],
                                  ["npm", "install", "--script-shell=/bin/sh"],
                                  ["npm", "ci", "--offline"]])
def test_fetch_commands_outside_the_closed_table_are_refused(node, args):
    cfg, req, *_ = node
    _refused(cfg, dict(fetch(req), args=args), "not allow-listed")


def test_ticket_workdir_cache_and_user_config_must_be_the_runs_own(node, tmp_path):
    cfg, req, repo, ticket, work = node
    other = ticket.parent / "rt-123"                       # a repo-gate ticket
    (other / "src").mkdir(parents=True)
    (other / "cache").mkdir()
    _refused(cfg, dict(req, writable_dirs=[str(other)]), "not a node ticket")
    foreign = Path(cfg["aitelier_home"]) / "gate-reports" / ("0" * 64) / "node-x"
    _refused(cfg, dict(req, writable_dirs=[str(foreign)]), "not a node ticket")
    _refused(cfg, dict(req, workdir=str(repo)), "inside the ticket's src/")
    _refused(cfg, dict(req, workdir=str(work) + "/../.."), "normalized")
    _refused(cfg, dict(req, npm_cache=str(tmp_path)), "own cache")
    _refused(cfg, dict(req, writable_dirs=[str(ticket), str(other)]), "exactly")
    _refused(cfg, dict(req, report_dir=str(ticket)), "take no report_dir")
    _refused(cfg, dict(req, run_id="run-b"), "")           # no binding row
    (ticket / "npmrc").write_text("install-links=true\nscript-shell=/x\n")
    _refused(cfg, req, "npm user config is not the gate's")
    (ticket / "npmrc").unlink()
    (ticket / "npmrc").symlink_to(tmp_path / "elsewhere")
    _refused(cfg, req, "")
    del req["profile"]
    _refused(cfg, req, "belong to a node profile")


def test_links_inside_the_ticket_are_refused_without_reading_them(node, tmp_path):
    cfg, req, repo, ticket, work = node
    secret = tmp_path / "FAKE-secret.json"
    secret.write_text('{"FAKE": "SECRET"}')
    linked = ticket / "src" / "linked"
    linked.symlink_to(work)
    _refused(cfg, dict(req, workdir=str(linked)), "not a link")
    (work / "package.json").unlink()
    (work / "package.json").symlink_to(secret)
    with pytest.raises(host.LaunchRefused) as err:
        host.validate_request(cfg, req)
    assert "not a link" in str(err.value) and "SECRET" not in str(err.value)


def test_node_needs_the_operator_node_image_and_fetch_its_network(node):
    cfg, req, *_ = node
    no_image = {k: v for k, v in cfg.items() if k != "node_executor_image"}
    _refused(no_image, req, "no node executor image")
    _refused(no_image, fetch(req), "no node executor image")
    with pytest.raises(host.LaunchRefused, match="node_executor_image must be an immutable"):
        host.validate_request(dict(cfg, node_executor_image="aitelier:latest"), req)
    _refused({k: v for k, v in cfg.items() if k != "node_fetch_network"}, fetch(req),
             "not enabled by the operator")
    for bad in ("host", "none", "container:abc"):
        with pytest.raises(host.LaunchRefused, match="node_fetch_network"):
            host.validate_request(dict(cfg, node_fetch_network=bad), fetch(req))


def test_the_project_npmrc_and_its_dependency_specs_are_left_to_npm(node):
    """No homemade spec/lock grammar: npm 12 with the gate's own flags decides."""
    cfg, req, repo, ticket, work = node
    (work / "package.json").write_text(json.dumps({"name": "x", "dependencies": {
        "g": "github:user/repo", "u": "https://example.invalid/x.tgz", "d": "file:../lib"}}))
    (work / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "node_modules/d": {"resolved": "file:../lib"}}}))      # an ordinary npm 9 lock entry
    assert host.validate_request(cfg, fetch(req))["args"][:2] == ["npm", "ci"]
    assert not hasattr(executor, "node_fetch_refusal")
    assert not hasattr(executor, "build_fetch_view")


def test_requests_without_a_profile_are_unchanged(node, tmp_path):
    """The pytest/gate request carries no Node field, so an older launcher accepts it."""
    path = tmp_path / "c.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(2)
    listener.settimeout(10)
    seen = []

    def serve():
        for _ in range(2):
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                seen.append(json.loads(conn.recv(65536).split(b"\n")[0]))
                conn.sendall(json.dumps({"ok": True, "result": {
                    "returncode": 0, "stdout": "", "stderr": ""}}).encode() + b"\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        executor._launch_via_host(str(path), tmp_path, ["python3", "-m", "pytest"], 5, run_id="r")
        executor._launch_via_host(str(path), tmp_path, ["npm", "test"], 5, run_id="r",
                                  writable_dirs=["/t"], profile="node_run", workdir="/t/src",
                                  npm_cache="/t/cache")
    finally:
        thread.join(5)
        listener.close()
    assert set(seen[0]) == {"op", "repo", "args", "timeout", "writable_dirs", "relay_socket",
                            "import_module", "pytest_timeout", "report_dir", "run_id"}
    assert seen[1]["profile"] == "node_run" and seen[1]["workdir"] == "/t/src"


def test_a_result_without_the_pinned_npm_is_unavailable_not_a_red(tmp_path):
    path = tmp_path / "u.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(10)

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(json.dumps({"ok": True, "result": {
                "returncode": -1, "stdout": "", "stderr": "", "timed_out": False,
                "unavailable": "pinned npm 12.2.0 not in this image (found 9.2.0)"}}).encode() + b"\n")

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(executor.IsolationUnavailable, match="found 9.2.0"):
        executor._launch_via_host(str(path), tmp_path, ["npm", "test"], 5, run_id="r",
                                  writable_dirs=["/t"], profile="node_run", workdir="/t/src",
                                  npm_cache="/t/cache")
    listener.close()


def test_entry_refuses_without_the_pinned_npm(tmp_path):
    """The executor entry itself: no pinned npm on its PATH means nothing runs."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "entry", Path(executor.__file__).with_name("repository_executor_entry.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    marker = tmp_path / "RAN"
    result = entry.run({"args": ["sh", "-c", f"touch {marker}"], "timeout": 10,
                        "repo": str(tmp_path), "npm_cache": str(tmp_path),
                        "node_bin": str(tmp_path / "no-node-here"), "expect_npm": "12.2.0"})
    assert result["unavailable"].startswith("pinned npm 12.2.0 not in this image")
    assert not marker.exists()


def test_direct_adapter_validates_the_same_profile(node, monkeypatch):
    cfg, req, repo, ticket, work = node
    monkeypatch.setattr(executor.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setenv("AITELIER_HOME", cfg["aitelier_home"])
    monkeypatch.delenv(executor.NODE_FETCH_NETWORK_ENV, raising=False)
    monkeypatch.delenv(executor.NODE_EXECUTOR_IMAGE_ENV, raising=False)
    kw = dict(writable_dirs=[ticket], run_id="run-a", workdir=str(work), npm_cache=str(ticket / "cache"))
    with pytest.raises(executor.NodeRefused, match="no node executor image"):
        executor._execute(repo, ["npm", "test"], 5, profile="node_run", **kw)
    monkeypatch.setenv(executor.NODE_EXECUTOR_IMAGE_ENV, NODE_IMAGE)
    with pytest.raises(executor.NodeRefused, match="not enabled by the operator"):
        executor._execute(repo, ["npm", "ci"], 5, profile="node_fetch", **kw)
    with pytest.raises(executor.NodeRefused, match="not allow-listed"):
        executor._execute(repo, ["bash", "-c", "id"], 5, profile="node_run", **kw)


def test_read_regular_never_follows_a_link_at_any_component(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("ok")
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    assert executor.read_regular(tmp_path, "real/f", 10) == b"ok"
    with pytest.raises(executor.NodeRefused):
        executor.read_regular(tmp_path, "alias/f", 10)
    with pytest.raises(executor.NodeRefused, match="exceeds"):
        executor.read_regular(tmp_path, "real/f", 1)
    with pytest.raises(executor.NodeRefused):
        executor.read_regular(tmp_path, "../x", 10)
    assert executor.read_regular(tmp_path, "real/missing", 10) is None
    os.mkfifo(tmp_path / "real" / "pipe")
    with pytest.raises(executor.NodeRefused, match="regular"):
        executor.read_regular(tmp_path, "real/pipe", 10)
