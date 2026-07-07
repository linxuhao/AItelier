# Technical Architecture Design: DPE Language Instruction Placement Consistency

## Overview

Fix the inconsistent placement of the `[Language]` instruction block across the DPE prompt pipeline. Currently, the user's language preference (from `prompt_assembler`) is not always the final instruction the model sees — in some code paths it's prepended (diluted by following template text), and in others it's followed by a `[Turn Budget]` block that steals the critical "last thing the model reads" position. This design ensures `[Language]` is always the **absolute last block** in the user message across all code paths, maximizing recency-weighted influence on the model's output language.

### Problem Summary

| Location | Current Behavior | Issue |
|---|---|---|
| `meta_conversation.py:183` (`detect_intent`) | `lang_instruction + "\n\n" + system_prompt` | PREPEND — template text follows [Language] |
| `meta_conversation.py:291` (`revise_brief`) | `lang_instruction + "\n\n" + system_prompt` | PREPEND — same issue |
| `meta_conversation.py:232` (`MetaConversationAgent.__init__`) | `system_prompt + "\n\n" + lang_instruction` | APPEND — correct (reference pattern) |
| `meta_conversation.py:431` (`TaskMetaConversationAgent.__init__`) | `system_prompt + "\n\n" + lang_instruction` | APPEND — correct |
| `prompt_assembler.py:392-393` (`assemble()`) | `sections.append(lang_instruction)` at end | Correct within assemble(), but `dpe_pipeline.py` appends [Turn Budget] AFTER this |
| `dpe_pipeline.py:452-458` (JSON retry) | `prompt += turn_budget` after assemble() | [Turn Budget] follows [Language] |
| `dpe_pipeline.py:1177-1192` (native) | `user_prompt += turn_budget` after assemble() | Same issue |
| `dpe_pipeline.py:1200` (system message) | `f"{preamble}\n\n{agent.system_prompt}"` | Preamble ends with [Language], but agent template follows it |
| `meta_agent.py:1073,1082` (`_build_system_prompt`) | `prompt + "\n\n" + lang_block` | APPEND — already correct, no change needed |

## Architecture

### Design Principle: Single Responsibility for [Language] Placement

The `PromptAssembler.assemble()` method currently owns [Language] injection but cannot control its final position because `dpe_pipeline.py` appends `[Turn Budget]` *after* `assemble()` returns. The fix moves [Language] injection responsibility to `dpe_pipeline.py`, which is the only place that knows about both [Language] and [Turn Budget] and can therefore guarantee the correct ordering.

### Data Flow (After Fix)

```
User Language Preference (e.g. "zh-CN")
        │
        ▼
build_language_instruction(user_lang)   ← module-level function in prompt_assembler.py
        │
        ├──▶ meta_conversation.py        ← uses directly (APPEND pattern)
        ├──▶ meta_agent.py               ← uses directly (APPEND pattern, already correct)
        ├──▶ prompt_assembler.py         ← build_shared_preamble() (end of preamble, already correct)
        └──▶ dpe_pipeline.py             ← injects AFTER [Turn Budget] as final user-message block
```

### Component: `core/meta_conversation.py`

**Change**: Two one-line fixes — swap concatenation order from PREPEND to APPEND.

**`detect_intent()` (line 183)**:
```python
# Before (PREPEND — bug):
system_prompt = lang_instruction + "\n\n" + system_prompt

# After (APPEND — fix):
system_prompt = system_prompt + "\n\n" + lang_instruction
```

**`revise_brief()` (line 291)**:
```python
# Before (PREPEND — bug):
system_prompt = lang_instruction + "\n\n" + system_prompt

# After (APPEND — fix):
system_prompt = system_prompt + "\n\n" + lang_instruction
```

**Rationale**: These two functions build a system prompt and then prepend the language instruction. The model reads the language instruction first, then the actual system prompt content (which may contain language-specific examples or be written in a different language). By appending instead, [Language] is the last thing the model processes in the system prompt, giving it maximum recency weight. This is consistent with the already-correct `MetaConversationAgent.__init__()` (line 232) and `TaskMetaConversationAgent.__init__()` (line 431).

