#!/usr/bin/env python3
"""Identity observer for the normal game gate; never invokes an engine itself.

Owner ruling 2026-11-04 (wuxia ``iss-5e51a56608c64f60``). The frozen binding is a
RECORD OF REVIEW, not a pre-dispatch licence to refuse. This helper used to
refuse with exit 69 -- before anything was dispatched -- whenever one literal in
that record no longer matched the bytes on disk. One platform rebuild
(2026-10-24 -> 11-03, ``198ded9bcd`` changing the engine) therefore stopped every
project's every gate in seconds, and collapsed "did not measure" and "measured
and failed" into the same outcome. So a stale literal is now DATA:

  * the identity actually in front of the gate is OBSERVED and recorded, and the
    fields where it differs from the reviewed binding are written into the
    receipt (`platform`) and the exit file;
  * a RED verdict is returned as the gate's own exit code whatever the identity:
    "the code is broken" is a property of the code, not of who measured it;
  * a GREEN verdict carries `platform_unreviewed` when the identity is not the
    reviewed binding: it is a measurement, not release evidence;
  * an explicit binding (or its sha256) can still be passed in to judge a run
    against a historical review -- that is now a choice, not the default path.

What stays a refusal is everything that is NOT a stale literal, because those
guards decide whether there is a measurement at all: a manifest whose bytes are
unreadable/malformed or changed after dispatch, an unobservable platform
identity, and an identity that moved while the gate ran (`run_bound`).

Mode `platform` answers a question about a receipt that was already written, so
"this receipt is not `reviewed`" and "there is no receipt to read" are two
different facts. The first is a verdict on stdout (`reviewed`, `platform_unreviewed
…`, `unknown <why>`) with exit 0. The second is not a verdict at all: the receipt
or the binding bytes named by GATE_BINDING_FILE cannot be read as the records they
were named to be, so the process exits 3 and prints ONE line on stderr naming the
path and the failure kind, with nothing on stdout. Until 2026-11-08 both ended in
the same answer string -- `unknown platform record unavailable` -- which the
launcher's own `||` fallback (`gate_run.sh:139`) spelled too, so a reader could
not tell "the tool answered unknown" from "the tool could not be asked". The two
fallbacks in `tools/gate_run.sh` now spell a value this tool never prints
(`unanswered:receipt-tool-failed`, `unanswered:platform-tool-failed`), and
`tests/unit/test_gate_binding_modes_and_usage.py` reads those literals out of the
launcher's bytes and fails if any answer below equals one.
"""
import argparse
import hashlib
import importlib
from importlib import metadata
import json
import os
import re
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")
# The two readonly coordinates the Source is mounted at inside the gate container.
SOURCES = ("root", "fixture")
REVIEWED = "reviewed"
UNREVIEWED = "unreviewed"
UNKNOWN = "unknown"


class IdentityMismatch(ValueError):
    """An identity difference was observed, rather than an observation lost."""


def read_regular(path):
    # Check the opened descriptor, not a path that can change before open/read.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"required regular identity file unavailable: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"required regular identity file unavailable: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    except OSError as exc:
        raise ValueError(f"required regular identity file unavailable: {path}") from exc
    finally:
        os.close(fd)


def digest(path):
    return hashlib.sha256(read_regular(path)).hexdigest()


def digest_or_none(path):
    """The digest of a file that must be measured but may have moved or vanished."""
    try:
        return digest(path)
    except (OSError, ValueError):
        return None


