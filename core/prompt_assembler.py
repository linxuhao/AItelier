# core/prompt_assembler.py
# [说明] DPE 提示词组装器。替代原始 read_inbox() 的文件全量拼接，
#        转为结构化组装：项目设计 + 任务卡片 + 工具定义 + 目录树自动注入。
#        Agent 通过工具按需探索 project/，而非被动接收全量上下文。
#        目录树自动注入确保 Agent 知道确切的文件名，避免浪费工具轮次猜测。

from pathlib import Path
from typing import Optional
from core.workspace_manager import DPE_GRAPH_NAME, TASK_STEP_SEQUENCE, PROJECT_STEP_SEQUENCE, STEP_SEQUENCE


# Tool definitions are handled by skillflow — injected via _tool_schemas and
# native function calling.  No hardcoded tool descriptions in prompts.


# [Workspace Layout] — SF-10: static, project-global boilerplate. Identical for
# every step, so it lives in the shared system preamble (build_shared_preamble)
# to land in the cacheable prefix rather than being re-billed per step.
WORKSPACE_LAYOUT = (
    "[Workspace Layout]\n"
    "Files in this pipeline live in three locations:\n"
    "1. **Project root** — committed/delivered code (e.g., `hello.py` "
    "after a previous step's `repo_apply`).\n"
    "2. **Step staging (`.tmp`)** — files you just wrote via `write_*` "
    "tools go here FIRST. They are promoted to the step output dir when "
    "the step completes.\n"
    "3. **Step output** — files from previous retries of this step "
    "(if any).\n\n"
    "`read_file` and `list_tree` search in order: "
    "project root → step staging → step output. "
    "The `found_in` field tells you which location the file came from. "
    "When you write a file and need to verify it, use `read_file` — it "
    "will find your file in the staging directory even though it hasn't "
    "been committed to the project root yet."
)


