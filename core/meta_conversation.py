# core/meta_conversation.py
# Pre-pipeline meta conversation agent.
# Chats with the user to gather project requirements (user stories, goals, non-goals)
# before the DPE pipeline starts.
#
# Supports two usage modes:
#   1. Turn-by-turn (REPL-friendly): start() → next_turn() → ... → brief
#   2. Blocking: converse() — owns its own IO loop (for tests / scripting)
#
# Also provides detect_intent() for classifying user prompts as new-project
# vs existing-code requests.

import json
import yaml
from pathlib import Path
from typing import Optional
from core.ai_router import AIGateway
from core.prompt_assembler import build_language_instruction

# Default config path (relative to project root)
_DEFAULT_CONFIG_PATH = "dpe_roles_config.yaml"

# Hardcoded fallbacks used when YAML config is unavailable
_DEFAULT_PROJECT_MODEL = "deepseek/deepseek-v4-flash"
_DEFAULT_PROJECT_TEMPLATE = "templates/meta_conversation.md"
_DEFAULT_PROJECT_MAX_TURNS = 6
_DEFAULT_TASK_MODEL = "deepseek/deepseek-v4-flash"
_DEFAULT_TASK_TEMPLATE = "templates/task_meta_conversation.md"
_DEFAULT_TASK_MAX_TURNS = 4
_DEFAULT_INTENT_MODEL = "deepseek/deepseek-v4-flash"

# JSON schema for conversation (brief / task spec)
META_JSON_SCHEMA = """

[CRITICAL HARD RESTRAINT]
You MUST output ONLY a valid JSON object.
The JSON must perfectly match one of the following schemas:

**Schema A — Need clarification (ask the user):**
{
    "status": "asking",
    "message": "Your natural conversational response — acknowledge the user's input, share your understanding, ask follow-up naturally",
    "analysis_so_far": "Brief summary of what you understand so far"
}

Use Schema A when ANY of these apply:
- The input is too vague or generic to derive a real software project ("I'd like a app", "make something cool")
- The input is nonsense, gibberish, or clearly not a software project request
- The input contains only profanity or trolling
- You need more information about what to build, for whom, or core features
- You cannot yet determine whether the user wants a NEW project or to MODIFY existing code

**Schema B — Ready to proceed (produce the brief):**
{
    "status": "complete",
    "message": "Brief friendly summary of what you understood and produced",
    "intent": "new_project" or "existing_code",
    "project_brief": {
        "project_name": "Short descriptive name",
        "description": "1-3 sentence summary",
        "user_stories": ["...", "..."],
        "goals": ["...", "..."],
        "non_goals": ["...", "..."],
        "tech_constraints": ["...", "..."],
        "target_users": "Who will use this",
        "success_criteria": "How to know it's done"
    }
}

Use Schema B when ALL of these are met:
- You understand WHAT the user wants to build
- You can determine intent: "new_project" (build from scratch) or "existing_code" (modify existing code)
- You have enough for a structured brief (the downstream pipeline handles minor gaps)
"""

# Prompt for brief revision
REVISION_SYSTEM_PROMPT = (
    "You are a friendly project manager. The user has reviewed the project brief and wants changes. "
    "Apply their feedback to the brief and return the revised version.\n\n"
    "You MUST output ONLY a valid JSON object matching this schema:\n"
    '{"status": "complete", "message": "Brief summary of changes made", '
    '"project_brief": {'
    '"project_name": "...", "description": "...", "user_stories": [...], '
    '"goals": [...], "non_goals": [...], "tech_constraints": [...], '
    '"target_users": "...", "success_criteria": "..."}}'
)

# Intent detection JSON schema
_INTENT_SCHEMA = """

[CRITICAL HARD RESTRAINT]
You MUST output ONLY a valid JSON object matching this schema:
{
    "intent": "new_project" or "existing_code" or "unclear",
    "reasoning": "Brief explanation of your classification"
}

Classification rules:
- "new_project": The user wants to CREATE something from scratch. Signals: "build me", "create", "make a", "develop a", "I want to build", "design a", "implement a", greenfield, starting fresh.
- "existing_code": The user wants to MODIFY or ADD TO existing code. Signals: "add", "fix", "modify", "update my", "refactor", "change the", "improve", "extend", "debug", "in my project", references to existing files or features.
- "unclear": The prompt is ambiguous — could be either new or existing work.

When in doubt, prefer "unclear" over guessing wrong.
"""

