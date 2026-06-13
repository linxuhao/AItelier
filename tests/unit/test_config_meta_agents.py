# tests/unit/test_config_meta_agents.py
# Tests that MetaConversationAgent and TaskMetaConversationAgent
# read configuration from YAML with fallback to hardcoded defaults.

import json
import pytest
from unittest.mock import patch, MagicMock, mock_open
from pathlib import Path


MOCK_YAML_WITH_CONFIG = """
meta_conversation:
  project:
    model: "custom/project-model"
    template_file: "templates/meta_conversation.md"
    max_turns: 8
  task:
    model: "custom/task-model"
    template_file: "templates/task_meta_conversation.md"
    max_turns: 3
  intent_detection:
    model: "custom/intent-model"
steps:
  - step_id: "1"
    role_name: "Nominator"
    green_team:
      model: "deepseek/deepseek-v4-flash"
      temperature: 0.2
      template_file: "templates/step1_nominator.md"
    red_team:
      model: "deepseek/deepseek-v4-flash"
      temperature: 0.1
      template_file: "templates/step1_nominator_red.md"
"""

MOCK_YAML_EMPTY = """
steps:
  - step_id: "1"
    role_name: "Nominator"
    green_team:
      model: "deepseek/deepseek-v4-flash"
      temperature: 0.2
      template_file: "templates/step1_nominator.md"
    red_team:
      model: "deepseek/deepseek-v4-flash"
      temperature: 0.1
      template_file: "templates/step1_nominator_red.md"
"""

MOCK_TEMPLATE = "# Mock Template\nYou are a PM."


class TestMetaConversationAgentConfig:
    """Tests for MetaConversationAgent reading config from YAML."""

    @patch("core.meta_conversation.AIGateway")
    def test_reads_model_from_config(self, mock_gw_cls):
        """Agent uses model from YAML config when no model_name arg given."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "project": {
                    "model": "custom/project-model",
                    "template_file": "templates/meta_conversation.md",
                    "max_turns": 8,
                }
            }
            # Mock the template file read
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import MetaConversationAgent
                agent = MetaConversationAgent(config_path="dummy.yaml")

        mock_gw_cls.assert_called_with(
            model_name="custom/project-model",
            enable_thinking=False,
            thinking_effort=None,
        )
        assert agent._max_turns == 8

    @patch("core.meta_conversation.AIGateway")
    def test_model_name_arg_overrides_config(self, mock_gw_cls):
        """Explicit model_name parameter overrides YAML config."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "project": {"model": "custom/project-model", "max_turns": 8}
            }
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import MetaConversationAgent
                agent = MetaConversationAgent(
                    model_name="override/model", config_path="dummy.yaml"
                )

        mock_gw_cls.assert_called_with(
            model_name="override/model",
            enable_thinking=False,
            thinking_effort=None,
        )

    @patch("core.meta_conversation.AIGateway")
    def test_fallback_when_config_missing(self, mock_gw_cls):
        """Falls back to defaults when YAML has no meta_conversation section."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {}  # no project config
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import MetaConversationAgent
                agent = MetaConversationAgent(config_path="dummy.yaml")

        mock_gw_cls.assert_called_with(
            model_name="deepseek/deepseek-v4-flash",
            enable_thinking=False,
            thinking_effort=None,
        )
        assert agent._max_turns == 6  # default

    @patch("core.meta_conversation.AIGateway")
    def test_max_turns_from_config(self, mock_gw_cls):
        """Agent respects max_turns from config."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "project": {
                    "model": "deepseek/deepseek-v4-flash",
                    "template_file": "templates/meta_conversation.md",
                    "max_turns": 3,
                }
            }
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import MetaConversationAgent
                agent = MetaConversationAgent(config_path="dummy.yaml")

        assert agent._max_turns == 3

    @patch("core.meta_conversation.AIGateway")
    def test_forces_brief_at_configured_max_turns(self, mock_gw_cls):
        """Agent forces brief generation at the configured turn limit."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "project": {
                    "model": "deepseek/deepseek-v4-flash",
                    "template_file": "templates/meta_conversation.md",
                    "max_turns": 2,
                }
            }
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import MetaConversationAgent
                agent = MetaConversationAgent(config_path="dummy.yaml")

        mock_gw = MagicMock()
        mock_gw.generate.side_effect = [
            json.dumps({"status": "asking", "message": "Q1", "analysis_so_far": "..."}),
            json.dumps({"status": "asking", "message": "Q2", "analysis_so_far": "..."}),
            # Turn 2 hits max_turns → forced brief
            json.dumps({
                "status": "complete",
                "project_brief": {
                    "project_name": "Test", "description": "test",
                    "user_stories": [], "goals": [], "non_goals": [],
                    "tech_constraints": [], "target_users": "", "success_criteria": ""
                }
            }),
        ]
        agent.gateway = mock_gw

        agent.start("test")
        result = agent.next_turn("answer 1")
        assert result["status"] == "asking"
        result = agent.next_turn("answer 2")
        assert result["status"] == "complete"


class TestTaskMetaConversationAgentConfig:
    """Tests for TaskMetaConversationAgent reading config from YAML."""

    @patch("core.meta_conversation.AIGateway")
    def test_reads_task_model_from_config(self, mock_gw_cls):
        """Task agent uses model from task section of YAML config."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "task": {
                    "model": "custom/task-model",
                    "template_file": "templates/task_meta_conversation.md",
                    "max_turns": 3,
                }
            }
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import TaskMetaConversationAgent
                agent = TaskMetaConversationAgent(config_path="dummy.yaml")

        mock_gw_cls.assert_called_with(
            model_name="custom/task-model",
            enable_thinking=False,
            thinking_effort=None,
        )
        assert agent._max_turns == 3

    @patch("core.meta_conversation.AIGateway")
    def test_task_fallback_defaults(self, mock_gw_cls):
        """Task agent falls back to defaults when config missing."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {}
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import TaskMetaConversationAgent
                agent = TaskMetaConversationAgent(config_path="dummy.yaml")

        mock_gw_cls.assert_called_with(
            model_name="deepseek/deepseek-v4-flash",
            enable_thinking=False,
            thinking_effort=None,
        )
        assert agent._max_turns == 4  # default task max turns

    @patch("core.meta_conversation.AIGateway")
    def test_task_model_name_override(self, mock_gw_cls):
        """Explicit model_name overrides task config."""
        with patch("core.meta_conversation._load_meta_config") as mock_cfg:
            mock_cfg.return_value = {
                "task": {"model": "custom/task-model", "max_turns": 3}
            }
            with patch.object(Path, "read_text", return_value=MOCK_TEMPLATE):
                from core.meta_conversation import TaskMetaConversationAgent
                agent = TaskMetaConversationAgent(
                    model_name="override/task-model", config_path="dummy.yaml"
                )

        mock_gw_cls.assert_called_with(
            model_name="override/task-model",
            enable_thinking=False,
            thinking_effort=None,
        )
