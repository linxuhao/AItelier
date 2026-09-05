#!/usr/bin/env python3
"""Call the running AItelier server: mcp.py TOOL [JSON | @JSON_FILE].

All execution and authorization stay in the server. This production helper
requires human checkpoints for run_pipeline and never starts the service.
"""
import argparse
import json
import os
from pathlib import Path
import sys

import httpx
from dotenv import dotenv_values

_ENDPOINT = "http://127.0.0.1:4444/mcp/"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool")
    parser.add_argument("arguments", nargs="?", default="{}")
    options = parser.parse_args(argv)
    try:
        raw = options.arguments
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        if options.tool == "run_pipeline":
            if arguments.get("checkpoints", "ask") != "ask":
                raise ValueError("run_pipeline requires checkpoints=ask")
            arguments["checkpoints"] = "ask"
    except (OSError, ValueError) as exc:
        # Do not echo the supplied brief or JSON contents in a parse error.
        print(f"Invalid arguments ({type(exc).__name__}); use a JSON object; "
              "run_pipeline requires checkpoints=ask.", file=sys.stderr)
        return 2

    token = os.environ.get("AITELIER_ADMIN_TOKEN")
    if not token:
        token = dotenv_values(Path.home() / "AItelier" / ".env").get("AITELIER_ADMIN_TOKEN")
    if not token:
        print("AITELIER_ADMIN_TOKEN is missing from environment and ~/AItelier/.env.",
              file=sys.stderr)
        return 2
    try:
        response = httpx.post(
            _ENDPOINT,
            headers={"X-AItelier-Admin-Token": token,
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": options.tool, "arguments": arguments}},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        print(f"AItelier MCP rejected the request (HTTP {exc.response.status_code}).",
              file=sys.stderr)
        return 1
    except httpx.RequestError:
        print("AItelier MCP service unavailable or request timed out; service was not started. "
              "After a timeout, inspect run state before retrying a write.", file=sys.stderr)
        return 1
    except ValueError:
        print("AItelier MCP returned invalid JSON.", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload.get("error") or payload.get("result", {}).get("isError") else 0


if __name__ == "__main__":
    raise SystemExit(main())