# Intent detection system prompt
_INTENT_SYSTEM_PROMPT = """You are a project intent classifier. Your job is to determine whether the user wants to:
1. Create a brand new project from scratch ("new_project")
2. Work on existing code — add features, fix bugs, refactor ("existing_code")
3. The intent is ambiguous ("unclear")

Analyze the user's prompt and classify it. Be concise."""


def _load_meta_config(config_path: str = _DEFAULT_CONFIG_PATH) -> dict:
    """Load meta_conversation section from YAML config. Returns empty dict on failure."""
    try:
        path = Path(config_path)
        if not path.exists():
            # Try relative to this file's parent (project root)
            path = Path(__file__).resolve().parent.parent / config_path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config.get("meta_conversation", {})
    except Exception:
        pass
    return {}


def detect_intent_heuristic(prompt: str) -> dict | None:
    """
    Fast keyword-based intent detection. Returns None when ambiguous
    (caller should fall back to LLM-based detect_intent).
    """
    prompt_lower = prompt.lower()

    existing_signals = [
        "fix", "bug", "add to", "refactor", "update my", "modify",
        "change the", "improve", "extend", "debug", "in my project",
        "in the code", "existing", "current implementation", "our codebase",
        "in the repo", "in this project", "the function", "the class",
        "the module", "this file", "error in", "broken", "crash",
        # Continue / resume development on an existing project
        "continue", "继续", "接着做", "接着开发", "继续开发", "resume",
    ]
    new_signals = [
        "build me", "create a", "make a", "develop a", "i want to build",
        "design a", "implement a", "new project", "from scratch",
        "build a", "create a new", "start a", "scaffold",
    ]

    existing_score = sum(1 for s in existing_signals if s in prompt_lower)
    new_score = sum(1 for s in new_signals if s in prompt_lower)

    if existing_score > new_score and existing_score >= 1:
        return {"intent": "existing_code", "reasoning": "Keyword heuristic (existing signals detected)"}
    elif new_score > existing_score and new_score >= 1:
        return {"intent": "new_project", "reasoning": "Keyword heuristic (new project signals detected)"}
    else:
        return None  # Ambiguous — caller should use LLM


def detect_intent(prompt: str, config_path: str = _DEFAULT_CONFIG_PATH,
                  user_lang: str | None = None) -> dict:
    """
    Single-shot LLM call to classify whether the user wants a new project
    or to work on existing code. Returns:
        {"intent": "new_project"|"existing_code"|"unclear", "reasoning": "..."}
    """
    meta_cfg = _load_meta_config(config_path)
    model = meta_cfg.get("intent_detection", {}).get("model", _DEFAULT_INTENT_MODEL)

    intent_cfg = meta_cfg.get("intent_detection", {})
    gateway = AIGateway(
        model_name=model,
        enable_thinking=intent_cfg.get("enable_thinking", False),
        thinking_effort=intent_cfg.get("thinking_effort") if intent_cfg.get("enable_thinking") else None,
    )
    system_prompt = _INTENT_SYSTEM_PROMPT + _INTENT_SCHEMA
    lang_instruction = build_language_instruction(user_lang)
    if lang_instruction:
        system_prompt = lang_instruction + "\n\n" + system_prompt
    response = gateway.generate(
        system_prompt=system_prompt,
        user_prompt=f"Classify this user prompt:\n\n{prompt}",
        is_json_mode=True
    )

    try:
        clean = response.replace('```json', '').replace('```', '').strip()
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        # Fallback: assume new project if classification fails
        return {"intent": "new_project", "reasoning": "Intent detection failed, defaulting to new project"}

    intent = parsed.get("intent", "unclear")
    if intent not in ("new_project", "existing_code", "unclear"):
        intent = "unclear"

    return {"intent": intent, "reasoning": parsed.get("reasoning", "")}


