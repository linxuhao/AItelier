# core/agents.py
# AgentFactory — reads agent config from agent_configs/ directory or dict.
# v2: each agent config is indexed by role name, contains only LLM params
# (model, template, tools list, thinking, etc.). No JSON schemas hardcoded.
# v3: native tool calling support via DPEAgentNative.

import os
from pathlib import Path
from typing import Optional
from core.ai_router import AIGateway, NativeTurn

# Default model for skillflow "host"-delegated agents (e.g. the skill_converter
# roles, or any generated pipeline's agents). Such configs declare model:"host"
# with their prompt embedded as system_prompt; AItelier maps that single token to
# one real model instead of requiring a per-role agent_config. Override via env.
HOST_AGENT_MODEL = os.getenv("AITELIER_HOST_AGENT_MODEL", "deepseek/deepseek-v4-flash")


class DPEAgent:
    """JSON-mode agent: single-call, response is a JSON string."""

    def __init__(self, gateway: AIGateway, system_prompt: str):
        self.gateway = gateway
        self.system_prompt = system_prompt

    def run(self, user_prompt: str) -> str:
        return self.gateway.generate(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            is_json_mode=True,
        )


class DPEAgentNative:
    """Native tool-calling agent: multi-turn conversation with structured tool calls."""

    def __init__(self, gateway: AIGateway, system_prompt: str):
        self.gateway = gateway
        self.system_prompt = system_prompt

    def turn(self, messages: list[dict], *,
             tools: list[dict] | None = None,
             tool_choice: str = "auto") -> NativeTurn:
        """Single turn of native tool calling.

        Caller owns the messages list — appends assistant/tool messages
        between turns and passes the accumulated list back.
        """
        # Ensure system prompt is first message
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": self.system_prompt})
        return self.gateway.generate_native(
            messages=messages, tools=tools, tool_choice=tool_choice,
        )


class AgentFactory:
    """Creates DPEAgent / DPEAgentNative instances from skillflow's AgentRegistry.

    Only AItelier concern: loading markdown template files and wiring
    up the LLM gateway.  skillflow owns agent config registration.
    """

    DEFAULT_MAX_TOOL_TURNS = 10

    def __init__(self, *, registry=None,
                 template_base: Path | None = None):
        self._registry = registry
        self._template_base = template_base or (
            Path(__file__).parent.parent / "templates"
        )

    def _get_config(self, name: str) -> dict:
        """Look up agent config from registry."""
        if self._registry and name in self._registry:
            ac = self._registry.get(name)
            if ac:
                return ac.to_dict()
        raise ValueError(f"Agent config '{name}' not found in registry")

    def _build_gateway(self, name: str) -> AIGateway:
        """Build AIGateway from agent config."""
        cfg = self._get_config(name)
        model = cfg.get("model", "")
        # skillflow host-delegated roles use the sentinel "host"/"default" — map
        # to one real model so AItelier can run them without a per-role config.
        if model in ("host", "default", ""):
            model = HOST_AGENT_MODEL
        cfg_inner = cfg.get("config", {})
        thinking = cfg_inner.get("thinking", {})
        enable_thinking = thinking.get("enable", False) if isinstance(thinking, dict) else False
        thinking_effort = thinking.get("effort") if enable_thinking else None
        temperature = cfg_inner.get("temperature", 0.2)
        max_output_tokens = cfg_inner.get("max_output_tokens", 8192)

        return AIGateway(
            model_name=model,
            enable_thinking=enable_thinking,
            thinking_effort=thinking_effort,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    def _load_template(self, template_file: str) -> str:
        template_path = (self._template_base / template_file).resolve()
        if not template_path.exists():
            raise FileNotFoundError(f"Template file not found: {template_path}")
        return template_path.read_text(encoding="utf-8")

    # ── Public API ───────────────────────────────────────────────────

    def get_agent(self, name: str) -> DPEAgent:
        """Create a JSON-mode DPEAgent for the given agent config name."""
        gateway = self._build_gateway(name)
        cfg = self._get_config(name)
        cfg_inner = cfg.get("config", {})
        template_file = cfg_inner.get("template", "")
        # Prefer an AItelier template; fall back to a skillflow host config's
        # embedded system_prompt (the converter / generated-pipeline roles).
        template_content = (self._load_template(template_file) if template_file
                            else cfg_inner.get("system_prompt", ""))
        return DPEAgent(gateway=gateway, system_prompt=template_content)

    def get_native_agent(self, name: str) -> DPEAgentNative:
        """Create a native-mode DPEAgentNative for the given agent config name."""
        gateway = self._build_gateway(name)
        cfg = self._get_config(name)
        cfg_inner = cfg.get("config", {})
        template_file = cfg_inner.get("template", "")
        # Prefer an AItelier template; fall back to a skillflow host config's
        # embedded system_prompt (the converter / generated-pipeline roles).
        template_content = (self._load_template(template_file) if template_file
                            else cfg_inner.get("system_prompt", ""))
        return DPEAgentNative(gateway=gateway, system_prompt=template_content)

    def is_native(self, name: str) -> bool:
        """Check if an agent config enables native tool calling."""
        try:
            cfg = self._get_config(name)
            cfg_inner = cfg.get("config", {})
            return cfg_inner.get("native_tool_calling", False)
        except ValueError:
            return False

    def get_fallback_to_json(self, name: str) -> bool:
        """Check if native agent should fall back to JSON mode on failure."""
        try:
            cfg = self._get_config(name)
            cfg_inner = cfg.get("config", {})
            return cfg_inner.get("fallback_to_json_mode", False)
        except ValueError:
            return False

    # ── Existing helpers ─────────────────────────────────────────────

    def _get_config_by_name_or_step(self, name: str) -> dict | None:
        if self._registry and name in self._registry:
            ac = self._registry.get(name)
            if ac:
                return ac.to_dict()
        return None

    def get_max_tool_turns(self, name_or_step_id: str) -> int:
        cfg = self._get_config_by_name_or_step(name_or_step_id)
        if cfg:
            return cfg.get("config", cfg).get("max_tool_turns", cfg.get("max_tool_turns", self.DEFAULT_MAX_TOOL_TURNS))
        return self.DEFAULT_MAX_TOOL_TURNS

    def get_max_retries(self, step_id: str) -> int:
        return 3  # v2: retries defined in graph config, not agent config

    def get_agent_tools(self, name_or_step_id: str) -> list[str]:
        cfg = self._get_config_by_name_or_step(name_or_step_id)
        if cfg:
            return cfg.get("tools", [])
        return []
