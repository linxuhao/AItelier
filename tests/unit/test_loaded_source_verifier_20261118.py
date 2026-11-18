"""/health's `source_identity` must have a consumer that really runs, and one rule.

The backend reports which source bytes the answering process imported (root, a
digest captured once at import, the capture moment). These tests execute the two
halves of the contract the consumer exists for:

* a LIVE answer, produced by a real uvicorn process serving ``api.main:app`` on
  loopback inside this test, checked by ``python -m deploy.loaded_source`` against
  the tree that process imported -> exit 0 and equal digests
  (``test_the_verifier_agrees_with_the_live_process``);
* the same answer checked against a COPY of that tree whose
  ``api/_read_cache.py`` bytes differ -> exit 1, both digests printed, and the
  differing file named
  (``test_the_verifier_names_the_file_whose_bytes_differ``);
* the answer's digest equals ``core.source_identity``'s computation on the same
  tree, in this process, independently of the verifier subprocess, so the shared
  implementation is shown to be the one both sides use
  (``test_the_reported_digest_is_the_shared_implementations_value``);
* the rule's loop exists in exactly ONE place, and that place is
  ``core/source_identity.py``: ``api/main.py`` imports it instead of owning a
  copy, and no other non-test module the repository TRACKS carries the rule's
  fingerprint (``test_the_digest_rule_is_implemented_exactly_once``, with
  ``test_the_scan_ignores_files_the_repository_does_not_track`` holding the
  difference between that scan and a filesystem walk - the deployed checkout
  carries untracked ``.cache/`` wheels full of third-party Python).

The server is started with ``--lifespan off`` on purpose: /health's payload does
not touch what the lifespan builds (State backend, scheduler, claims), and the
identity under test is captured when ``api.main`` is IMPORTED, which happens
either way. Starting the real lifespan in a test container would contend for the
single-instance lock and the State pool to prove nothing about this field.

Nothing here edits a file that the serving process imported from its own tree:
the mismatch arm runs against a copy made under the test's temp directory, so the
live answer stays trustworthy for the whole module.
"""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from core.source_identity import compute_digest

REPO_ROOT = Path(__file__).resolve().parents[2]

# One covered file, mutated in a COPY. It is covered by the rule and imported by
# the serving module, which the live arm asserts.
MUTATED_FILE = "api/_read_cache.py"

STARTUP_TIMEOUT = 120.0
VERIFIER_TIMEOUT = 120.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _answer(url: str, timeout: float = 5.0):
    """The /health body, or None while the server is not answering yet."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


@pytest.fixture(scope="module")
def live_backend():
    """A real uvicorn process on loopback; yields (url, source_identity)."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/health"
    log_path = Path(tempfile.mkdtemp(prefix="loaded-source-uvicorn-")) / "server.log"
    proc = None
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.main:app",
             "--host", "127.0.0.1", "--port", str(port),
             "--lifespan", "off", "--no-access-log", "--log-level", "warning"],
            cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + STARTUP_TIMEOUT
        body = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            body = _answer(url)
            if isinstance(body, dict) and "source_identity" in body:
                break
            body = None
            time.sleep(0.05)
        try:
            if body is None:
                log.flush()
                output = log_path.read_text(errors="replace")[-4000:]
                pytest.fail(f"uvicorn never answered {url}\n--- server log ---\n{output}")
            yield url, body["source_identity"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)


def _run_verifier(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "deploy.loaded_source", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=VERIFIER_TIMEOUT,
    )


def test_the_verifier_agrees_with_the_live_process(live_backend):
    """Arm one: a live answer, the tree its process imported, exit 0."""
    url, identity = live_backend

    # The process imported the checkout this test lives in - not a guess: the
    # server was started there, and its answer names it.
    assert Path(identity["root"]).resolve() == REPO_ROOT
    assert identity["reason"] is None
    assert list(identity["files"])

    json_run = _run_verifier("--url", url, "--tree", str(REPO_ROOT), "--json")
    assert json_run.returncode == 0, (json_run.returncode, json_run.stdout, json_run.stderr)
    report = json.loads(json_run.stdout)
    assert report["status"] == "match"
    assert report["reported_digest"] == identity["digest"]
    assert report["recomputed_digest"] == identity["digest"]
    assert report["reported_root"] == identity["root"]
    assert report["captured_at"] == identity["captured_at"]
    # The file list came from the ANSWER, not from a local constant: it is the
    # answer's list, in the answer's order.
    assert report["files"] == list(identity["files"])

    text_run = _run_verifier("--url", url, "--tree", str(REPO_ROOT))
    assert text_run.returncode == 0, (text_run.returncode, text_run.stdout, text_run.stderr)
    assert "MATCH" in text_run.stdout
    assert identity["digest"] in text_run.stdout
    assert identity["root"] in text_run.stdout
    assert identity["captured_at"] in text_run.stdout


