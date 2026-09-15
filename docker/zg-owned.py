#!/usr/bin/env python3
"""Owned public zg entry point; raw package binary is an internal implementation."""
import os
import sys

from core.resource_ownership import run_command

if __name__ == "__main__":
    resource = "semantic-service" if sys.argv[1:3] == ["server", "run"] else "semantic"
    raise SystemExit(run_command(resource, [os.environ.get("AITELIER_ZG_EXECUTABLE", "/usr/local/bin/zg-real"), *sys.argv[1:]]))
