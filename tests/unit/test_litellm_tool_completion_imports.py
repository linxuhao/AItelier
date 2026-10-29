"""A completion that passes `tools` must not die on an import inside litellm.

The 2026-10-28 image rebuild floated litellm from 1.105.1 to 1.106.5. That release
imports ``mcp.types.CacheableResult`` as soon as ``litellm.completion`` is given
``tools``, and the mcp this project allows (<2, 1.30.0) does not define it, so every
tool-calling pipeline step failed with ImportError before any provider was called.
No existing test sent ``tools`` through the real litellm entry point, so the whole
suite stayed green. ``mock_response`` keeps this offline and deterministic. The async
twin covers the entry point the butler uses (``core/meta_agent.py`` -> ``acompletion``).
"""
import asyncio
import re
import tomllib
from importlib import metadata
from pathlib import Path

import litellm
from packaging.requirements import Requirement
from packaging.version import Version

TOOLS = [{"type": "function", "function": {
    "name": "read", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
MESSAGES = [{"role": "user", "content": "hi"}]
MODEL = "anthropic/claude-sonnet-5-5"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_completion_with_tools_runs_through_the_real_litellm_entry_point():
    reply = litellm.completion(model=MODEL, messages=MESSAGES, tools=TOOLS, mock_response="ok")
    assert reply.choices[0].message.content == "ok"


def test_async_completion_with_tools_runs_through_the_real_litellm_entry_point():
    reply = asyncio.run(litellm.acompletion(model=MODEL, messages=MESSAGES, tools=TOOLS,
                                            mock_response="ok"))
    assert reply.choices[0].message.content == "ok"


def test_the_installed_litellm_satisfies_the_bound_declared_in_pyproject():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    requirement = next(Requirement(line) for line in declared
                       if re.match(r"litellm\b", line.strip()))
    assert requirement.specifier, "pyproject must keep a bounded litellm requirement"
    assert "<" in str(requirement.specifier), "litellm needs an upper bound (see module docstring)"
    installed = Version(metadata.version("litellm"))
    assert installed in requirement.specifier, (
        f"installed litellm {installed} is outside the declared {requirement.specifier}")
