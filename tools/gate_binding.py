#!/usr/bin/env python3
"""Identity guard for the normal game gate; never invokes an engine itself."""
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


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=*", "-C", str(root), *args],
        text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).strip()


def read_binding(path, expected_sha256=None):
    raw = read_regular(path)
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


def validate_source(binding, root=None):
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
    observed, observed_sha256 = read_binding(os.environ["GATE_BINDING_FILE"], binding_sha256)
    if observed != binding:
        raise IdentityMismatch("manifest differs from caller's frozen binding")
    validate_source(binding, root)
    validate_source(binding, fixture_root)
    files = []
    # Import the modules run_tests actually reads, and attest their real paths.
    for name, relative in zip(("docker.godot.godot_harness", "aitelier.tools.godot_playtest.impl"), FILES):
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve()
        if actual != Path(root) / relative or digest(actual) != binding["files"][relative]:
            raise IdentityMismatch(f"normal Python import resolves to wrong Source: {actual}")
        for path in (actual, Path(fixture_root) / relative):
            files.append({"path": str(path), "sha256": digest(path)})
    sdk = metadata.version("skillflow-py")
    if sdk != binding["sdk"]:
        raise IdentityMismatch(f"normal gate SDK differs: expected {binding['sdk']}, observed {sdk}")
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
    if (engine["path"] != "/srv/godot_harness.py"
            or engine["sha256"] != binding["engine_sha256"]
            or engine["loaded_sha256"] != binding["engine_sha256"]):
        raise IdentityMismatch("actual loaded engine identity differs")
    return {"files": files, "engine": engine, "sdk": sdk,
            "binding_sha256": observed_sha256,
            "python": sys.executable, "pid": os.getpid(), "image": binding["image"]}


def run_bound(binding_path, output, root="/app", fixture_root="/home/linxuhao/AItelier",
              expected_sha256=None):
    receipt = {"raw_exit": None, "identity": "unknown"}
    try:
        report = Path(output).open("x")
    except OSError as exc:
        print(f"UNKNOWN platform identity: receipt unavailable ({exc})", file=sys.stderr)
        return 69
    try:
        binding, binding_sha256 = read_binding(binding_path, expected_sha256)
        receipt["before"] = capture(binding, binding_sha256, root, fixture_root)
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


def receipt_identity(path, raw_exit, binding_path, expected_sha256):
    """Only a complete, consistent terminal receipt can authorize reclamation."""
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
            set(before) == {"files", "engine", "sdk", "binding_sha256", "python", "pid", "image"}
            and type(before["files"]) is list and len(before["files"]) == 4
            and all(type(f) is dict and set(f) == {"path", "sha256"}
                    and Path(f["path"]).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
                    for f in before["files"])
            and before["sdk"] == binding["sdk"] and Path(before["python"]).is_absolute()
            and type(before["pid"]) is int and before["pid"] > 0
            and re.fullmatch(r"[0-9a-f]{64}", before["binding_sha256"])
            and before["binding_sha256"] == expected_sha256
            and re.fullmatch(r"sha256:[0-9a-f]{64}", before["image"])
            and before["image"] == binding["image"]
            and engine["path"] == "/srv/godot_harness.py"
            and re.fullmatch(r"[0-9a-f]{64}", engine["sha256"])
            and engine["sha256"] == binding["engine_sha256"]
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("source", "image", "sha256", "run", "sidecar", "receipt"))
    parser.add_argument("binding")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--binding-sha256")
    args = parser.parse_args()
    if args.mode == "receipt":
        print(receipt_identity(args.binding, args.output,
                               os.environ.get("GATE_BINDING_FILE"), args.binding_sha256))
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
    binding, sha256 = read_binding(args.binding, args.binding_sha256)
    validate_source(binding)
    print(sha256 if args.mode == "sha256" else binding[args.mode])
    return 0


if __name__ == "__main__":
    sys.exit(main())