**No changes** to `MetaConversationAgent.__init__()` (line 232) or `TaskMetaConversationAgent.__init__()` (line 431) — they already use the correct APPEND pattern.

### Component: `core/prompt_assembler.py`

**Change**: Remove [Language] block from `assemble()` output; the caller is now responsible for injecting it at the correct position.

**`assemble()` method (lines 390-393)**:
```python
# Before:
# [Language Instruction] — at the very END so it never busts the
# prompt-cache prefix when the user changes language mid-project.
if lang_instruction:
    sections.append(lang_instruction)

# After:
# [Language Instruction] — NOT appended here. The caller (dpe_pipeline.py)
# injects it AFTER the [Turn Budget] block so it is the absolute last
# content the model sees, maximizing recency-weighted language override.
# The lang_instruction is still resolved from user_lang above (line 196)
# for use by build_shared_preamble() and for traceability.
```

**Critical constraint**: The `_detect_language_instruction(user_lang)` call on line 196 is kept — it's still referenced by `build_shared_preamble()` (line 417) which remains unchanged. Removing only the append on lines 390-393 is sufficient.

**`build_shared_preamble()` (lines 429-430)**: **No change**. [Language] remains at the end of the system preamble. Having [Language] in both the system preamble and the user message is defense-in-depth — the user-message copy (after turn budget) provides the primary override, and the system-preamble copy provides a secondary reinforcement. The preamble's [Language] placement is already correct (last in the preamble parts list), and it stays byte-stable for prompt-cache purposes since it's resolved from `user_lang` which changes rarely.

### Component: `core/dpe_pipeline.py`

**Change**: Import `build_language_instruction` and append [Language] AFTER [Turn Budget] at all three injection points.

**Injection Point 1 — JSON-mode retry loop (line 449-458)**:
```python
# After appending turn budget + step-control instructions:
prompt += (
    f"\n\n[Turn Budget: {remaining} remaining]\n"
    "Step-control tools available in your tool list:\n"
    ...
)
# NEW: append [Language] as the absolute last block
lang_instruction = build_language_instruction(self._user_lang)
if lang_instruction:
    prompt += "\n\n" + lang_instruction
```

**Injection Point 2 — Native-mode loop (line 1177-1192)**:
```python
# After appending turn budget:
user_prompt += (
    f"\n\n[Turn Budget: {max_turns} turns total, then forced output]\n"
    ...
)
# NEW: append [Language] as the absolute last block
lang_instruction = build_language_instruction(self._user_lang)
if lang_instruction:
    user_prompt += "\n\n" + lang_instruction
```

**Defense-in-Depth — System message (line 1200, optional)**:
```python
# After constructing system_content:
system_content = f"{preamble}\n\n{agent.system_prompt}"
# NEW: re-append [Language] after the agent template so it
# overrides any template-native language in the system message itself.
lang_instruction = build_language_instruction(self._user_lang)
if lang_instruction and not system_content.endswith(lang_instruction):
    system_content += "\n\n" + lang_instruction
```

**Rationale for defense-in-depth**: The preamble ends with [Language], but then the agent template text is appended (line 1200: `f"{preamble}\n\n{agent.system_prompt}"`). This means the template's native language follows [Language] in the system message. By re-appending [Language] after the template, we ensure both the system message AND the user message end with [Language]. The guard `not system_content.endswith(lang_instruction)` prevents double-appending if the template already ends with it.

### Component: `core/meta_agent.py`

**No change**. Verified that `_build_system_prompt()` (lines 1073, 1082) already uses the correct APPEND pattern:
```python
# Line 1073 (coding mode):
prompt = prompt + "\n\n" + lang_block

# Line 1082 (butler mode):
prompt = prompt + "\n\n" + lang_block
```
The `SYSTEM_PROMPT` constant has no hardcoded `[Language]` directive.

### Component: Template Files (`templates/*.md`)

**No change**. Verified that no `.md` template file contains a `[Language]` directive or hardcoded language rule that could counteract the injected instruction. Templates may remain in any language — the `[Language]` instruction overrides them.

## Prompt-Cache Analysis