class MetaConversationAgent:
    """
    Pre-pipeline agent that chats with the user to extract a structured
    project brief (goals, user stories, non-goals) before the DPE pipeline runs.

    Configuration is read from dpe_roles_config.yaml (meta_conversation.project section).
    Falls back to hardcoded defaults if config is unavailable.
    """

    def __init__(self, model_name: str = None, config_path: str = _DEFAULT_CONFIG_PATH,
                 user_lang: str | None = None):
        meta_cfg = _load_meta_config(config_path)
        project_cfg = meta_cfg.get("project", {})

        resolved_model = model_name or project_cfg.get("model", _DEFAULT_PROJECT_MODEL)
        template_file = project_cfg.get("template_file", _DEFAULT_PROJECT_TEMPLATE)
        self._max_turns = project_cfg.get("max_turns", _DEFAULT_PROJECT_MAX_TURNS)

        _proj_thinking = project_cfg.get("enable_thinking", False)
        self.gateway = AIGateway(
            model_name=resolved_model,
            enable_thinking=_proj_thinking,
            thinking_effort=project_cfg.get("thinking_effort") if _proj_thinking else None,
        )
        template_path = Path(__file__).resolve().parent.parent / template_file
        self.system_prompt = template_path.read_text(encoding="utf-8")
        lang_instruction = build_language_instruction(user_lang)
        if lang_instruction:
            self.system_prompt = self.system_prompt + "\n\n" + lang_instruction
        self._history = []
        self._turn_count = 0
        self._last_message = None

    # ── Turn-by-turn API (REPL-friendly) ──

    def start(self, initial_prompt: str) -> dict:
        """
        Begin the conversation with the user's initial prompt.
        Returns {"status": "asking", "message": "..."} or
                {"status": "complete", "message": "...", "project_brief": {...}}.
        Resets internal state for a new conversation.
        """
        self._history = []
        self._turn_count = 0
        return self._call_llm(initial_prompt)

    def next_turn(self, user_answer: str) -> dict:
        """
        Feed the user's answer back and get the next response.
        Returns same format as start().
        """
        self._turn_count += 1

        # Record the last message + user answer into history
        if self._last_message:
            self._history.append({
                "assistant_message": self._last_message,
                "user_answer": user_answer
            })

        # Force completion if we hit the turn limit
        if self._turn_count >= self._max_turns:
            return self._call_llm(
                "[System: You have asked enough questions. Produce the project brief NOW using Schema B.]"
            )

        return self._call_llm(user_answer)

    def force_brief(self) -> dict:
        """Force the agent to produce a brief regardless of conversation state."""
        return self._call_llm(
            "[System: Produce the project brief NOW using Schema B.]"
        )

    def revise_brief(self, brief: dict, feedback: str,
                     user_lang: str | None = None) -> dict:
        """
        Revise an existing brief based on user feedback.
        Returns {"status": "complete", "message": "...", "project_brief": {...}}.
        """
        user_prompt = (
            f"[Current Project Brief]\n{json.dumps(brief, indent=2, ensure_ascii=False)}\n\n"
            f"[User Feedback]\n{feedback}"
        )
        system_prompt = REVISION_SYSTEM_PROMPT
        lang_instruction = build_language_instruction(user_lang)
        if lang_instruction:
            system_prompt = lang_instruction + "\n\n" + system_prompt
        response = self.gateway.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            is_json_mode=True
        )

        try:
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # Retry once
            user_prompt += "\n[System: Your last response was not valid JSON. Please try again.]"
            response = self.gateway.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json_mode=True
            )
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)

        brief = parsed.get("project_brief")
        if not brief:
            raise ValueError("Revise brief returned 'complete' but missing 'project_brief'")
        return parsed

    # ── Blocking API (backward compat for tests) ──

    def converse(self, initial_prompt: str, io_handler=None) -> dict:
        """
        Blocking multi-turn conversation. Owns its own IO loop.
        Used by tests and scripting. REPL should use start()/next_turn() instead.
        """
        if io_handler is None:
            from rich.prompt import Prompt
            io_handler = lambda m: Prompt.ask(f"\n{m}")

        result = self.start(initial_prompt)

        while result.get("status") == "asking":
            message = result.get("message", "Can you tell me more?")
            answer = io_handler(message)
            result = self.next_turn(answer)

        brief = result.get("project_brief")
        if not brief:
            raise ValueError("Meta agent did not produce a project_brief")
        return brief

    # ── Internal ──

    def _call_llm(self, current_input: str) -> dict:
        """Call the LLM and parse the response."""
        self._last_message = None
        user_prompt = self._build_user_prompt(self._history, current_input)

        response = self.gateway.generate(
            system_prompt=self.system_prompt + META_JSON_SCHEMA,
            user_prompt=user_prompt,
            is_json_mode=True
        )

        try:
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # Retry once with a hint
            user_prompt = self._build_user_prompt(
                self._history,
                "[System: Your last response was not valid JSON. Please try again.]"
            )
            response = self.gateway.generate(
                system_prompt=self.system_prompt + META_JSON_SCHEMA,
                user_prompt=user_prompt,
                is_json_mode=True
            )
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)

        status = parsed.get("status")

        if status == "asking":
            self._last_message = parsed.get("message", "Can you tell me more about your project?")
            return parsed

        if status == "complete":
            brief = parsed.get("project_brief")
            if not brief:
                raise ValueError("Meta agent returned 'complete' but missing 'project_brief'")
            return parsed

        raise ValueError(f"Meta agent returned unknown status: {status}")

    def _build_user_prompt(self, conversation_history: list, current_input: str) -> str:
        """
        Serialize conversation history + current input into a single user prompt
        (since AIGateway.generate() only accepts system_prompt + user_prompt).
        """
        parts = []

        if conversation_history:
            parts.append("[Conversation History]")
            for i, turn in enumerate(conversation_history, 1):
                parts.append(f"Assistant: {turn['assistant_message']}")
                parts.append(f"User: {turn['user_answer']}")
            parts.append("")

        parts.append(f"[Current User Input]\n{current_input}")

        return "\n".join(parts)


