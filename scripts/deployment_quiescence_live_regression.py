#!/usr/bin/env python3
"""Controlled live deployment-quiescence regression.

This is deliberately opt-in.  It starts one temporary Godot fixture request,
proves a second render admission is rejected, proves a restart is refused while
the owner is active, then performs one real restart after the owner settles and
records before/after container, health, sidecar, and optional MCP evidence.
Run only with ``--execute`` and ``AITELIER_LIVE_REGRESSION=1`` on the intended
deployment host.  No project or State data is used by the temporary fixture.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _url_json(url: str, payload: dict | None = None, timeout: int = 10) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {"error": str(exc)}
        return exc.code, body


def _containers() -> list[dict]:
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=aitelier", "--format",
         "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],
        capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker ps failed")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({"id": parts[0], "name": parts[1], "image": parts[2],
                         "status": parts[3]})
    return rows


def _fixture(path: Path) -> None:
    (path / "project.godot").write_text(
        "[application]\nrun/main_scene=\"res://main.tscn\"\n"
        "[display]\nwindow/size/viewport_width=320\nwindow/size/viewport_height=180\n",
        encoding="utf-8")
    (path / "main.tscn").write_text(
        "[gd_scene load_steps=2 format=3]\n\n"
        "[ext_resource path=\"res://main.gd\" type=\"Script\" id=\"1\"]\n\n"
        "[node name=\"Main\" type=\"Node\"]\nscript = ExtResource(\"1\")\n",
        encoding="utf-8")
    (path / "main.gd").write_text(
        "extends Node\nfunc _ready():\n    await get_tree().create_timer(0.1).timeout\n",
        encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--action", choices=("restart",), default="restart")
    parser.add_argument("--report", type=Path,
                        default=Path("deployment-quiescence-live-regression.json"))
    parser.add_argument("--builder-url", default=os.environ.get(
        "GODOT_BUILDER_URL", "http://localhost:8080"))
    parser.add_argument("--backend-url", default=os.environ.get(
        "AITELIER_URL", "http://localhost:4444"))
    args = parser.parse_args()
    if not args.execute or os.environ.get("AITELIER_LIVE_REGRESSION") != "1":
        print("refusing live action: use --execute with AITELIER_LIVE_REGRESSION=1",
              file=sys.stderr)
        return 2

    evidence: dict = {"started_at": time.time(), "action": args.action,
                      "builder_url": args.builder_url, "backend_url": args.backend_url,
                      "first_failure": None}
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=15,
                       check=True)
        evidence["containers_before"] = _containers()
        health_before = _url_json(args.backend_url + "/health")
        evidence["health_before"] = health_before
        with tempfile.TemporaryDirectory(prefix="aitelier-quiescence-live-") as raw:
            fixture = Path(raw)
            _fixture(fixture)
            payload = {"project_dir": str(fixture), "project_id": "live-fixture",
                       "run_id": "live-quiescence", "operation_id": "live-op-a",
                       "frames": 600}
            result: dict = {}

            def run_fixture() -> None:
                status, body = _url_json(args.builder_url + "/playtest", payload,
                                        timeout=900)
                result.update(status=status, body=body)

            worker = threading.Thread(target=run_fixture, daemon=True)
            worker.start()
            owner = {}
            for _ in range(120):
                status, body = _url_json(args.builder_url + "/lifecycle")
                evidence.setdefault("lifecycle_samples", []).append(body)
                active = [row for row in body.get("owners", [])
                          if row.get("status") == "active"]
                if active:
                    owner = active[-1]
                    break
                time.sleep(0.25)
            if not owner:
                raise RuntimeError("controlled fixture never acquired durable render owner")
            status, duplicate = _url_json(
                args.builder_url + "/playtest",
                {**payload, "operation_id": "live-op-b"}, timeout=10)
            evidence["duplicate_admission"] = {"status": status, "body": duplicate}
            if status != 409:
                raise RuntimeError(f"duplicate render admission was not rejected: {status}")

            from cli import server
            try:
                server.restart_server(max_wait=30)
            except Exception as exc:
                evidence["restart_while_active"] = {"refused": True, "error": str(exc)}
            else:
                raise RuntimeError("restart unexpectedly proceeded during active render")
            worker.join(timeout=930)
            if worker.is_alive():
                raise RuntimeError("controlled fixture did not settle")
            evidence["fixture_result"] = result
            status, settled = _url_json(args.builder_url + "/lifecycle")
            evidence["lifecycle_settled"] = settled
            if any(row.get("status") == "active" for row in settled.get("owners", [])):
                raise RuntimeError("render owner remained active after fixture completion")
            if owner and not any(row.get("owner_id") == owner["owner_id"]
                                 and row.get("status") == "released"
                                 for row in settled.get("owners", [])):
                raise RuntimeError("fixture owner did not settle as released")

        from cli import server
        server.restart_server(max_wait=120)
        evidence["health_after"] = _url_json(args.backend_url + "/health")
        evidence["projects_after"] = _url_json(args.backend_url + "/api/projects")
        mcp_url = os.environ.get("AITELIER_MCP_URL")
        if mcp_url:
            evidence["mcp_after"] = _url_json(mcp_url, {"jsonrpc": "2.0",
                "id": 1, "method": "initialize", "params": {}})
        evidence["containers_after"] = _containers()
        evidence["passed"] = True
    except Exception as exc:
        evidence["passed"] = False
        evidence["first_failure"] = {"type": type(exc).__name__, "message": str(exc)}
    evidence["finished_at"] = time.time()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "report": str(args.report),
                      "first_failure": evidence["first_failure"]}, indent=2))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