| What | Cache Impact |
|---|---|
| Remove [Language] from `assemble()` | None — [Language] was in the volatile suffix (past cache boundary); removing it doesn't change the byte-identical prefix |
| Append [Language] after [Turn Budget] in dpe_pipeline.py | None — [Turn Budget] is also in the volatile suffix; the cache-prefix boundary is unchanged |
| Append [Language] after agent template in system message | None — system message is not part of the user-message KV-cache prefix |
| Swap PREPEND→APPEND in meta_conversation.py | None — these are standalone calls, not cached prefixes |

All changes are cache-safe: they only affect content past the prompt-cache prefix boundary (the volatile suffix) or content in separate messages (system message vs. user message).

## Interface Contracts

### `build_language_instruction(user_lang: str | None) -> str`

- **Module**: `core/prompt_assembler` (already exists, no signature change)
- **Input**: Language code (e.g. `"zh-CN"`, `"en"`, `None`)
- **Output**: `[Language]\n...` instruction block string, or `""` if no applicable instruction
- **Behavior**: Unchanged — returns the appropriate `[Language]` block from `_LANG_INSTRUCTIONS` dict, falling back to a generic instruction for unknown codes. When `user_lang` is `None`, defaults to English (`"en"`).
- **Callers after fix**: `meta_conversation.py` (4 locations, all APPEND), `meta_agent.py` (2 locations, already APPEND), `prompt_assembler.py` (`build_shared_preamble()`), `dpe_pipeline.py` (3 new injection points)

### `PromptAssembler.assemble()` contract change

- **Before**: Returns user prompt with `[Language]` as the last section.
- **After**: Returns user prompt **without** `[Language]`. The caller is responsible for appending it at the correct position (after turn budget).
- **Breaking change**: Yes, but all production callers are in `dpe_pipeline.py` and are updated in the same changeset. Test callers will need the same treatment if they assert on `[Language]` presence.

## Error Handling

- If `build_language_instruction()` returns `""` (no applicable language), the guard `if lang_instruction:` at each injection point ensures no empty block is appended.
- If `user_lang` is `None`, `build_language_instruction` defaults to English — agents always have a language directive, never falling back to template language.
- No new failure modes introduced — the function is already called in 6+ existing locations with the same error characteristics.

## Rollback Path

All changes are reversible by reverting the commits:
1. Revert `meta_conversation.py` — swap APPEND back to PREPEND at the two lines
2. Revert `prompt_assembler.py` — restore the `sections.append(lang_instruction)` lines
3. Revert `dpe_pipeline.py` — remove the `build_language_instruction` import and the new append blocks

No data migrations, schema changes, or irreversible operations.

## Implementation Order

| Order | File | Change | Risk |
|---|---|---|---|
| 1 | `core/meta_conversation.py` | Fix PREPEND→APPEND (lines 183, 291) | Minimal — one-character operator swap |
| 2 | `core/prompt_assembler.py` | Remove [Language] from `assemble()` (lines 390-393) | Low — affects only the volatile suffix |
| 3 | `core/dpe_pipeline.py` | Add `build_language_instruction` import; append [Language] after turn budget at JSON retry (line ~458), native loop (line ~1192), and optionally system message (line ~1201) | Low — new appends in volatile suffix |
| 4 | Tests | Update any test that asserts on [Language] position in `assemble()` output | Low — test-only |

## Verification Checklist

- [ ] `detect_intent()` appends lang_instruction (not prepends)
- [ ] `revise_brief()` appends lang_instruction (not prepends)
- [ ] `MetaConversationAgent.__init__()` still appends (unchanged, verify)
- [ ] `TaskMetaConversationAgent.__init__()` still appends (unchanged, verify)
- [ ] `assemble()` no longer contains [Language] in output
- [ ] JSON-mode retry loop: [Language] is the LAST block after [Turn Budget]
- [ ] Native-mode loop: [Language] is the LAST block after [Turn Budget]
- [ ] System message (optional): [Language] follows agent template text
- [ ] `_build_system_prompt()` in meta_agent.py already correct (verify, no change)
- [ ] No template `.md` file contains conflicting `[Language]` directive
- [ ] Prompt-cache prefix remains byte-identical across steps
