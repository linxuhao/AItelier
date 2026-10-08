#!/usr/bin/env python3
"""Identity guard for the normal game gate; never invokes an engine itself."""
import argparse
import hashlib
import importlib
from importlib import metadata
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required regular identity file unavailable: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=*", "-C", str(root), *args],
        text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).strip()


def load_binding(path, root=None):
    digest(path)  # refuses missing files and symlinked manifests
    binding = json.loads(Path(path).read_text())
    if set(binding) != {"source", "head", "tree", "image", "files", "engine_sha256"}:
        raise ValueError("binding must contain only source/head/tree/image/files/engine_sha256")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", binding["image"]):
        raise ValueError("gate image must be an immutable image ID")
    if set(binding["files"]) != set(FILES):
        raise ValueError("binding must name the actual Python harness and reader")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value)
           for value in [*binding["files"].values(), binding["engine_sha256"]]):
        raise ValueError("invalid identity hash")
    source = Path(root or binding["source"])
    if not Path(binding["source"]).is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be an absolute regular directory")
    if (git(source, "rev-parse", "HEAD") != binding["head"]
            or git(source, "rev-parse", "HEAD^{tree}") != binding["tree"]
            or git(source, "status", "--porcelain")):
        raise ValueError("platform Source is not the frozen clean commit/tree")
    for relative, expected in binding["files"].items():
        if digest(source / relative) != expected:
            raise ValueError(f"platform Source identity differs: {relative}")
    return binding


def capture(binding, root="/app", fixture_root="/home/linxuhao/AItelier"):
    load_binding(os.environ["GATE_BINDING_FILE"], root)
    load_binding(os.environ["GATE_BINDING_FILE"], fixture_root)
    files = []
    # Import the modules run_tests actually reads, and attest their real paths.
    for name, relative in zip(("docker.godot.godot_harness", "aitelier.tools.godot_playtest.impl"), FILES):
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve()
        if actual != Path(root) / relative or digest(actual) != binding["files"][relative]:
            raise ValueError(f"normal Python import resolves to wrong Source: {actual}")
        for path in (actual, Path(fixture_root) / relative):
            files.append({"path": str(path), "sha256": digest(path)})
    sdk = metadata.version("skillflow-py")
    if sdk != "1.5.85":
        raise ValueError(f"normal gate SDK differs: {sdk}")
    with urllib.request.urlopen(os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080") + "/health", timeout=10) as response:
        health = json.load(response)
    engine = health["source_identity"]
    if (health.get("ok") is not True or engine.get("path") != "/srv/godot_harness.py"
            or engine.get("sha256") != binding["engine_sha256"]
            or engine.get("loaded_sha256") != binding["engine_sha256"]
            or type(engine.get("pid")) is not int or engine["pid"] < 1):
        raise ValueError("actual loaded engine identity missing or differs")
    return {"files": files, "engine": engine, "sdk": sdk,
            "binding_sha256": digest(os.environ["GATE_BINDING_FILE"]),
            "python": sys.executable, "pid": os.getpid(), "image": binding["image"]}


def run_bound(binding_path, output, root="/app", fixture_root="/home/linxuhao/AItelier"):
    receipt = {"raw_exit": None}
    try:
        binding = load_binding(binding_path, root)
        receipt["before"] = capture(binding, root, fixture_root)
        receipt["raw_exit"] = subprocess.run(["./run_tests.sh"]).returncode
        receipt["after"] = capture(binding, root, fixture_root)
        if receipt["before"] != receipt["after"]:
            raise ValueError("normal Python/engine identity changed during gate")
        receipt["identity"] = "unchanged"
        return receipt["raw_exit"]
    except Exception as exc:
        receipt.update(identity="poisoned", error=f"{type(exc).__name__}: {exc}")
        print(f"POISONED platform identity: {exc}; real rc was {receipt['raw_exit']}", file=sys.stderr)
        return 69
    finally:
        Path(output).write_text(json.dumps(receipt, indent=2) + "\n")


def sidecar_identity(output, before=None):
    value = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Id}} {{.Image}} {{.State.Pid}} {{.State.StartedAt}} {{.State.Status}}", "aitelier-godot"], text=True).strip().split()
    if len(value) != 5 or value[4] != "running" or not value[2].isdigit() or int(value[2]) < 1:
        raise ValueError("actual sidecar identity unavailable")
    result = dict(zip(("cid", "image", "pid", "started_at", "status"), value))
    Path(output).write_text(json.dumps(result, indent=2) + "\n")
    if before and json.loads(Path(before).read_text()) != result:
        raise ValueError("actual sidecar identity changed during normal gate")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("source", "image", "run", "sidecar"))
    parser.add_argument("binding")
    parser.add_argument("output", nargs="?")
    args = parser.parse_args()
    if args.mode == "sidecar":
        sidecar_identity(args.binding, args.output)
        return 0
    if args.mode == "run":
        return run_bound(args.binding, args.output)
    print(load_binding(args.binding)[args.mode])
    return 0


if __name__ == "__main__":
    sys.exit(main())
