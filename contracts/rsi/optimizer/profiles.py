"""The ONE predeclared benchmark profile selection, shared by fake and native.

``fixtures/native_profiles.json`` declares each profile's per-case max_points and,
for the boundary scenario, the holdout answer script per arm and repetition.
Both the standalone fake and the native fixture select a scenario's profile from
its ``scenario_profiles`` map before prepare, freeze or any effect.

``rubric_points`` is the trusted checker's declared rubric (core/rsi_checker.py:
value = (max_points*6)//10, citation = the remainder) applied to the answer
script's levels; the fake uses it to emit the raw scores the script implies, and
the native differential checks it against the actual checker outputs.
"""
from __future__ import annotations

import json
from pathlib import Path

PROFILES = json.loads((Path(__file__).with_name("fixtures") / "native_profiles.json").read_text())


def for_scenario(name: str) -> str:
    return PROFILES["scenario_profiles"].get(name, "ordinary")


def spec(profile: str) -> dict:
    return PROFILES["profiles"][profile]


def rubric_points(level: str, max_points: int) -> int:
    value = (max_points * 6) // 10
    return {"full": max_points, "value": value, "cited_wrong": max_points - value}[level]
