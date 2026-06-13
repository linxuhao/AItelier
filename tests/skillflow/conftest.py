"""Fixtures for AItelier-specific skillflow integration tests."""

import pytest
from skillflow.core import SkillFlow


@pytest.fixture
def sf():
    """Isolated in-memory SkillFlow instance."""
    return SkillFlow(":memory:")
