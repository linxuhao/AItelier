# tests/unit/test_force_all_agent_models.py
# AITELIER_FORCE_ALL_AGENT_MODELS pins EVERY agent to one model so a benchmark
# result is attributable to the harness rather than to the stronger model a few
# production roles (architect / pm / final_verifier) are pinned to. Unset must
# be a total no-op; set must win over explicit per-role models AND over the
# skillflow "host"/"default" sentinel.

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.agents import AgentFactory, HOST_AGENT_MODEL
from core.ai_router import resolve_agent_model
from core.meta_agent import MetaAgent
from skillflow.agent_registry import AgentRegistry

ENV = "AITELIER_FORCE_ALL_AGENT_MODELS"
PIN = "deepseek/deepseek-v4-flash"
MOCK_TEMPLATE = "# Template"


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch):
    """Every test states its own pin; never inherit one from the environment."""
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture
def factory():
    reg = AgentRegistry()
    # The production shape: most roles on flash, a few on pro, plus a
    # host-delegated role (generated pipelines / converter roles).
    reg.register("researcher", model="deepseek/deepseek-v4-flash",
                 template="step1_5_researcher.md", tools=[])
    reg.register("architect", model="deepseek/deepseek-v4-pro",
                 template="step2_architect.md", tools=[])
    reg.register("generated_role", model="host",
                 system_prompt="you are generated", tools=[])
    f = AgentFactory(registry=reg)
    f._load_template = MagicMock(return_value=MOCK_TEMPLATE)
    return f


class TestResolveAgentModel:
    def test_unset_returns_the_model_unchanged(self):
        assert resolve_agent_model("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"

    def test_set_replaces_the_model(self, monkeypatch):
        monkeypatch.setenv(ENV, PIN)
        assert resolve_agent_model("deepseek/deepseek-v4-pro") == PIN

    def test_empty_value_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv(ENV, "")
        assert resolve_agent_model("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"


class TestAgentFactory:
    def test_unset_keeps_per_role_models(self, factory):
        assert factory.get_agent("researcher").gateway.litellm_model.endswith(
            "deepseek-v4-flash")
        assert factory.get_agent("architect").gateway.litellm_model.endswith(
            "deepseek-v4-pro")
        assert factory.get_agent("generated_role").gateway.litellm_model.endswith(
            HOST_AGENT_MODEL.split("/")[-1])

    def test_pin_wins_over_an_explicit_role_model(self, factory, monkeypatch):
        monkeypatch.setenv(ENV, PIN)
        assert factory.get_agent("architect").gateway.litellm_model.endswith(
            "deepseek-v4-flash")

    def test_pin_covers_every_role_including_the_host_sentinel(
            self, factory, monkeypatch):
        monkeypatch.setenv(ENV, "deepseek/deepseek-v4-pro")
        for role in ("researcher", "architect", "generated_role"):
            assert factory.get_agent(role).gateway.litellm_model.endswith(
                "deepseek-v4-pro"), role

    def test_pin_applies_to_native_agents_too(self, factory, monkeypatch):
        monkeypatch.setenv(ENV, "deepseek/deepseek-v4-pro")
        agent = factory.get_native_agent("researcher")
        assert agent.gateway.litellm_model.endswith("deepseek-v4-pro")


class TestMetaConversation:
    """The meta conversation runs as part of the benchmarked path."""

    def _project_agent(self, model):
        from core.meta_conversation import MetaConversationAgent
        with patch("core.meta_conversation._load_meta_config") as cfg:
            cfg.return_value = {"project": {
                "model": model, "template_file": "templates/meta_conversation.md"}}
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                return MetaConversationAgent(config_path="dummy.yaml")

    def _task_agent(self, model):
        from core.meta_conversation import TaskMetaConversationAgent
        with patch("core.meta_conversation._load_meta_config") as cfg:
            cfg.return_value = {"task": {
                "model": model,
                "template_file": "templates/task_meta_conversation.md"}}
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                return TaskMetaConversationAgent(config_path="dummy.yaml")

    def test_unset_keeps_the_configured_model(self):
        assert self._project_agent(
            "deepseek/deepseek-v4-pro").gateway.litellm_model.endswith(
                "deepseek-v4-pro")
        assert self._task_agent(
            "deepseek/deepseek-v4-pro").gateway.litellm_model.endswith(
                "deepseek-v4-pro")

    def test_pin_wins(self, monkeypatch):
        monkeypatch.setenv(ENV, PIN)
        assert self._project_agent(
            "deepseek/deepseek-v4-pro").gateway.litellm_model.endswith(
                "deepseek-v4-flash")
        assert self._task_agent(
            "deepseek/deepseek-v4-pro").gateway.litellm_model.endswith(
                "deepseek-v4-flash")

    def _intent_model(self):
        """Run detect_intent and report the model its gateway resolved to."""
        from core import meta_conversation
        seen = {}

        def fake_generate(gw_self, **kwargs):
            seen["model"] = gw_self.litellm_model
            return '{"intent": "new_project", "reasoning": ""}'

        with patch.object(meta_conversation, "_load_meta_config") as cfg:
            cfg.return_value = {"intent_detection": {
                "model": "deepseek/deepseek-v4-pro"}}
            with patch.object(meta_conversation.AIGateway, "generate",
                              fake_generate):
                meta_conversation.detect_intent("build a todo app",
                                                config_path="dummy.yaml")
        return seen["model"]

    def test_intent_detection_unset_keeps_the_configured_model(self):
        assert self._intent_model().endswith("deepseek-v4-pro")

    def test_pin_reaches_intent_detection(self, monkeypatch):
        monkeypatch.setenv(ENV, PIN)
        assert self._intent_model().endswith("deepseek-v4-flash")


class TestMetaAgent:
    """The butler bypasses AIGateway — it resolves its own model."""

    @pytest.fixture
    def deps(self, tmp_path):
        db = MagicMock()
        ws = MagicMock()
        ws._get_secure_path.return_value = tmp_path
        return db, ws

    def test_unset_keeps_the_configured_model(self, deps):
        db, ws = deps
        with patch("core.meta_agent._load_meta_agent_config",
                   return_value={"model": "deepseek/deepseek-v4-pro"}):
            agent = MetaAgent(db, ws, owner_email="t@local")
        assert agent._raw_model == "deepseek/deepseek-v4-pro"
        assert agent.litellm_model.endswith("deepseek-v4-pro")

    def test_pin_wins(self, deps, monkeypatch):
        db, ws = deps
        monkeypatch.setenv(ENV, PIN)
        with patch("core.meta_agent._load_meta_agent_config",
                   return_value={"model": "deepseek/deepseek-v4-pro"}):
            agent = MetaAgent(db, ws, owner_email="t@local")
        assert agent._raw_model == PIN
        assert agent.litellm_model.endswith("deepseek-v4-flash")

    async def test_pin_reaches_the_compacter(self, deps, monkeypatch):
        db, ws = deps
        monkeypatch.setenv(ENV, "deepseek/deepseek-v4-pro")
        agent = MetaAgent(db, ws, owner_email="t@local", mode="coding")
        seen = {}

        async def fake_acompletion(**kwargs):
            seen.update(kwargs)
            resp = MagicMock()
            resp.choices[0].message.content = "summary"
            return resp

        with patch("core.meta_agent._load_agent_role_config",
                   return_value={"model": "deepseek/deepseek-v4-flash",
                                 "template": "compaction.md"}):
            with patch("litellm.acompletion", side_effect=fake_acompletion):
                out = await agent._summarize_chunk("some transcript")

        assert out == "summary"
        assert seen["model"].endswith("deepseek-v4-pro")
