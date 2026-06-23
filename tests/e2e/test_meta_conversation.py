# tests/test_meta_conversation.py
# Tests for the pre-pipeline meta conversation agent (fully mocked — no network).

import json
import pytest
from unittest.mock import patch, MagicMock
from core.meta_conversation import MetaConversationAgent, format_brief_as_markdown


# ── Unit Tests (no network) ──

class TestFormatBriefAsMarkdown:
    def test_full_brief(self):
        brief = {
            "project_name": "Fibonacci Calculator",
            "description": "CLI tool to compute Fibonacci numbers",
            "target_users": "Developers",
            "user_stories": [
                "As a dev, I want to compute fib(n) so I can verify correctness"
            ],
            "goals": ["Handle n up to 10000"],
            "non_goals": ["No GUI", "No web server"],
            "tech_constraints": ["Python 3.12"],
            "success_criteria": "Returns correct fib(10) = 55"
        }
        md = format_brief_as_markdown(brief)
        assert "# Project Brief: Fibonacci Calculator" in md
        assert "CLI tool to compute Fibonacci numbers" in md
        assert "- Handle n up to 10000" in md
        assert "- No GUI" in md
        assert "- Python 3.12" in md
        assert "Returns correct fib(10) = 55" in md
        assert "- As a dev" in md

    def test_minimal_brief(self):
        brief = {"project_name": "X"}
        md = format_brief_as_markdown(brief)
        assert "# Project Brief: X" in md

    def test_empty_brief(self):
        md = format_brief_as_markdown({})
        assert "# Project Brief: Untitled" in md


class TestBuildUserPrompt:
    def test_first_turn(self):
        agent = MetaConversationAgent.__new__(MetaConversationAgent)
        result = agent._build_user_prompt([], "Build a fibonacci calculator")
        assert "[Current User Input]" in result
        assert "Build a fibonacci calculator" in result
        assert "[Conversation History]" not in result

    def test_with_history(self):
        agent = MetaConversationAgent.__new__(MetaConversationAgent)
        history = [
            {"assistant_message": "What tech stack?", "user_answer": "Python"},
            {"assistant_message": "Any GUI?", "user_answer": "No"},
        ]
        result = agent._build_user_prompt(history, "that's all")
        assert "[Conversation History]" in result
        assert "Assistant: What tech stack?" in result
        assert "User: Python" in result
        assert "Assistant: Any GUI?" in result
        assert "User: No" in result


class TestConverseMocked:
    """Tests with mocked LLM to verify conversation loop logic."""

    @patch("core.meta_conversation.AIGateway")
    def test_immediate_complete(self, mock_gw_cls):
        """LLM returns complete brief on first turn."""
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "status": "complete",
            "message": "Here's the brief!",
            "project_brief": {
                "project_name": "Fib Calc",
                "description": "Fibonacci calculator",
                "user_stories": ["As a user, compute fib(n)"],
                "goals": ["Fast computation"],
                "non_goals": ["No GUI"],
                "tech_constraints": [],
                "target_users": "Devs",
                "success_criteria": "Correct results"
            }
        })
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        brief = agent.converse("build a fibonacci calculator")

        assert brief["project_name"] == "Fib Calc"
        assert len(brief["goals"]) >= 1

    @patch("core.meta_conversation.AIGateway")
    def test_multi_turn_conversation(self, mock_gw_cls):
        """LLM asks one question, then completes."""
        mock_gw = MagicMock()
        responses = [
            json.dumps({
                "status": "asking",
                "message": "Nice idea! What language do you prefer?",
                "analysis_so_far": "User wants a fibonacci calculator"
            }),
            json.dumps({
                "status": "complete",
                "message": "Got it!",
                "project_brief": {
                    "project_name": "Fib Calc",
                    "description": "Fibonacci calculator",
                    "user_stories": [],
                    "goals": ["Compute fib(n)"],
                    "non_goals": [],
                    "tech_constraints": ["Python"],
                    "target_users": "Devs",
                    "success_criteria": "Works"
                }
            }),
        ]
        mock_gw.generate.side_effect = responses
        mock_gw_cls.return_value = mock_gw

        answers = iter(["Python 3.12"])
        io_handler = lambda m: next(answers)

        agent = MetaConversationAgent()
        brief = agent.converse("build fib calc", io_handler=io_handler)

        assert brief["project_name"] == "Fib Calc"
        assert mock_gw.generate.call_count == 2

    @patch("core.meta_conversation.AIGateway")
    def test_json_parse_retry(self, mock_gw_cls):
        """LLM returns bad JSON once, then valid."""
        mock_gw = MagicMock()
        responses = [
            "This is not JSON at all",
            json.dumps({
                "status": "complete",
                "message": "Here's the brief.",
                "project_brief": {
                    "project_name": "Retry Test",
                    "description": "test",
                    "user_stories": [],
                    "goals": [],
                    "non_goals": [],
                    "tech_constraints": [],
                    "target_users": "",
                    "success_criteria": ""
                }
            }),
        ]
        mock_gw.generate.side_effect = responses
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        brief = agent.converse("test", io_handler=lambda m: "answer")

        assert brief["project_name"] == "Retry Test"

    @patch("core.meta_conversation.AIGateway")
    def test_force_brief_on_turn_limit(self, mock_gw_cls):
        """After max turns, agent forces a brief generation."""
        mock_gw = MagicMock()
        # Return "asking" for all turns (hit the limit)
        asking_response = json.dumps({
            "status": "asking",
            "message": "Tell me more",
            "analysis_so_far": "gathering info"
        })
        forced_response = json.dumps({
            "status": "complete",
            "message": "Here's the brief based on what we discussed.",
            "project_brief": {
                "project_name": "Force Test",
                "description": "forced brief",
                "user_stories": [],
                "goals": [],
                "non_goals": [],
                "tech_constraints": [],
                "target_users": "",
                "success_criteria": ""
            }
        })
        # 6 turns of asking + 1 forced complete
        mock_gw.generate.side_effect = [asking_response] * 6 + [forced_response]
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        brief = agent.converse("test", io_handler=lambda m: "more info")

        assert brief["project_name"] == "Force Test"


