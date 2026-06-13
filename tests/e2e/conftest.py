# tests/e2e/conftest.py
# E2E 测试默认使用便宜模型，不影响 prod 配置。
# 可通过环境变量覆盖：E2E_GREEN_MODEL / E2E_RED_MODEL

import pytest
import os
from core.agents import AgentFactory

# ── 在这里改成你想用的便宜模型 ──────────────────────────────
CHEAP_GREEN = os.getenv("E2E_GREEN_MODEL", "deepseek/deepseek-v4-flash")
CHEAP_RED = os.getenv("E2E_RED_MODEL", "deepseek/deepseek-v4-flash")


@pytest.fixture(autouse=True)
def use_cheap_models():
    """自动将所有 E2E 测试的 AgentFactory 模型替换为便宜模型。"""
    original_init = AgentFactory.__init__

    def patched_init(self, config_path="dpe_roles_config.yaml"):
        original_init(self, config_path)
        for step in self.config.get("steps", []):
            if "green_team" in step:
                step["green_team"]["model"] = CHEAP_GREEN
            if "red_team" in step:
                step["red_team"]["model"] = CHEAP_RED

    AgentFactory.__init__ = patched_init
    yield
    AgentFactory.__init__ = original_init
