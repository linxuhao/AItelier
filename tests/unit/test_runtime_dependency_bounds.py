"""Runtime dependencies a rebuild can float must carry an upper bound pinned by a test.

After the 2026-10-28 litellm 1.106.5 incident, apscheduler is the next unbounded runtime
dependency: apscheduler 4 is a full API rewrite without
``apscheduler.schedulers.asyncio.AsyncIOScheduler``, which ``core/scheduler.py`` imports.
The Dockerfile must also fail the build when a tool-calling litellm completion cannot run,
so these tests pin both the bound and the presence/order of that build-time smoke RUN.
"""
import re
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "Dockerfile"


def test_apscheduler_has_an_upper_bound_and_the_installed_version_satisfies_it():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    requirement = next(Requirement(line) for line in declared
                       if re.match(r"apscheduler\b", line.strip()))
    assert "<" in str(requirement.specifier), "apscheduler needs an upper bound (see module docstring)"
    installed = Version(metadata.version("apscheduler"))
    assert installed in requirement.specifier, (
        f"installed apscheduler {installed} is outside the declared {requirement.specifier}")


def test_dockerfile_runs_the_litellm_tools_smoke_after_require_output_engine():
    text = DOCKERFILE.read_text()
    engine = text.find("RUN python -c \"from core.output_migration import require_output_engine;")
    assert engine != -1, "Dockerfile lost the require_output_engine build check"
    smoke_lines = [m for m in re.finditer(r"^RUN .*litellm\.completion\(.*$", text, re.MULTILINE)
                   if "tools=" in m.group(0) and "mock_response=" in m.group(0)
                   and "LITELLM_LOCAL_MODEL_COST_MAP=True" in m.group(0)]
    assert len(smoke_lines) == 1, "Dockerfile needs exactly one litellm tools smoke RUN"
    assert smoke_lines[0].start() > engine, (
        "the litellm tools smoke RUN must come after the require_output_engine RUN")