class PromptAssembler:
    """
    组装结构化的 Agent 提示词。
    替代原有 read_inbox() 的原始文件拼接方式。
    """

    def __init__(self, aitelier_root: Optional[Path] = None,
                 repo_type: str = "new"):
        """
        :param aitelier_root: AItelier 项目根目录，用于定位 project/ 设计文档。
                             None 则使用当前工作目录。
        :param repo_type: 项目仓库类型 ("new"/"existing"/"clone")，
                          用于决定是否注入工具定义和目录树。
        """
        self.aitelier_root = aitelier_root or Path.cwd()
        self._repo_type = repo_type

    def assemble(self, step_id: str, project_path: Path,
                 task_card: str = "", feedback: str = "",
                 task_id: int | None = None, code_path: Path = None,
                 resolved_context: dict | None = None,
                 tool_schemas: dict | None = None,
                 *, native: bool = False,
                 hoist_globals: bool = False,
                 hoist_design: bool = False) -> str:
        """
        组装完整的 Agent 提示词。

        :param step_id: 当前步骤 ID
        :param project_path: DPS workspace 根路径
        :param task_card: 任务卡片内容 (deprecated, Inbox removed)
        :param feedback: Red Agent 反馈 (重试时非空)
        :param task_id: Optional task ID — if set, inject project planning context
        :param code_path: Project 代码仓库路径 (如未提供则使用 project_path)
        :param resolved_context: skillflow-resolved context (name→content map)
        :param tool_schemas: skillflow-provided merged tool schemas dict
        :param native: If True, use native-friendly output rules (no JSON format enforcement)
        :return: 结构化提示词字符串
        """
        if code_path is None:
            code_path = project_path

        sections = [""]

        # [Language Instruction] — detect from project brief
        brief = self._load_project_brief(project_path)
        lang_instruction = self._detect_language_instruction(brief)
        if lang_instruction:
            sections.append(lang_instruction)

        # [Output Delivery] — native tool-calling mode: output is ONLY persisted
        # when the agent CALLS a write tool. Some models (e.g. deepseek) tend to
        # paste the file contents as a JSON object / Markdown in their reply
        # instead of calling the tool; that text is discarded and the step fails
        # validation. Make the contract explicit so the agent uses the tools.
        if native:
            write_tools = sorted(
                n for n in (tool_schemas or {})
                if n.startswith(("write_", "create_", "append_")) or n == "write"
            )
            if write_tools:
                tool_list = ", ".join(f"`{t}`" for t in write_tools)
                delivery = (
                    "[Output Delivery — REQUIRED]\n"
                    "You are in native tool-calling mode. Produce every output "
                    f"file by CALLING the matching write tool ({tool_list}). "
                    "Do NOT paste file contents as text, Markdown, or a JSON "
                    "object in your reply — anything not written via a tool call "
                    "is discarded and the step will fail validation. The step is "
                    "complete only once you have called the write tool(s) for all "
                    "required files."
                )
                if "write" in write_tools:
                    # AT-9: pin one canonical root for the generic write(file, …).
                    delivery += (
                        "\nThe `write` tool's `file` is a repo-root-relative path "
                        "including directories (e.g. `strkit/core.py`, "
                        "`tests/test_core.py`). Use the EXACT path the task requires; "
                        "do NOT prefix it with `project/`, and write each file once "
                        "under a single path."
                    )
                sections.append(delivery)
        else:
            # [Output Delivery — JSON mode]
            # This is the ORIGINAL delivery path; native mode (above) was added later.
            # Regression fix: after the stepflow migration (04d7074), JSON-mode prompts
            # lost their tool definitions — only native mode got them back (26c7f6c).
            # Tell the LLM what write tools exist, what files they produce, and the
            # JSON format to use (actions + files dict).
            write_tools = sorted(
                n for n in (tool_schemas or {})
                if n.startswith(("write_", "create_", "append_")) or n == "write"
            )
            if write_tools:
                tool_lines = []
                for t in write_tools:
                    schema = (tool_schemas or {}).get(t, {})
                    desc = schema.get("description", "")
                    params = schema.get("parameters", {})
                    # Show ALL parameter names — the LLM needs to know about
                    # `id` for glob patterns (replaces * in filename) and
                    # `file` for the generic write tool.  Hiding parameters
                    # causes the LLM to omit them → "path traversal denied"
                    # or all output landing in "unknown.json".
                    param_names = list(params.keys())
                    tool_lines.append(f"  - `{t}({', '.join(param_names)})` — {desc}")
                tool_list_block = "\n".join(tool_lines)
                # Build a params example from the tool with the most parameters,
                # so the LLM sees the full required shape (including id, file etc.).
                example_tool = max(write_tools, key=lambda t: len(
                    (tool_schemas or {}).get(t, {}).get("parameters", {})
                ))
                example_schema = (tool_schemas or {}).get(example_tool, {})
                example_params = example_schema.get("parameters", {})
                example_params_json = ", ".join(
                    f'"{k}": "<{k}>"' for k in sorted(example_params.keys())
                )
                delivery = (
                    "[Output Delivery — REQUIRED]\n"
                    "You are in JSON tool-calling mode. Produce output by writing "
                    "your response as a JSON object. Use ONE of these patterns:\n\n"
                    "Pattern A — write via actions:\n"
                    '{"thoughts": "...", '
                    '"actions": [{"tool": "<write_tool_name>", '
                    f'"params": {{{example_params_json}}}}}]}}\n\n'
                    "Pattern B — write via files shortcut:\n"
                    '{"thoughts": "...", '
                    '"files": {"<output_filename>": "<file content here>"}}\n\n'
                    "Available write tools (each writes a specific output file):\n"
                    f"{tool_list_block}\n\n"
                    "Use ONLY the exact tool names listed above. "
                    "Include ALL required parameters shown in the tool signatures. "
                    "Do NOT wrap the JSON in markdown code fences. "
                    "The step is complete only once you have written ALL required "
                    "output files."
                )
                sections.append(delivery)

        # [Workspace Layout] — static boilerplate. When hoist_globals is set the
        # caller has placed it (and the brief) in the shared system preamble, so
        # skip it here to avoid duplication.
        if not hoist_globals:
            sections.append(WORKSPACE_LAYOUT)

        # ── STABLE PREFIX ────────────────────────────────────────────────
        # Prompt-cache ordering (Phase 1): emit the large, slow-changing blocks
        # FIRST so they form a byte-identical prefix that the provider KV cache
        # reuses across the N per-task loop steps (t_plan/t_impl/t_verify all
        # share the same brief + design). Volatile blocks (resolved context,
        # directory tree, feedback) go AFTER so they never poison the prefix.

        # [Project Brief] — inject for all steps except verification (handled below).
        # Skipped when hoist_globals: the brief is in the shared system preamble.
        if step_id not in ("t_verify", "5") and not hoist_globals:
            if brief:
                sections.append(f"[Project Brief]\n{brief}")
            # [Project Spec] — the verbatim requirements, un-truncated and placed
            # in the stable prefix next to the brief (NOT routed through the
            # 6000-char-clipped resolved-context block) so detailed rules survive.
            spec = self._load_project_spec(project_path)
            if spec:
                sections.append(f"[Project Spec — verbatim requirements]\n{spec}")

        # NOTE: the per-step `[Project Design]` code dump was removed. It eagerly
        # scanned the whole code repo (code_path.rglob) and pasted every file's
        # full content into every step — redundant with the skillflow context
        # system (every agent step declares `{ from: "repository" }`, served
        # lazily via the directory tree + read_file/list_tree tools), and
        # unbounded on existing projects (it ballooned the meta `intent_detect`
        # prompt to ~4 MB). The bounded, cache-stable design block lives in
        # build_shared_preamble (_load_fixed_step_docs) for configs that opt in.

        # [Verification Context] — inject brief + goals for verifier steps
        if step_id in ("t_verify", "5"):
            brief = self._load_project_brief(project_path)
            if brief:
                sections.append(f"[Project Brief - for Verification]\n{brief}")
            spec = self._load_project_spec(project_path)
            if spec:
                sections.append(f"[Project Spec — verbatim requirements]\n{spec}")
            # step1_goals.json is delivered to the verifier via skillflow's
            # declared cross-config context (configs/dpe_default.yaml step "5":
            # {config: meta_conversation, step: finalize, output: step1_goals.json}),
            # rendered in the [Pre-resolved Context] block below — no host-side
            # path read (the old dpe_default_v2/1/ copy was wiped by step-1
            # promotion before this ran).

        # ── VOLATILE SUFFIX ──────────────────────────────────────────────

        # [Pre-resolved Context] — context resolved by skillflow from graph specs
        # Includes cross-config reads, step outputs, and tool outputs (e.g. dir_tree).
        # Provided "for free" so the agent doesn't need to spend tool turns exploring.
        if resolved_context:
            ctx_parts = []
            for label, content in resolved_context.items():
                # Truncate very long content to avoid token waste
                if len(content) > 6000:
                    content = content[:6000] + "\n... [truncated]"
                ctx_parts.append(f"### {label}\n{content}")
            if ctx_parts:
                sections.append("[Pre-resolved Context]\n" + "\n\n".join(ctx_parts))

        # [Workspace Directory Tree]
        tree = self._build_workspace_tree(project_path, step_id, code_path=code_path)
        if tree:
            sections.append(f"[Workspace Directory Tree]\n{tree}")

        # [Previous Feedback] — 重试时的 Red Agent 反馈
        if feedback:
            sections.append(f"[Previous Feedback — MUST FIX]\n{feedback}")

        # [User Rejection History] — accumulated user checkpoint rejections
        rejection_history = self._load_user_rejection_history(project_path, step_id)
        if rejection_history:
            sections.append(rejection_history)

        return "\n\n".join(sections)

    def build_shared_preamble(self, project_path: Path, code_path: Path = None,
                              *, graph_name: str,
                              preamble_steps: list[str] | None = None,
                              include_design: bool = False) -> str:
        """Build the byte-identical, project-global system preamble (F1/F2).

        Placed at the FRONT of the system message for steps of a config that
        opts in (``x-aitelier.preamble_steps``), so the provider KV cache reuses
        it across steps. Contains only content identical across a project's
        steps: the static workspace-layout boilerplate, the project brief, and
        (when include_design) the FIXED outputs of ``preamble_steps`` — never the
        growing code repo, which would make the block volatile.

        Fully config-driven: ``graph_name`` and ``preamble_steps`` come from the
        host config registry, so any pipeline (not just DPE) can opt in.
        """
        parts = [WORKSPACE_LAYOUT]
        brief = self._load_project_brief(project_path)
        if brief:
            parts.append(f"[Project Brief]\n{brief}")
        spec = self._load_project_spec(project_path)
        if spec:
            parts.append(f"[Project Spec — verbatim requirements]\n{spec}")
        if include_design and preamble_steps:
            design = self._load_fixed_step_docs(project_path, graph_name,
                                                preamble_steps)
            if design:
                parts.append(f"[Project Design]\n{design}")
        return "\n\n".join(parts)

    def _load_fixed_step_docs(self, project_path: Path, graph_name: str,
                              steps: list[str]) -> str:
        """Read the outputs of the given (stable) step ids for a config.

        These step outputs are fixed once their steps complete, so they stay
        byte-stable across all later steps (unlike the growing code repo, which
        agents read on demand via the directory tree + read_file/list_tree). No
        pipeline-specific step ids are hardcoded: the caller supplies them from
        config.
        """
        docs = []
        for step_key in steps:
            step_dir = project_path / graph_name / str(step_key)
            if not step_dir.is_dir():
                continue
            for f in sorted(step_dir.glob("*")):
                if not f.is_file() or f.name == "_snapshot.json" \
                        or f.name.startswith("instruction"):
                    continue
                try:
                    docs.append(f"### {f.name}\n{f.read_text(encoding='utf-8')}")
                except Exception:
                    pass
        return "\n\n".join(docs)

    def _build_workspace_tree(self, project_path: Path, step_id: str,
                              for_red: bool = False, code_path: Path = None) -> str:
        """
        构建工作区目录树摘要，注入到 Agent 提示词中。
        让 Agent 看到确切的文件名，避免浪费工具轮次猜测。

        Green Agent: code repo + Inbox_{step_id}/ + {prev_step}/
        Red Agent:   code repo + {step_id}/

        :param project_path: DPS workspace 根路径
        :param step_id: 当前步骤 ID
        :param for_red: 是否为 Red Agent 构建
        :param code_path: Project 代码仓库路径
        :return: 目录树字符串，为空则返回 ""
        """
        if code_path is None:
            code_path = project_path
        BLOCKED = {".git", "__pycache__", ".venv", "node_modules", ".gitkeep", "_snapshot.json"}
        MAX_DEPTH = 3
        MAX_ENTRIES = 100

        def _tree_lines(directory: Path, label: str) -> list[str]:
            """生成单个目录的树形文本。"""
            if not directory.exists() or not directory.is_dir():
                return []
            entries = []
            count = 0
            for item in sorted(directory.rglob("*")):
                if count >= MAX_ENTRIES:
                    entries.append(f"  ... [truncated at {MAX_ENTRIES} entries]")
                    break
                rel = item.relative_to(directory)
                parts = rel.parts
                if len(parts) > MAX_DEPTH:
                    continue
                if any(p in BLOCKED for p in parts):
                    continue
                indent = "  " * len(parts)
                name = parts[-1]
                if item.is_dir():
                    entries.append(f"{indent}{name}/")
                else:
                    # No file size: it changes on every edit and would bust the
                    # prompt-cache prefix; the agent only needs the filename.
                    entries.append(f"{indent}{name}")
                count += 1
            if not entries:
                return []
            return [f"{label}/"] + [f"  {e}" for e in entries]

        trees = []

        # AT-9: the code-repo tree must be rooted at "." with a clarifying note,
        # NOT a bare "project/" label. Models read "project/" as a real directory
        # and mirror it into write paths (project/pkg/x.py alongside pkg/x.py),
        # producing duplicate/un-importable files. The repo root IS the write base.
        REPO_ROOT_NOTE = ("# repo root (write paths are relative to here, "
                          "e.g. strkit/core.py — do NOT prefix with project/):")

        def _repo_lines() -> list[str]:
            lines = _tree_lines(code_path, ".")
            return ([REPO_ROOT_NOTE] + lines) if lines else []

        if for_red:
            # Red Agent: code repo tree + current step's output dir
            trees.extend(_repo_lines())
            lines = _tree_lines(project_path / DPE_GRAPH_NAME / step_id, f"Step_{step_id}")
            trees.extend(lines)
        else:
            # Green Agent: code repo tree + previous step's final output
            # (Inbox dirs are no longer created — skillflow deprecated them)
            trees.extend(_repo_lines())

            # Previous step's output dir for context
            try:
                if step_id in TASK_STEP_SEQUENCE:
                    seq = TASK_STEP_SEQUENCE
                elif step_id in PROJECT_STEP_SEQUENCE:
                    seq = PROJECT_STEP_SEQUENCE
                else:
                    seq = STEP_SEQUENCE  # legacy fallback
                idx = seq.index(step_id)
                if idx > 0:
                    prev_step = seq[idx - 1]
                    lines = _tree_lines(
                        project_path / DPE_GRAPH_NAME / prev_step,
                        f"Step_{prev_step}"
                    )
                    trees.extend(lines)
            except ValueError:
                pass

        return "\n".join(trees)

    def _load_project_spec(self, project_path: Path) -> str:
        """Load the verbatim requirements spec from {DPS workspace}/project/spec.md.

        This is the raw requirements conversation (un-summarized), preserved by
        ``core/project_submit.py`` so detail the brief omits — e.g. detailed game
        rules — still reaches the agents. Returns "" when absent (simple projects
        carry no separate spec, only the brief).
        """
        spec_file = project_path / "project" / "spec.md"
        if spec_file.exists():
            try:
                return spec_file.read_text(encoding="utf-8").strip()
            except Exception:
                return ""
        return ""

    def _load_project_brief(self, project_path: Path) -> str:
        """
        Load the project brief from {DPS workspace}/project/project_brief.md.
        Also appends any user amendments from checkpoint rejections.
        This is the brief generated by the meta-conversation, stored in the DPE workspace.
        """
        brief_file = project_path / "project" / "project_brief.md"
        content = ""
        if brief_file.exists():
            content = brief_file.read_text(encoding="utf-8")

        # Fallback: some submission paths only leave the meta-conversation
        # draft at <workspace>/draft_brief.json without promoting it to
        # project/project_brief.md. Without this, the researcher gets no brief
        # and guesses the project domain (AT-3). Read the draft as a fallback.
        if not content.strip():
            draft_file = project_path / "draft_brief.json"
            if draft_file.exists():
                try:
                    import json as _json
                    from core.meta_conversation import format_brief_as_markdown
                    draft = _json.loads(draft_file.read_text(encoding="utf-8"))
                    brief = draft.get("brief", draft) if isinstance(draft, dict) else draft
                    content = format_brief_as_markdown(brief)
                except Exception:
                    content = ""

        try:
            if not content.strip():
                return ""
            lines = content.splitlines()
            if len(lines) > 2000:
                content = "\n".join(lines[:2000]) + "\n\n... [brief truncated at 2000 lines]"

            # Append user amendments from checkpoint rejections.
            # These are scope changes explicitly requested by the user that
            # all agents (Green AND Red) must respect.
            amendments_file = project_path / "project" / "user_amendments.md"
            if amendments_file.exists():
                amendments = amendments_file.read_text(encoding="utf-8")
                if amendments.strip():
                    content += (
                        "\n\n---\n# User Amendments (from checkpoint rejections)\n"
                        "The user explicitly requested these changes when rejecting outputs. "
                        "These are NOT scope creep — they are approved additions to the brief:\n\n"
                        + amendments
                    )
            return content
        except Exception:
            return ""

    def _load_project_planning_outputs(self, project_path: Path) -> str:
        """
        Load project-level planning outputs from step directories.
        Kept for backward compat with project-level steps.
        """
        return self._load_step_relevant_context(project_path, step_id=None)

    def _load_step_relevant_context(self, project_path: Path, step_id: str | None = None) -> str:
        """
        Load project-level planning outputs filtered by step relevance.
        Each task-level step only receives the context it actually needs,
        avoiding the critical token waste of loading ALL docs into every prompt.
        """
        # Context relevance map: what each step needs from prior outputs
        # "summary" = first 100 lines, "full" = full content, "interfaces" = API/contract sections only
        CONTEXT_MAP = {
            "t_plan":  {"1": "summary", "2": "full"},
            "t_impl":  {"2": "interfaces"},
            "t_verify": {"1": "summary", "2": "interfaces"},
        }

        # For project-level steps or unmapped steps, load everything (backward compat)
        needs = CONTEXT_MAP.get(step_id, {"1": "full", "2": "full"}) if step_id else {"1": "full", "2": "full"}

        section_labels = {
            "1": "Project SOTA / Codebase Analysis (P1)",
            "2": "Project Architecture (P2)",
        }

        sections = []
        for step_key, mode in needs.items():
            step_dir = project_path / DPE_GRAPH_NAME / step_key
            if not step_dir.exists():
                continue
            for f in step_dir.glob("*"):
                if f.is_file() and f.name != "_snapshot.json" and not f.name.startswith("instruction"):
                    try:
                        content = f.read_text(encoding="utf-8")
                    except Exception:
                        continue

                    if mode == "summary":
                        content = self._summarize(content, max_lines=100)
                    elif mode == "interfaces":
                        content = self._extract_interfaces(content)
                    # "full" = no filtering

                    label = section_labels.get(step_key, f"Step {step_key}")
                    sections.append(f"### {label} — {f.name}\n{content}")

        return "\n\n".join(sections)

    @staticmethod
    def drop_preamble_steps(resolved_context: dict | None,
                            preamble_steps: list[str] | None) -> dict:
        """Drop resolved_context entries already carried by the shared preamble.

        skillflow labels a ``{step: N}`` context source ``"Step N"``. When F2
        hoists those step outputs (design docs) into the preamble, their
        resolved_context copies would duplicate the design in the prompt — so
        remove them. Driven by ``preamble_steps`` (config), no hardcoding.
        """
        if not resolved_context:
            return resolved_context or {}
        drop = {f"Step {s}" for s in (preamble_steps or [])}
        return {k: v for k, v in resolved_context.items() if k not in drop}

    @staticmethod
    def _compact_json(text: str) -> str:
        """Lossless format shrink (Phase 4): minify a pretty-printed JSON blob.

        Re-serializes the SAME data with no indentation / no inter-token spaces,
        which removes pure-whitespace tokens. If the text is not valid JSON it
        is returned unchanged, so this is safe to call on any injected blob.
        """
        import json as _json
        try:
            data = _json.loads(text)
        except (ValueError, TypeError):
            return text
        return _json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def _summarize(self, content: str, max_lines: int = 100) -> str:
        """Extract a summary by keeping first N lines of content."""
        lines = content.splitlines()
        if len(lines) <= max_lines:
            return content
        return "\n".join(lines[:max_lines]) + "\n... [summary truncated]"

    def _extract_interfaces(self, content: str) -> str:
        """
        Extract only interface/API/contract sections from architecture docs.
        Keeps sections whose headers contain: Interface, API, Contract, Function,
        Class, Module, Endpoint, or code blocks with function signatures.
        """
        import re
        lines = content.splitlines()
        result = []
        in_interface_section = False
        section_depth = 0
        interface_keywords = {"interface", "api", "contract", "endpoint", "module boundary",
                              "component", "data flow", "interaction"}

        for i, line in enumerate(lines):
            # Detect markdown headers
            header_match = re.match(r'^(#{1,4})\s+(.*)', line)
            if header_match:
                header_text = header_match.group(2).lower()
                current_depth = len(header_match.group(1))

                if any(kw in header_text for kw in interface_keywords):
                    in_interface_section = True
                    section_depth = current_depth
                    result.append(line)
                elif in_interface_section and current_depth <= section_depth:
                    # New section at same or higher level — stop including
                    in_interface_section = False
                    # Check if this new header is also interface-related
                    if any(kw in header_text for kw in interface_keywords):
                        in_interface_section = True
                        section_depth = current_depth
                        result.append(line)
                elif in_interface_section:
                    result.append(line)
            elif in_interface_section:
                result.append(line)

        if not result:
            # Fallback: keep first 150 lines if no interface sections found
            return "\n".join(lines[:150]) + "\n... [no interface sections found, showing summary]"

        extracted = "\n".join(result)
        if len(extracted.splitlines()) > 500:
            lines = extracted.splitlines()
            extracted = "\n".join(lines[:500]) + "\n... [interfaces truncated]"
        return extracted

    def _load_user_rejection_history(self, project_path: Path, step_id: str) -> str:
        """
        Read user_rejection_history.json from the step's output directory.
        Returns a formatted string with all rejection entries, or empty string.
        """
        import json as _json
        history_file = project_path / DPE_GRAPH_NAME / step_id / "user_rejection_history.json"
        if not history_file.exists():
            return ""

        try:
            history = _json.loads(history_file.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, ValueError):
            return ""

        if not history:
            return ""

        parts = ["[User Rejection History — MUST ADDRESS ALL OF THE FOLLOWING]"]
        for entry in history:
            attempt = entry.get("attempt", "?")
            feedback = entry.get("user_feedback", "")
            parts.append(f"\n--- Rejection #{attempt} ---")
            parts.append(f"User feedback: {feedback}")

        # Include latest output summary if available
        latest = history[-1] if history else None
        if latest and latest.get("rejected_output_summary"):
            parts.append(f"\n[Latest Rejected Output]\n{latest['rejected_output_summary']}")

        return "\n".join(parts)

    def _detect_language_instruction(self, brief: str) -> str:
        """Deprecated in v2: language selection is a UI-level setting.

        Returns empty string — UI will provide language choice.
        """
        return ""
