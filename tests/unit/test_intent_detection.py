# tests/unit/test_intent_detection.py
# Tests for detect_intent() and detect_intent_heuristic() in core/meta_conversation.py.

import json
import pytest
from unittest.mock import patch, MagicMock


class TestDetectIntentHeuristic:
    """Tests for the keyword-based heuristic intent detection."""

    def test_continue_developing_project(self):
        """'continue developing my X' → existing_code."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("continue developing my todo app")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_chinese_continue_developing(self):
        """'继续开发我的 X' → existing_code."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("继续开发我的习惯追踪应用")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_chinese_continue_doing(self):
        """'接着做 X' → existing_code."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("接着做登录功能")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_chinese_continue_dev(self):
        """'接着开发 X' → existing_code."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("接着开发那个 dashboard")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_resume_keyword(self):
        """'resume work on X' → existing_code."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("resume work on the api server")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_add_feature_still_works(self):
        """Existing keywords still work."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("fix the login bug")
        assert result is not None
        assert result["intent"] == "existing_code"

    def test_new_project_still_works(self):
        """New project keywords still work."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("build me a todo app")
        assert result is not None
        assert result["intent"] == "new_project"

    def test_ambiguous_returns_none(self):
        """Purely ambiguous prompt returns None (LLM fallback)."""
        from core.meta_conversation import detect_intent_heuristic
        result = detect_intent_heuristic("software")
        assert result is None


class TestDetectIntent:
    """Tests for the LLM-based intent detection function."""

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_detect_new_project(self, mock_gw_cls, mock_cfg):
        """LLM classifies prompt as new project."""
        mock_cfg.return_value = {"intent_detection": {"model": "deepseek/deepseek-v4-flash"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "new_project",
            "reasoning": "User wants to create something from scratch"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        result = detect_intent("build me a todo app")

        assert result["intent"] == "new_project"
        assert "scratch" in result["reasoning"].lower() or result["reasoning"]

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_detect_existing_code(self, mock_gw_cls, mock_cfg):
        """LLM classifies prompt as existing code modification."""
        mock_cfg.return_value = {"intent_detection": {"model": "deepseek/deepseek-v4-flash"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "existing_code",
            "reasoning": "User wants to modify existing code"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        result = detect_intent("add dark mode to my todo app")

        assert result["intent"] == "existing_code"

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_detect_unclear(self, mock_gw_cls, mock_cfg):
        """LLM classifies prompt as unclear."""
        mock_cfg.return_value = {"intent_detection": {"model": "deepseek/deepseek-v4-flash"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "unclear",
            "reasoning": "Prompt could be either new or existing"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        result = detect_intent("todo app")

        assert result["intent"] == "unclear"

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_invalid_json_defaults_to_new_project(self, mock_gw_cls, mock_cfg):
        """If LLM returns invalid JSON, defaults to new_project."""
        mock_cfg.return_value = {"intent_detection": {"model": "deepseek/deepseek-v4-flash"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = "This is not JSON at all"
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        result = detect_intent("some prompt")

        assert result["intent"] == "new_project"

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_unknown_intent_defaults_to_unclear(self, mock_gw_cls, mock_cfg):
        """If LLM returns unknown intent value, defaults to unclear."""
        mock_cfg.return_value = {"intent_detection": {"model": "deepseek/deepseek-v4-flash"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "something_else",
            "reasoning": "bad value"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        result = detect_intent("some prompt")

        assert result["intent"] == "unclear"

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_uses_config_model(self, mock_gw_cls, mock_cfg):
        """Verify the model from config is used."""
        mock_cfg.return_value = {"intent_detection": {"model": "custom/model-v2"}}
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "new_project", "reasoning": "test"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        detect_intent("test")

        mock_gw_cls.assert_called_once_with(
            model_name="custom/model-v2",
            enable_thinking=False,
            thinking_effort=None,
        )

    @patch("core.meta_conversation._load_meta_config")
    @patch("core.meta_conversation.AIGateway")
    def test_fallback_model_when_config_missing(self, mock_gw_cls, mock_cfg):
        """Falls back to default model when config has no intent_detection section."""
        mock_cfg.return_value = {}  # no intent_detection key
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "intent": "new_project", "reasoning": "test"
        })
        mock_gw_cls.return_value = mock_gw

        from core.meta_conversation import detect_intent
        detect_intent("test")

        mock_gw_cls.assert_called_once_with(
            model_name="deepseek/deepseek-v4-flash",
            enable_thinking=False,
            thinking_effort=None,
        )