def test_the_verifier_names_the_file_whose_bytes_differ(live_backend, tmp_path):
    """Arm two: the same answer against a tree where one covered file differs."""
    url, identity = live_backend
    copy = tmp_path / "candidate-checkout"
    for rel in identity["files"]:
        destination = copy / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, destination)
    assert MUTATED_FILE in identity["files"]

    target = copy / MUTATED_FILE
    original = target.read_bytes()
    changed = original + b"\n# a merge this serving process never imported\n"
    target.write_bytes(changed)
    assert hashlib.sha256(changed).hexdigest() != hashlib.sha256(original).hexdigest()

    run = _run_verifier("--url", url, "--tree", str(copy))
    assert run.returncode == 1, (run.returncode, run.stdout, run.stderr)

    # Both digests are printed, and the recomputed one is really the copy's.
    expected = compute_digest(copy, identity["files"])
    assert identity["digest"] != expected
    assert identity["digest"] in run.stdout
    assert expected in run.stdout
    assert "MISMATCH" in run.stdout

    # The differing file is NAMED, not merely counted.
    differences = [line for line in run.stdout.splitlines()
                   if line.strip().startswith("differing files")]
    assert differences, run.stdout
    assert MUTATED_FILE in differences[0]
    # The unmodified covered files are not blamed.
    assert "api/main.py" not in differences[0]
    assert "core/cf_access.py" not in differences[0]


def test_the_reported_digest_is_the_shared_implementations_value(live_backend):
    """One rule: this process and the serving process agree, over the same tree."""
    _url, identity = live_backend

    assert compute_digest(REPO_ROOT, identity["files"]) == identity["digest"]
    # And the covered list is the module's fixed list, in its fixed order.
    from core.source_identity import SOURCE_IDENTITY_MODULES
    assert list(identity["files"]) == list(SOURCE_IDENTITY_MODULES)


# The fingerprint of the documented rule: the inner per-file hex digest is fed as
# hex into the outer hasher. A second copy of the rule contains this expression.
_RULE_FINGERPRINT = "hexdigest().encode()"


def _tracked_python_sources(root: Path):
    """This repository's own Python sources: git's view, not the filesystem's.

    A filesystem walk also sees trees this repository does not track, and the
    DEPLOYED checkout carries them: ``.cache/apply-patch-dev/uv-cache`` holds
    vendored wheels whose third-party modules contain the same expression, so a
    walk reported seven holders of the rule and six of them were httpx and
    litellm. A guard whose verdict flips on a file the repository does not track
    is not a guard (measured 2026-11-18 on the deployed checkout; the clean
    worktree the change was written in had none of those directories, which is
    how the walk looked correct there).
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
    )
    assert listing.returncode == 0, (
        "git ls-files could not list the repository's sources, so this scan "
        "measured nothing", listing.returncode, listing.stderr[-500:])
    for name in listing.stdout.split("\0"):
        if not name:
            continue
        relative = Path(name)
        if relative.parts[0] in {"tests", "web"}:
            continue
        yield relative, (root / relative).read_text(encoding="utf-8",
                                                    errors="replace")


def test_the_digest_rule_is_implemented_exactly_once():
    """The loop lives in core/source_identity.py and nowhere else."""
    holders = [str(relative) for relative, text in _tracked_python_sources(REPO_ROOT)
               if _RULE_FINGERPRINT in text]
    assert holders == ["core/source_identity.py"]

    # api/main.py consumes it: no loop of its own, no NUL-separator update pair,
    # and the import is there.
    main_source = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert _RULE_FINGERPRINT not in main_source
    assert "b\"\\0\"" not in main_source
    assert ".update(" not in main_source
    assert "from core.source_identity import" in main_source
    assert "_capture_source_identity(" in main_source

    # The verifier imports the shared computation and never api.main: a checker
    # that loaded the app would only be proving something about itself. The
    # import graph is read from the AST (the module docstring legitimately NAMES
    # api.main while explaining why it is not imported), and then observed at run
    # time by importing the verifier in a fresh process.
    verifier_path = REPO_ROOT / "deploy" / "loaded_source.py"
    verifier_source = verifier_path.read_text(encoding="utf-8")
    assert "from core.source_identity import" in verifier_source
    imported = set()
    for node in ast.walk(ast.parse(verifier_source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported
                if name == "api" or name.startswith("api.")], imported
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, deploy.loaded_source;"
         "print('api.main' in sys.modules)"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False"


def test_the_scan_ignores_files_the_repository_does_not_track(tmp_path):
    """The guard must not move when an untracked tree appears under the checkout.

    Witnessed both ways in one fixture: a TRACKED module carrying the fingerprint
    is found, and the same bytes in an untracked file (``.cache/vendored/…``, the
    shape the deployed checkout actually has) are not. The rglob walk this
    replaced reports both, so the assertion separates the guard from a directory
    listing rather than comparing two listings.
    """
    repo = tmp_path / "repo"
    (repo / "instrument").mkdir(parents=True)
    (repo / "instrument" / "rule.py").write_text(
        "stolen = hashlib.sha256(b'')." + _RULE_FINGERPRINT + "\n",
        encoding="utf-8")
    (repo / "README.txt").write_text("not python\n", encoding="utf-8")
    identity = ["-c", "user.email=director@example.invalid",
                "-c", "user.name=director"]
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", *identity, "add", "instrument/rule.py", "README.txt"],
                   cwd=str(repo), check=True)
    untracked = repo / ".cache" / "vendored" / "third_party.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("same = hashlib.sha256(b'')." + _RULE_FINGERPRINT + "\n",
                         encoding="utf-8")

    tracked_holders = [str(relative) for relative, text in _tracked_python_sources(repo)
                       if _RULE_FINGERPRINT in text]
    assert tracked_holders == ["instrument/rule.py"], tracked_holders

    walk_holders = sorted(str(path.relative_to(repo)) for path in repo.rglob("*.py")
                          if _RULE_FINGERPRINT in path.read_text(encoding="utf-8"))
    assert walk_holders == [".cache/vendored/third_party.py",
                            "instrument/rule.py"], walk_holders