class TaskMetaConversationAgent(MetaConversationAgent):
    """
    Task-scoped meta agent that gathers requirements for a single task
    within an existing project.

    Configuration is read from dpe_roles_config.yaml (meta_conversation.task section).
    Falls back to hardcoded defaults if config is unavailable.
    """

    def __init__(self, model_name: str = None, config_path: str = _DEFAULT_CONFIG_PATH,
                 user_lang: str | None = None):
        meta_cfg = _load_meta_config(config_path)
        task_cfg = meta_cfg.get("task", {})

        resolved_model = model_name or task_cfg.get("model", _DEFAULT_TASK_MODEL)
        template_file = task_cfg.get("template_file", _DEFAULT_TASK_TEMPLATE)
        self._max_turns = task_cfg.get("max_turns", _DEFAULT_TASK_MAX_TURNS)

        _task_thinking = task_cfg.get("enable_thinking", False)
        self.gateway = AIGateway(
            model_name=resolved_model,
            enable_thinking=_task_thinking,
            thinking_effort=task_cfg.get("thinking_effort") if _task_thinking else None,
        )
        template_path = Path(__file__).resolve().parent.parent / template_file
        self.system_prompt = template_path.read_text(encoding="utf-8")
        lang_instruction = build_language_instruction(user_lang)
        if lang_instruction:
            self.system_prompt = self.system_prompt + "\n\n" + lang_instruction
        self._history = []
        self._turn_count = 0
        self._last_message = None

    def set_project_context(self, brief: str | None, existing_tasks: list[dict] | None):
        """Inject project brief and existing tasks into the system prompt."""
        parts = [self.system_prompt, "\n[Project Context]"]
        if brief:
            parts.append(f"Project Brief:\n{brief[:2000]}")
        if existing_tasks:
            parts.append("Existing Tasks:")
            for t in existing_tasks[:10]:
                status = t.get("status", "?")
                prompt = t.get("prompt", "")[:80]
                parts.append(f"  - #{t.get('id', '?')} [{status}]: {prompt}")
        self.system_prompt = "\n".join(parts)

    def _call_llm(self, current_input: str) -> dict:
        """Override to handle task_spec instead of project_brief."""
        self._last_message = None
        user_prompt = self._build_user_prompt(self._history, current_input)

        response = self.gateway.generate(
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            is_json_mode=True
        )

        try:
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            user_prompt = self._build_user_prompt(
                self._history,
                "[System: Your last response was not valid JSON. Please try again.]"
            )
            response = self.gateway.generate(
                system_prompt=self.system_prompt,
                user_prompt=user_prompt,
                is_json_mode=True
            )
            clean = response.replace('```json', '').replace('```', '').strip()
            parsed = json.loads(clean)

        status = parsed.get("status")
        if status == "asking":
            self._last_message = parsed.get("message", "Can you tell me more?")
            return parsed
        if status == "complete":
            spec = parsed.get("task_spec")
            if not spec:
                raise ValueError("Task meta agent returned 'complete' but missing 'task_spec'")
            return parsed
        raise ValueError(f"Task meta agent returned unknown status: {status}")

    def next_turn(self, user_answer: str) -> dict:
        self._turn_count += 1
        if self._last_message:
            self._history.append({
                "assistant_message": self._last_message,
                "user_answer": user_answer
            })
        if self._turn_count >= self._max_turns:
            return self._call_llm(
                "[System: You have asked enough questions. Produce the task spec NOW using Schema B.]"
            )
        return self._call_llm(user_answer)