def git(root, *args):
    # Host shells carry GIT_CONFIG_COUNT/GIT_CONFIG_KEY_i/GIT_CONFIG_VALUE_i for
    # the git credential helper. A half-exported set makes EVERY git call fail
    # ("missing config key GIT_CONFIG_KEY_0") -- and observing the platform
    # identity must not depend on an unrelated injected config. These commands
    # are read-only (rev-parse/status), so the injected config is dropped.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_CONFIG_")}
    return subprocess.check_output(
        ["git", "-c", "safe.directory=*", "-C", str(root), *args],
        text=True, env={**env, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_COUNT": "0"}).strip()


def read_binding(path, expected_sha256=None):
    return binding_record(read_regular(path), expected_sha256)


def binding_record(raw, expected_sha256=None):
    """Validate a frozen binding from bytes already read.

    Split out so a caller that must first prove the file is readable (and report
    "could not read it" separately from "it is not the reviewed record") reads
    the file once instead of twice.
    """
    sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise IdentityMismatch("frozen binding bytes differ")
    binding = json.loads(raw)
    if (type(binding) is not dict
            or set(binding) != {"source", "head", "tree", "image", "sdk", "files", "engine_sha256"}):
        raise ValueError("binding must contain only source/head/tree/image/sdk/files/engine_sha256")
    if (not isinstance(binding["sdk"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", binding["sdk"])):
        raise ValueError("binding sdk must be an explicit exact X.Y.Z release")
    if (not isinstance(binding["image"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding["image"])):
        raise ValueError("gate image must be an immutable image ID")
    if type(binding["files"]) is not dict or set(binding["files"]) != set(FILES):
        raise ValueError("binding must name the actual Python harness and reader")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in [*binding["files"].values(), binding["engine_sha256"]]):
        raise ValueError("invalid identity hash")
    return binding, sha256


def observe_root(root):
    """What one mounted Source IS, right now.

    Only the ability to LOOK is refused (`ValueError`): a coordinate that is not
    an absolute regular directory cannot be observed at all. A Source that is not
    the reviewed clean checkout, a moved commit or a changed/vanished file is an
    OBSERVATION (`None`/value), never a refusal -- `compare_source` reports it as
    data and the caller decides what the verdict is worth.
    """
    source = Path(root)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError(f"Source {root} must be an absolute regular directory")
    observed = {"head": None, "tree": None, "dirty": None, "files": {}}
    try:
        observed["head"] = git(source, "rev-parse", "HEAD")
        observed["tree"] = git(source, "rev-parse", "HEAD^{tree}")
        observed["dirty"] = len(git(source, "status", "--porcelain").splitlines())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"unobserved Source Git identity at {root}: {exc}", file=sys.stderr)
    for relative in FILES:
        observed["files"][relative] = digest_or_none(source / relative)
    return observed


def compare_source(binding, name, source):
    """Every field where one observed Source is NOT the reviewed binding."""
    differences = []

    def record(field, bound, observed):
        differences.append({"field": f"{name}.{field}", "bound": bound, "observed": observed})

    if type(source) is not dict:
        record("identity", "observable", None)
        return differences
    for field in ("head", "tree"):
        if source.get(field) != binding.get(field):
            record(field, binding.get(field), source.get(field))
    if source.get("dirty") != 0:
        record("dirty", 0, source.get("dirty"))
    for relative, bound in (binding.get("files") or {}).items():
        observed = (source.get("files") or {}).get(relative)
        if observed != bound:
            record(f"files[{relative}]", bound, observed)
    return differences


def compare_to_binding(binding, observation):
    """Every field where the platform in front of the gate is not the reviewed one."""
    sources = (observation or {}).get("source") or {}
    differences = []
    for name in SOURCES:
        differences.extend(compare_source(binding, name, sources.get(name)))
    engine = (observation or {}).get("engine") or {}
    for field in ("path", "sha256", "loaded_sha256"):
        bound = "/srv/godot_harness.py" if field == "path" else binding.get("engine_sha256")
        if engine.get(field) != bound:
            differences.append({"field": f"engine.{field}", "bound": bound,
                                "observed": engine.get(field)})
    if (observation or {}).get("sdk") != binding.get("sdk"):
        differences.append({"field": "sdk", "bound": binding.get("sdk"),
                            "observed": (observation or {}).get("sdk")})
    return differences


def observe_source(binding):
    """The bound Source as the launcher sees it before dispatch (host side)."""
    try:
        return {"root": observe_root(binding["source"])}
    except (OSError, ValueError) as exc:
        print(f"unobserved bound Source {binding['source']}: {exc}", file=sys.stderr)
        return {"root": {"head": None, "tree": None, "dirty": None,
                         "files": {relative: None for relative in FILES}}}


def record_observation(binding, binding_sha256, observation, frozen_sha256=None):
    """The one place that says what a set of observations is WORTH.

    `reviewed` requires a complete observation that is byte-for-byte the reviewed
    binding. Anything else is `unreviewed`, with the differing fields kept as
    data. It is never an error: the gate has already run, or is about to.
    """
    differences = compare_to_binding(binding, observation)
    return {"binding_sha256": binding_sha256,
            "frozen_sha256": frozen_sha256,
            "status": UNREVIEWED if differences else REVIEWED,
            "differences": differences}


def validate_source(binding, root=None):
    """Strict predicate: is this exactly the frozen clean Source? (a review check)"""
    source = Path(root or binding["source"])
    if not Path(binding["source"]).is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be an absolute regular directory")
    if (git(source, "rev-parse", "HEAD") != binding["head"]
            or git(source, "rev-parse", "HEAD^{tree}") != binding["tree"]
            or git(source, "status", "--porcelain")):
        raise IdentityMismatch("platform Source is not the frozen clean commit/tree")
    for relative, expected in binding["files"].items():
        if digest(source / relative) != expected:
            raise IdentityMismatch(f"platform Source identity differs: {relative}")


def load_binding(path, root=None):
    binding, _ = read_binding(path)
    validate_source(binding, root)
    return binding


def capture(binding, binding_sha256, root="/app", fixture_root="/home/linxuhao/AItelier"):
    # The caller supplies its frozen expectation; observation cannot replace it.
    manifest, observed_sha256 = read_binding(os.environ["GATE_BINDING_FILE"], binding_sha256)
    if manifest != binding:
        raise IdentityMismatch("manifest differs from caller's frozen binding")
    sources = {name: observe_root(path) for name, path in zip(SOURCES, (root, fixture_root))}
    files = []
    # Import the modules run_tests actually reads, and attest their real paths.
    # Where an import resolves is evidence; a difference from the reviewed
    # binding is recorded by `compare_to_binding`, not raised here.
    for name, relative in zip(("docker.godot.godot_harness", "aitelier.tools.godot_playtest.impl"), FILES):
        try:
            actual = Path(importlib.import_module(name).__file__).resolve()
        except Exception as exc:  # an unimportable harness is an observation, not a verdict
            print(f"unobserved import {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            actual = Path(root) / relative
        fixture_file = Path(fixture_root) / relative
        files.append({"path": str(actual), "sha256": digest_or_none(actual)})
        files.append({"path": str(fixture_file), "sha256": digest_or_none(fixture_file)})
    sdk = metadata.version("skillflow-py")
    with urllib.request.urlopen(os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080") + "/health", timeout=10) as response:
        health = json.load(response)
    engine = health["source_identity"]
    if (health.get("ok") is not True or not isinstance(engine, dict)
            or not isinstance(engine.get("path"), str) or not engine["path"]
            or any(not isinstance(engine.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", engine[key])
                   for key in ("sha256", "loaded_sha256"))
            or type(engine.get("pid")) is not int or engine["pid"] < 1):
        raise ValueError("actual loaded engine identity unavailable")
    return {"files": files, "engine": engine, "sdk": sdk,
            "binding_sha256": observed_sha256,
            "python": sys.executable, "pid": os.getpid(), "image": binding["image"],
            "source": sources}


def run_bound(binding_path, output, root="/app", fixture_root="/home/linxuhao/AItelier",
              expected_sha256=None):
    """Run the gate's stage and record what platform actually ran it.

    The stage ALWAYS runs once the identity can be observed: a stale literal in
    the binding changes what the verdict is WORTH (`platform`), never whether the
    measurement happens. An unobservable identity (or an identity that moves
    during the gate) is still unknown/mismatch -- those are the guards that say
    whether there is a measurement at all, not review-literal checks.
    """
    receipt = {"raw_exit": None, "identity": UNKNOWN}
    try:
        report = Path(output).open("x")
    except OSError as exc:
        print(f"UNKNOWN platform identity: receipt unavailable ({exc})", file=sys.stderr)
        return 69
    try:
        # The manifest bytes are the caller's frozen expectation, not a licence:
        # if they changed after dispatch nothing below can be attributed to them.
        binding, binding_sha256 = read_binding(binding_path, expected_sha256)
        try:
            receipt["before"] = capture(binding, binding_sha256, root, fixture_root)
        finally:
            # Even when the identity cannot be observed at all, the record says
            # which fields were compared -- and it is never a reason to skip the
            # stage below unless there is nothing to attribute it to.
            receipt["platform"] = record_observation(
                binding, binding_sha256, receipt.get("before") or {}, expected_sha256)
        receipt["raw_exit"] = subprocess.run(["./run_tests.sh"]).returncode
        receipt["after"] = capture(binding, binding_sha256, root, fixture_root)
        if receipt["before"] != receipt["after"]:
            raise IdentityMismatch("normal Python/engine identity changed during gate")
        receipt["identity"] = "unchanged"
        return receipt["raw_exit"]
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, IdentityMismatch):
            receipt["identity"] = "mismatch"
        print(f"{receipt['identity'].upper()} platform identity: {exc}; real rc was {receipt['raw_exit']}", file=sys.stderr)
        return 69
    finally:
        with report:
            report.write(json.dumps(receipt, indent=2) + "\n")


def _complete_source(source):
    def complete_root(root):
        return (type(root) is dict and set(root) == {"head", "tree", "dirty", "files"}
                and isinstance(root["head"], str) and re.fullmatch(r"[0-9a-f]{40}", root["head"])
                and isinstance(root["tree"], str) and re.fullmatch(r"[0-9a-f]{40}", root["tree"])
                and type(root["dirty"]) is int
                and type(root["files"]) is dict and set(root["files"]) == set(FILES)
                and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                        for value in root["files"].values()))

    return (type(source) is dict and set(source) == set(SOURCES)
            and all(complete_root(source[name]) for name in SOURCES))


def receipt_identity(path, raw_exit, binding_path, expected_sha256):
    """Did this receipt settle, against the bytes frozen before dispatch?

    This answers SETTLEMENT ("the instrument was observed, did not move, and the
    stage exit is the one it recorded"), never REVIEW ("this is the platform a
    human reviewed"). The two questions used to be one: requiring the observed
    engine/SDK to EQUAL the bound literal here made every stale binding an
    unmeasurable platform, which is the outage the 2026-11-04 ruling removed.
    Review is decided by `platform_status`, from the same receipt.
    """
    try:
        receipt = json.loads(read_regular(path))
        if (receipt["identity"] == "mismatch"
                and str(receipt.get("error", "")).startswith("IdentityMismatch: ")):
            return "mismatch"
        if (not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            return "unknown"
        # Read only against the hash frozen before dispatch. A newly observed
        # manifest, SDK or image must never become the authorization baseline.
        binding, _ = read_binding(binding_path, expected_sha256)
        before, after = receipt["before"], receipt["after"]
        engine = before["engine"]
        complete = (
            set(before) == {"files", "engine", "sdk", "binding_sha256", "python", "pid",
                            "image", "source"}
            and _complete_source(before["source"])
            and type(before["files"]) is list and len(before["files"]) == 4
            and all(type(f) is dict and set(f) == {"path", "sha256"}
                    and Path(f["path"]).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
                    for f in before["files"])
            and isinstance(before["sdk"], str)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", before["sdk"])
            and Path(before["python"]).is_absolute()
            and type(before["pid"]) is int and before["pid"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", before["binding_sha256"])
            and before["binding_sha256"] == expected_sha256
            and re.fullmatch(r"sha256:[0-9a-f]{64}", before["image"])
            and before["image"] == binding["image"]
            and engine["path"] == "/srv/godot_harness.py"
            and re.fullmatch(r"[0-9a-f]{64}", engine["sha256"])
            and engine["loaded_sha256"] == engine["sha256"]
            and type(engine["pid"]) is int and engine["pid"] > 0)
        if (complete and receipt["identity"] == "unchanged" and before == after
                and type(receipt["raw_exit"]) is int
                # Timeout/launch-error/signal exit conventions do not prove settlement.
                and 0 <= receipt["raw_exit"] < 124
                and receipt["raw_exit"] == int(raw_exit)):
            return "unchanged"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return "unknown"


def _short(value):
    if value is None:
        return "unobserved"
    text = str(value)
    return text if len(text) <= 16 else text[:12]


class Unanswerable(Exception):
    """The question could not be put: an input is not the record it was named to be.

    A verdict (`unknown <why>`) describes a receipt that WAS read. This describes
    a receipt or binding that could not be read at all, so it is reported through
    the exit status and one stderr line instead of as a word on stdout. Both used
    to be the same string, which is how "the tool answered" and "the tool could
    not be asked" became indistinguishable in the gate's own record.
    """

    def __init__(self, path, kind, detail=""):
        self.path = str(path)
        self.kind = kind
        self.detail = detail
        super().__init__(self.line())

    def line(self):
        text = f"platform identity cannot be answered: {self.kind}: {self.path}"
        return f"{text}: {self.detail}" if self.detail else text


def _unreadable_kind(path):
    """WHY a path could not be read as a regular identity file, in the same words
    a reader of the log needs: absent, not a regular file (directory or symlink),
    or present and unreadable."""
    try:
        info = os.lstat(str(path))
    except OSError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "not-a-regular-file (symlink)"
    if stat.S_ISDIR(info.st_mode):
        return "not-a-regular-file (directory)"
    if not stat.S_ISREG(info.st_mode):
        return "not-a-regular-file"
    return "unreadable"


def _regular_input(path, what):
    try:
        return read_regular(path)
    except (OSError, ValueError) as exc:
        raise Unanswerable(path, _unreadable_kind(path), f"{what}: {exc}") from exc


def _receipt_record(path):
    raw = _regular_input(path, "receipt")
    try:
        receipt = json.loads(raw)
    except ValueError as exc:
        raise Unanswerable(path, "not-json", f"receipt: {exc}") from exc
    if type(receipt) is not dict:
        raise Unanswerable(path, "not-a-receipt",
                           f"receipt: a receipt is a JSON object, this is {type(receipt).__name__}")
    return receipt


def _binding_from_file(binding_path, expected_sha256):
    """The reviewed binding's record, or `(None, None)` when the bytes are readable
    but are NOT the record we were told to judge against (a verdict, not an
    unreadable input)."""
    if not binding_path:
        raise Unanswerable("GATE_BINDING_FILE", "unset",
                           "binding: mode platform reads the reviewed binding from this variable")
    raw = _regular_input(binding_path, "binding")
    try:
        return binding_record(raw, expected_sha256)
    except IdentityMismatch:
        return None, None
    except (ValueError, TypeError, KeyError) as exc:
        raise Unanswerable(binding_path, "not-a-binding-record", f"binding: {exc}") from exc


def platform_status(path, binding_path, expected_sha256):
    """Say what the receipt's identity is WORTH, as one line starting with the word.

    `reviewed` -- the identity that ran the gate is the reviewed binding: a green
    is release evidence. `unreviewed <differences>` -- a measurement, not release
    evidence. `unknown <why>` -- the receipt cannot say which platform measured.

    The verdict inside the receipt is DATA and is re-derived here from the
    receipt's own observations against the same manifest bytes the launcher
    froze; a receipt whose conclusion disagrees with its own evidence is
    `unknown`, never `reviewed`.

    Every verdict above is about bytes that WERE read. When the receipt or the
    binding cannot be read at all -- missing, not a regular file (a directory or a
    symlink), unreadable, not JSON, not a receipt object, or GATE_BINDING_FILE
    unset -- this raises `Unanswerable` instead of returning a word: "there is no
    receipt to judge" is not an answer about a platform, and a caller that reads
    it as one records a measurement that never happened.
    """
    receipt = _receipt_record(path)
    # The binding is read BEFORE settlement is judged: without the reviewed record
    # there is nothing to judge against, and reporting that as "this receipt cannot
    # say which platform measured" blames the receipt for a missing input.
    binding, binding_sha256 = _binding_from_file(binding_path, expected_sha256)
    if binding is None:
        return "unknown terminal receipt cannot say which platform measured"
    if receipt_identity(path, receipt.get("raw_exit"), binding_path, expected_sha256) != "unchanged":
        return "unknown terminal receipt cannot say which platform measured"
    record = receipt.get("platform")
    if (type(record) is not dict
            or set(record) != {"binding_sha256", "frozen_sha256", "status", "differences"}
            or record["binding_sha256"] != binding_sha256
            or record["frozen_sha256"] != expected_sha256
            or record["status"] not in (REVIEWED, UNREVIEWED)):
        return "unknown receipt carries no usable platform record"
    differences = compare_to_binding(binding, receipt["before"])
    status = UNREVIEWED if differences else REVIEWED
    if record["status"] != status or record["differences"] != differences:
        return "unknown receipt platform record disagrees with its own observation"
    if status == REVIEWED:
        return REVIEWED
    shown = " ".join(f"{item['field']}:{_short(item['bound'])}->{_short(item['observed'])}"
                     for item in differences[:6])
    more = f" (+{len(differences) - 6} more)" if len(differences) > 6 else ""
    return f"{UNREVIEWED} binding={binding_sha256[:12]} differs={len(differences)} {shown}{more}"


def sidecar_identity(output, before=None):
    def valid(value):
        return (len(value) == 5 and all(isinstance(v, str) for v in value)
                and re.fullmatch(r"[0-9a-f]{64}", value[0])
                and re.fullmatch(r"sha256:[0-9a-f]{64}", value[1])
                and value[4] == "running" and value[2].isdigit() and int(value[2]) > 0)

    value = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Id}} {{.Image}} {{.State.Pid}} {{.State.StartedAt}} {{.State.Status}}", "aitelier-godot"], text=True).strip().split()
    if not valid(value):
        raise ValueError("actual sidecar identity unavailable")
    result = dict(zip(("cid", "image", "pid", "started_at", "status"), value))
    with Path(output).open("x") as report:
        report.write(json.dumps(result, indent=2) + "\n")
    if before:
        previous = json.loads(Path(before).read_text())
        if (type(previous) is not dict or set(previous) != set(result)
                or not valid([previous[k] for k in result])):
            raise ValueError("prior sidecar identity unavailable")
        if previous != result:
            raise IdentityMismatch("actual sidecar identity changed during normal gate")


def write_observation(binding_path, output, expected_sha256):
    """Record, before dispatch, which fields of the reviewed binding already differ.

    The launcher refuses nothing here: this is the dispatch-time half of the
    record (the receipt carries the in-container half), and an unreadable bound
    Source is recorded as unobserved rather than raised.
    """
    binding, binding_sha256 = read_binding(binding_path, expected_sha256)
    observation = observe_source(binding)
    differences = compare_source(binding, "root", observation["root"])
    record = {"binding_sha256": binding_sha256,
              # Only the mounted Source is visible here; the SDK and the loaded
              # engine are observed inside the gate container, so this record
              # never claims the whole binding was reviewed -- the receipt does.
              "dispatch_status": "source_differs" if differences else "source_matches",
              "covers": ["root"],
              "bound": {key: binding[key] for key in ("source", "head", "tree", "image",
                                                      "sdk", "engine_sha256")}
              | {"files": binding["files"]},
              "observed": observation,
              "differences": differences}
    with Path(output).open("x") as report:
        report.write(json.dumps(record, indent=2) + "\n")
    if differences:
        print(f"platform_unreviewed: {len(differences)} reviewed Source field(s) differ "
              f"({', '.join(item['field'] for item in differences)}); running anyway",
              file=sys.stderr)
    return binding_sha256


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
The positional arguments mean different things per mode. argparse can only name
a positional once, so this list is the authority:
  source, image, sha256
               the positional is the binding (the reviewed record itself).
  run          the positional is the binding; the second is the receipt to write.
  sidecar      the positional is the sidecar record to WRITE; the second is the
               earlier record it is compared against.
  receipt      the positional is the gate's own receipt; the second is the
               stage's recorded raw exit.
  platform     the positional is the gate's own receipt -- the file
               tools/gate_run.sh wrote as $REPORTS/<suffix>.platform-identity.json.
               The reviewed binding is read from GATE_BINDING_FILE (frozen by the
               launcher before dispatch), never from the positional.
""")
    parser.add_argument("mode", choices=("source", "image", "sha256", "run", "sidecar",
                                         "receipt", "platform"))
    parser.add_argument("binding", metavar="RECEIPT_OR_BINDING")
    parser.add_argument("output", nargs="?", metavar="OUTPUT_OR_RAW_EXIT")
    parser.add_argument("--binding-sha256")
    parser.add_argument("--observe")
    args = parser.parse_args()
    if args.mode == "receipt":
        print(receipt_identity(args.binding, args.output,
                               os.environ.get("GATE_BINDING_FILE"), args.binding_sha256))
        return 0
    if args.mode == "platform":
        try:
            print(platform_status(args.binding, os.environ.get("GATE_BINDING_FILE"),
                                  args.binding_sha256))
        except Unanswerable as exc:
            # Exit 3, not 0: "the identity tool could not be asked" must be a
            # different fact from the answer `unknown …`, in the exit status and
            # on stderr, without a verdict token on stdout. 2 stays argparse.
            print(exc.line(), file=sys.stderr)
            return 3
        return 0
    if args.mode == "sidecar":
        try:
            sidecar_identity(args.binding, args.output)
        except IdentityMismatch as exc:
            print(f"MISMATCH sidecar identity: {exc}", file=sys.stderr)
            return 70
        return 0
    if args.mode == "run":
        if not args.binding_sha256:
            parser.error("run requires the binding hash frozen before dispatch")
        return run_bound(args.binding, args.output, expected_sha256=args.binding_sha256)
    if args.observe:
        if args.mode != "sha256":
            parser.error("--observe belongs to the sha256 read that precedes dispatch")
        print(write_observation(args.binding, args.observe, args.binding_sha256))
        return 0
    # Dispatch coordinates only. The review literals are compared by `--observe`
    # and in the gate container, never here: a stale literal must not be able to
    # stop the gate from being dispatched, and inventing a Source or an image
    # from what happens to be on disk would measure something nobody reviewed.
    binding, sha256 = read_binding(args.binding, args.binding_sha256)
    if args.mode == "source":
        # What dispatch needs is a directory to mount. That is a readability
        # check, not an identity check: a stale literal is data (see --observe).
        source = Path(binding["source"])
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            print(f"the bound Source is not a mountable directory: {binding['source']}",
                  file=sys.stderr)
            return 69
    print(sha256 if args.mode == "sha256" else binding[args.mode])
    return 0


if __name__ == "__main__":
    sys.exit(main())