class TestTurnByTurnMocked:
    """Tests for the turn-by-turn API (used by REPL)."""

    @patch("core.meta_conversation.AIGateway")
    def test_start_immediate_complete(self, mock_gw_cls):
        """start() returns complete brief on first call."""
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "status": "complete",
            "message": "Here's the brief!",
            "project_brief": {
                "project_name": "Quick",
                "description": "done",
                "user_stories": [],
                "goals": ["g1"],
                "non_goals": [],
                "tech_constraints": [],
                "target_users": "",
                "success_criteria": ""
            }
        })
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        result = agent.start("do something quick")

        assert result["status"] == "complete"
        assert result["project_brief"]["project_name"] == "Quick"

    @patch("core.meta_conversation.AIGateway")
    def test_start_then_next_turn(self, mock_gw_cls):
        """start() asks, next_turn() completes."""
        mock_gw = MagicMock()
        mock_gw.generate.side_effect = [
            json.dumps({
                "status": "asking",
                "message": "Great idea! What tech stack?",
                "analysis_so_far": "..."
            }),
            json.dumps({
                "status": "complete",
                "message": "Got it!",
                "project_brief": {
                    "project_name": "TT",
                    "description": "turn test",
                    "user_stories": [],
                    "goals": [],
                    "non_goals": [],
                    "tech_constraints": ["Python"],
                    "target_users": "",
                    "success_criteria": ""
                }
            }),
        ]
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        r1 = agent.start("build something")
        assert r1["status"] == "asking"
        assert r1["message"] == "Great idea! What tech stack?"

        r2 = agent.next_turn("Python")
        assert r2["status"] == "complete"
        assert r2["project_brief"]["tech_constraints"] == ["Python"]

        # Verify history was built correctly
        assert len(agent._history) == 1
        assert agent._history[0]["assistant_message"] == "Great idea! What tech stack?"
        assert agent._history[0]["user_answer"] == "Python"

    @patch("core.meta_conversation.AIGateway")
    def test_force_brief(self, mock_gw_cls):
        """force_brief() produces a brief immediately."""
        mock_gw = MagicMock()
        mock_gw.generate.side_effect = [
            json.dumps({
                "status": "asking",
                "message": "Q1",
                "analysis_so_far": "..."
            }),
            json.dumps({
                "status": "complete",
                "message": "Here's the brief.",
                "project_brief": {
                    "project_name": "Forced",
                    "description": "forced",
                    "user_stories": [],
                    "goals": [],
                    "non_goals": [],
                    "tech_constraints": [],
                    "target_users": "",
                    "success_criteria": ""
                }
            }),
        ]
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        agent.start("test")
        result = agent.force_brief()
        assert result["status"] == "complete"

    @patch("core.meta_conversation.AIGateway")
    def test_start_resets_state(self, mock_gw_cls):
        """start() resets history from previous conversations."""
        mock_gw = MagicMock()
        mock_gw.generate.side_effect = [
            # 1st start()
            json.dumps({
                "status": "asking",
                "message": "Q1",
                "analysis_so_far": "..."
            }),
            # next_turn()
            json.dumps({
                "status": "complete",
                "message": "Done!",
                "project_brief": {
                    "project_name": "First",
                    "description": "first",
                    "user_stories": [],
                    "goals": [],
                    "non_goals": [],
                    "tech_constraints": [],
                    "target_users": "",
                    "success_criteria": ""
                }
            }),
            # 2nd start() — should work with reset state
            json.dumps({
                "status": "complete",
                "message": "Fresh brief!",
                "project_brief": {
                    "project_name": "Fresh",
                    "description": "reset",
                    "user_stories": [],
                    "goals": [],
                    "non_goals": [],
                    "tech_constraints": [],
                    "target_users": "",
                    "success_criteria": ""
                }
            }),
        ]
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        agent.start("first")
        agent.next_turn("answer")
        assert len(agent._history) == 1

        # start() should reset
        result = agent.start("second")
        assert len(agent._history) == 0
        assert result["project_brief"]["project_name"] == "Fresh"

    @patch("core.meta_conversation.AIGateway")
    def test_revise_brief(self, mock_gw_cls):
        """revise_brief() applies user feedback and returns updated brief."""
        mock_gw = MagicMock()
        mock_gw.generate.return_value = json.dumps({
            "status": "complete",
            "message": "Updated the brief!",
            "project_brief": {
                "project_name": "Todo App",
                "description": "A todo app with email notifications",
                "user_stories": [],
                "goals": ["CRUD tasks", "Email notifications"],
                "non_goals": ["Mobile app"],
                "tech_constraints": [],
                "target_users": "Small teams",
                "success_criteria": "Works"
            }
        })
        mock_gw_cls.return_value = mock_gw

        agent = MetaConversationAgent()
        original_brief = {
            "project_name": "Todo App",
            "description": "A todo app",
            "user_stories": [],
            "goals": ["CRUD tasks"],
            "non_goals": ["Mobile app"],
            "tech_constraints": [],
            "target_users": "Small teams",
            "success_criteria": "Works"
        }
        result = agent.revise_brief(original_brief, "Add email notifications to goals")

        assert result["status"] == "complete"
        assert "Email notifications" in result["project_brief"]["goals"]