def format_task_spec_as_prompt(spec: dict) -> str:
    """Convert a task spec into an enriched prompt string."""
    lines = [spec.get("description", "")]
    criteria = spec.get("acceptance_criteria", [])
    if criteria:
        lines.append("\nAcceptance Criteria:")
        for c in criteria:
            lines.append(f"- {c}")
    scope = spec.get("scope")
    if scope:
        lines.append(f"\nScope: {scope}")
    oos = spec.get("out_of_scope")
    if oos:
        lines.append(f"Out of scope: {oos}")
    return "\n".join(lines)


def _coerce_str(val) -> str:
    """Coerce a value to string. Lists are joined; None becomes empty string."""
    if val is None:
        return ""
    if isinstance(val, list):
        return "; ".join(str(v) for v in val)
    return str(val)


def _coerce_list(val) -> list[str]:
    """Coerce a value to a flat list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        flat = []
        for item in val:
            if isinstance(item, list):
                flat.extend(str(i) for i in item)
            else:
                flat.append(str(item))
        return flat
    return [str(val)]


def format_brief_as_markdown(brief: dict) -> str:
    """Convert a structured project brief dict into a readable Markdown document."""
    lines = [f"# Project Brief: {_coerce_str(brief.get('project_name', 'Untitled'))}", ""]

    desc = _coerce_str(brief.get("description", ""))
    if desc:
        lines.extend(["## Description", desc, ""])

    target = _coerce_str(brief.get("target_users", ""))
    if target:
        lines.extend(["## Target Users", target, ""])

    stories = _coerce_list(brief.get("user_stories"))
    if stories:
        lines.append("## User Stories")
        for s in stories:
            lines.append(f"- {s}")
        lines.append("")

    goals = _coerce_list(brief.get("goals"))
    if goals:
        lines.append("## Goals")
        for g in goals:
            lines.append(f"- {g}")
        lines.append("")

    non_goals = _coerce_list(brief.get("non_goals"))
    if non_goals:
        lines.append("## Non-Goals")
        for ng in non_goals:
            lines.append(f"- {ng}")
        lines.append("")

    constraints = _coerce_list(brief.get("tech_constraints"))
    if constraints:
        lines.append("## Technical Constraints")
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")

    criteria = _coerce_str(brief.get("success_criteria", ""))
    if criteria:
        lines.extend(["## Success Criteria", criteria, ""])

    return "\n".join(lines)


# Header prepended to the verbatim requirements transcript when it is promoted
# to project/spec.md. Kept here (not duplicated in the emit tool / seed path) so
# the format stays consistent wherever spec.md is produced.
_SPEC_HEADER = (
    "# Project Spec — verbatim requirements conversation\n\n"
    "> Source: the full user/assistant requirements conversation. "
    "The Project Brief is a condensed summary of this; when the "
    "two differ, prefer the explicit detail here.\n\n"
)


def build_spec_markdown(transcript: str) -> str:
    """Wrap the verbatim requirements transcript as the project spec.md body.

    Returns "" for an empty/whitespace transcript so callers can skip writing.
    """
    raw = (transcript or "").strip()
    return (_SPEC_HEADER + raw) if raw else ""


def brief_to_step1_goals(brief: dict) -> dict:
    """Convert a project brief to step1_goals.json format.

    Used to skip the Nominator (Step 1) when the meta conversation
    already produced a detailed brief. The output is compatible with
    downstream steps that read from the step 1 output (step1_goals.json).
    """
    return {
        "mvp_goals": _coerce_list(brief.get("goals")),
        "non_goals": _coerce_list(brief.get("non_goals")),
        "constraints": _coerce_list(brief.get("tech_constraints")),
        "assumptions": [],
        "pending_confirmations": [],
        "project_name": _coerce_str(brief.get("project_name", "")),
        "description": _coerce_str(brief.get("description", "")),
        "user_stories": _coerce_list(brief.get("user_stories")),
        "target_users": _coerce_str(brief.get("target_users", "")),
        "success_criteria": _coerce_str(brief.get("success_criteria", "")),
    }
