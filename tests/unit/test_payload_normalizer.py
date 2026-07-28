"""A model that did the work must not have it discarded over the envelope.

Every shape here was taken from a trace of a real step that produced correct output
and was told it had produced nothing. `gen_mcp_server_builder`'s test-authoring step,
four attempts, all four dropped:

    1  {"read_file": {"file_path": "src/wordfreq/server.py"}}
    2  {"thought": "...", "action": "read_file", "path": "src/wordfreq/server.py"}
    3  {"file": "tests/test_tools.py", "content": "\"\"\"Tests for all tools...\"\"\""}
    4  {"file_path": "tests/test_tools.py", "content": "..."}

Attempts 3 and 4 contained the entire pytest suite. Attempts 1 and 2 were the agent
asking to read the module first — a reasonable request that was never executed, so the
retry feedback ("Nothing was written") answered a question it had not asked.
"""
import json

from core.dpe_pipeline import PipelineEngine

norm = PipelineEngine._normalize_payload
SCHEMAS = {"read_file": {}, "write": {}, "create": {}, "edit": {}, "finish_step": {}}


class TestSingleFileEnvelope:
    def test_file_and_content(self):
        p, e = norm({"file": "tests/test_tools.py", "content": "import pytest\n"}, SCHEMAS)
        assert p["files"] == {"tests/test_tools.py": "import pytest\n"}
        assert e["pattern"] == "single-file-envelope"

    def test_file_path_and_content(self):
        p, _ = norm({"file_path": "a/b.py", "content": "x"}, SCHEMAS)
        assert p["files"] == {"a/b.py": "x"}

    def test_path_and_content_with_thoughts_alongside(self):
        p, _ = norm({"thoughts": "writing it", "path": "a.md", "content": "# hi"}, SCHEMAS)
        assert p["files"] == {"a.md": "# hi"}

    def test_empty_content_is_still_a_file(self):
        """An intentionally empty file is a delivery, not a no-op."""
        p, _ = norm({"path": "__init__.py", "content": ""}, SCHEMAS)
        assert p["files"] == {"__init__.py": ""}

    def test_content_is_never_treated_as_a_filename(self):
        """The bare-key rule would otherwise write a file called `content`."""
        p, _ = norm({"path": "a.md", "content": "# hi"}, SCHEMAS)
        assert "content" not in p["files"]


class TestBareToolCall:
    def test_tool_name_as_the_key(self):
        p, e = norm({"read_file": {"file_path": "src/s.py"}}, SCHEMAS)
        assert p["actions"] == [{"tool": "read_file", "params": {"file_path": "src/s.py"}}]
        assert e["pattern"] == "bare-tool-call"

    def test_action_field_with_flat_params(self):
        p, _ = norm({"thought": "read first", "action": "read_file",
                     "path": "src/s.py"}, SCHEMAS)
        assert p["actions"] == [{"tool": "read_file", "params": {"path": "src/s.py"}}]

    def test_an_unknown_tool_name_is_not_invented_into_an_action(self):
        p, e = norm({"summon_daemon": {"x": 1}}, SCHEMAS)
        assert not p.get("actions") and e is None

    def test_step_control_tools_are_recognised_without_a_schema(self):
        p, _ = norm({"finish_step": {"summary": "done"}}, {})
        assert p["actions"][0]["tool"] == "finish_step"


class TestItLeavesCanonicalShapesAlone:
    def test_a_files_payload_is_untouched(self):
        src = {"files": {"a.md": "x"}}
        p, e = norm(dict(src), SCHEMAS)
        assert p == src and e is None

    def test_an_actions_payload_is_untouched(self):
        src = {"actions": [{"tool": "write", "params": {"file": "a.md", "content": "x"}}]}
        p, e = norm(dict(src), SCHEMAS)
        assert p == src and e is None

    def test_pure_thoughts_still_fall_through_to_another_turn(self):
        """Not every payload is a delivery — a thinking turn must stay a thinking
        turn, or the loop loses its message path."""
        p, e = norm({"thoughts": "considering the options"}, SCHEMAS)
        assert not p.get("files") and not p.get("actions") and e is None


class TestBareFilenameKeys:
    def test_dotted_keys_become_files(self):
        p, e = norm({"README.md": "# hi", "setup.py": "x"}, SCHEMAS)
        assert set(p["files"]) == {"README.md", "setup.py"}
        assert e["pattern"] == "bare-filename-keys"

    def test_prose_metadata_keys_are_not_mistaken_for_files(self):
        p, e = norm({"reasoning": "because", "summary": "did a thing"}, SCHEMAS)
        assert not p.get("files") and e is None


class TestMutationToolClassification:
    """`create` and `edit` ARE write tools. Classifying by name prefix was not.

    A `mode: write` step gets `create` / `edit` / `write` / `finish_step` injected by
    skillflow, and the forge palette teaches agents to call exactly those. The engine
    recognised only `write` and the `write_*` / `create_*` / `append_*` slot forms, so
    `create(file, content)` was neither a write, nor a read, nor an unknown write — it
    was dropped without a word. Live trace, four consecutive attempts of one step, each
    carrying the complete file:

        {"create": {"file_name": "tests/test_tools.py", "content": "..."}}
        {"action": "read_file", "file": "src/word_frequency/server.py"}
        {"tool_use": "write", "path": "tests/test_tools.py", "content": "..."}
        {"action": "create", "arguments": {"path": "...", "content": "..."}}

    Every one answered with "Nothing matching '*' was written."
    """
    WRITE_MODE = {"read_file": {}, "create": {}, "edit": {}, "write": {},
                  "finish_step": {}}
    CONTENT_MODE = {"read_file": {}, "create_verdict": {}, "write_verdict": {},
                    "finish_step": {}}

    def _is(self, name, schemas):
        return PipelineEngine._is_mutation_tool(name, schemas)

    def test_create_and_edit_are_writes_in_write_mode(self):
        assert self._is("create", self.WRITE_MODE)
        assert self._is("edit", self.WRITE_MODE)
        assert self._is("write", self.WRITE_MODE)

    def test_slot_tools_are_writes_in_content_mode(self):
        assert self._is("create_verdict", self.CONTENT_MODE)
        assert self._is("write_verdict", self.CONTENT_MODE)

    def test_read_and_step_control_are_not_writes(self):
        assert not self._is("read_file", self.WRITE_MODE)
        assert not self._is("finish_step", self.WRITE_MODE)

    def test_a_tool_the_step_does_not_have_is_not_executed(self):
        """`create` in a CONTENT-mode step is an invented name — it must fall
        through to the unknown-write branch that tells the agent what it may call,
        not be executed against a step that has no such tool."""
        assert not self._is("create", self.CONTENT_MODE)
        assert not self._is("write_file", self.WRITE_MODE)

    def test_the_empty_schema_case_is_not_a_write(self):
        assert not self._is("create", {})
        assert not self._is("", self.WRITE_MODE)


class TestTheDefectLivesInTheHandlerNotTheDispatcher:
    """Where the `create`-is-not-a-write defect had to be fixed, and where it did not.

    A `mode: write` step without `allow_full_write` has schemas
    `[read_file, create, edit, finish_step, read, search, list]` — no `write` — so the
    dispatcher's prefix test sends it to `_run_tool_step`. That is where EVERY
    write-mode step in this repo has always gone (26 of them, including dpe_default's
    `t_impl` and pipeline_forge's own `emit_graph`), and that handler writes fine.

    The bug was inside it: its `write_calls` filter used the same prefix test, so a
    correctly-formed `create` call was neither a write, nor a read, nor an unknown
    write, and was dropped without a word. Fixing the filter fixes the defect and
    leaves the routing of all 26 steps exactly as it was; making the DISPATCHER
    mutation-aware would have re-routed the entire system to fix one handler.
    """
    WRITE_MODE_NO_GENERIC = {"read_file": {}, "create": {}, "edit": {},
                             "finish_step": {}, "read": {}, "search": {}, "list": {}}

    def test_create_and_edit_are_writes_to_the_handler(self):
        ts = self.WRITE_MODE_NO_GENERIC
        assert PipelineEngine._is_mutation_tool("create", ts)
        assert PipelineEngine._is_mutation_tool("edit", ts)

    def test_the_dispatcher_predicates_are_left_alone(self):
        """Regression guard: this step must keep routing to `_run_tool_step`."""
        ts = self.WRITE_MODE_NO_GENERIC
        has_write = any(k.startswith("write") for k in ts)
        has_read = any(not k.startswith("write") and k != "write" for k in ts)
        assert has_write is False and has_read is True   # → the `else` branch


class TestActionContainerAliases:
    """`actions` has as many spellings as the file envelope does.

    Both observed live in `gen_mcp_server_builder`'s test-authoring step, each
    carrying a complete pytest suite, each discarded:

        {"tools": [{"tool": "write_file", "args": {"file": ..., "content": ...}}]}
        {"command": "create", "path": "tests/test_tools.py", "content": "..."}

    Canonicalising the container and per-call keys matters even when the tool NAME
    is wrong (`write_file` does not exist): only once the call reaches `actions`
    does the unknown-write branch fire and tell the agent what it may actually call.
    Dropped silently, it learns nothing and repeats the same guess.
    """
    SCHEMAS = {"read_file": {}, "create": {}, "edit": {}, "finish_step": {}}

    def test_tools_array_becomes_actions(self):
        p, _ = norm({"tools": [{"tool": "write_file",
                                "args": {"file": "t.py", "content": "x"}}]}, self.SCHEMAS)
        assert p["actions"] == [{"tool": "write_file",
                                 "params": {"file": "t.py", "content": "x"}}]

    def test_tool_calls_array_becomes_actions(self):
        p, _ = norm({"tool_calls": [{"name": "create",
                                     "arguments": {"file": "a.md", "content": "x"}}]},
                    self.SCHEMAS)
        assert p["actions"][0]["tool"] == "create"
        assert p["actions"][0]["params"] == {"file": "a.md", "content": "x"}

    def test_an_unknown_tool_name_still_reaches_the_actions_list(self):
        """So the unknown-write branch can teach, instead of silence."""
        p, _ = norm({"tools": [{"tool": "write_file", "args": {}}]}, self.SCHEMAS)
        assert p["actions"][0]["tool"] == "write_file"

    def test_a_canonical_actions_payload_is_untouched(self):
        src = {"actions": [{"tool": "create", "params": {"file": "a", "content": "b"}}]}
        p, e = norm(json.loads(json.dumps(src)), self.SCHEMAS)
        assert p == src and e is None

    def test_non_dict_entries_do_not_crash_the_pass(self):
        p, _ = norm({"tools": ["nonsense", {"tool": "create", "args": {"file": "a"}}]},
                    self.SCHEMAS)
        assert p["actions"][1]["params"] == {"file": "a"}


class TestTheSingleFileEnvelopeAlsoYieldsAnAction:
    """`_run_tool_step` reads only `actions` and ignores `files` entirely.

    It is the handler every `mode: write` step in this repo uses, so normalising
    `{path, content}` to `files` alone left the delivery in a dead end there.
    """
    def test_an_action_is_emitted_using_a_tool_the_step_has(self):
        p, _ = norm({"command": "create", "path": "t.py", "content": "x"},
                    {"create": {}, "edit": {}, "read_file": {}})
        assert p["files"] == {"t.py": "x"}
        assert p["actions"] == [{"tool": "create",
                                 "params": {"file": "t.py", "content": "x"}}]

    def test_it_falls_back_to_write_when_create_is_absent(self):
        p, _ = norm({"path": "t.py", "content": "x"}, {"write": {}, "read_file": {}})
        assert p["actions"][0]["tool"] == "write"

    def test_no_action_is_invented_when_the_step_has_no_mutator(self):
        p, _ = norm({"path": "t.py", "content": "x"}, {"read_file": {}})
        assert p["files"] == {"t.py": "x"}
        assert not p.get("actions")


class TestTheAliasGuardDoesNotEatContent:
    """`tools` is also a perfectly ordinary CONTENT key.

    The spec-writing step in this very pipeline emits
    `{"tools": [{"name": "word_frequency", "description": ...}]}` as its output.
    Converting that to actions would invent a call to a tool named
    `word_frequency`, produce no writes, and fail the step — trading one silent
    discard for another. Convert only when an entry names a tool the step can call.
    """
    SCHEMAS = {"read_file": {}, "create": {}, "edit": {}, "finish_step": {}}

    def test_a_spec_listing_tools_is_left_as_content(self):
        payload = {"tools": [{"name": "word_frequency",
                              "description": "top_n most frequent words"}]}
        p, e = norm(dict(payload), self.SCHEMAS)
        assert not p.get("actions"), p
        assert p["tools"] == payload["tools"]

    def test_a_real_call_is_still_converted(self):
        p, _ = norm({"tools": [{"tool": "create",
                                "args": {"file": "a.py", "content": "x"}}]},
                    self.SCHEMAS)
        assert p["actions"][0]["tool"] == "create"

    def test_an_unknown_tool_name_with_ARGS_is_still_a_call(self):
        """The live case: `{"tool": "write_file", "args": {...}}` where `write_file`
        does not exist. It must reach `actions` so the unknown-write branch can tell
        the agent what it may actually call — a content record has `description`,
        not `args`, which is what separates the two."""
        p, _ = norm({"tools": [{"tool": "write_file",
                                "args": {"file": "t.py", "content": "x"}}]},
                    self.SCHEMAS)
        assert p["actions"][0]["tool"] == "write_file"

    def test_a_mixed_list_converts_when_any_entry_is_a_real_call(self):
        p, _ = norm({"tools": [{"name": "notes"},
                               {"tool": "create", "args": {"file": "a", "content": "b"}}]},
                    self.SCHEMAS)
        assert p["actions"][1]["tool"] == "create"


class TestTheEnvelopeDoesNotOverruleTheAgent:
    """A tool the agent NAMED must not be silently swapped for another.

    Live sequence, all three turns of one step:

        turn 1  {"file_path": "tests/test_tools.py", "content": …}
                → normalised to `create` → "already exists — use 'edit'"
        turn 2  {"tool": "edit", "file_path": …, "content": …}   ← agent self-corrected
                → normalised to `create` AGAIN → identical error
        turn 3  …

    The agent read the error and did the right thing, and this rule overruled it —
    the same produce-then-discard pattern, introduced by the fix for it.
    """
    SCHEMAS = {"read_file": {}, "create": {}, "edit": {}, "finish_step": {}}

    def test_an_explicit_tool_is_honoured(self):
        p, _ = norm({"tool": "edit", "file_path": "t.py", "content": "x"}, self.SCHEMAS)
        assert p["actions"][0]["tool"] == "edit"

    def test_action_and_command_spellings_too(self):
        for key in ("action", "command"):
            p, _ = norm({key: "edit", "path": "t.py", "content": "x"}, self.SCHEMAS)
            assert p["actions"][0]["tool"] == "edit", key

    def test_it_still_defaults_when_no_tool_is_named(self):
        p, _ = norm({"path": "t.py", "content": "x"}, self.SCHEMAS)
        assert p["actions"][0]["tool"] == "create"

    def test_a_named_tool_the_step_lacks_falls_back_to_a_real_one(self):
        """`write_file` does not exist here; do not emit a call that cannot run."""
        p, _ = norm({"tool": "write_file", "path": "t.py", "content": "x"}, self.SCHEMAS)
        assert p["actions"][0]["tool"] == "create"

    def test_the_file_is_still_delivered_either_way(self):
        p, _ = norm({"tool": "edit", "file_path": "t.py", "content": "x"}, self.SCHEMAS)
        assert p["files"] == {"t.py": "x"}

    def test_a_named_READ_tool_does_not_become_the_writer(self):
        """The envelope carries a file body; emitting `read_file(content=…)` would be
        a call that cannot do what the payload plainly intends."""
        p, _ = norm({"tool": "read_file", "path": "t.py", "content": "x"},
                    {"read_file": {}, "create": {}, "edit": {}})
        assert p["actions"][0]["tool"] == "create"


class TestTheFlatCallFormNamesItsToolAnyWay:
    """`tool` is the most natural spelling of all, and it was the one missing.

    Live, turn 7 of a step that had already been told twice what to do: the agent
    read the file to obtain `old_str` and returned

        {"tool": "edit", "path": "tests/test_tools.py",
         "old_str": "...", "new_str": "..."}

    — a perfectly formed `edit` call. It was dropped, because only `action` was
    recognised for the flat form. No `content` key, so the single-file envelope did
    not match either; the payload fell through every rule.
    """
    SCHEMAS = {"read_file": {}, "create": {}, "edit": {}, "finish_step": {}}

    def test_tool_key_flat_form(self):
        p, e = norm({"tool": "edit", "path": "t.py",
                     "old_str": "a", "new_str": "b"}, self.SCHEMAS)
        assert p["actions"] == [{"tool": "edit",
                                 "params": {"path": "t.py", "old_str": "a",
                                            "new_str": "b"}}]
        assert e["pattern"] == "bare-tool-call"

    def test_action_key_still_works(self):
        p, _ = norm({"action": "read_file", "path": "x"}, self.SCHEMAS)
        assert p["actions"][0]["tool"] == "read_file"

    def test_the_name_key_is_not_passed_through_as_a_param(self):
        p, _ = norm({"tool": "edit", "path": "t.py", "old_str": "a"}, self.SCHEMAS)
        assert "tool" not in p["actions"][0]["params"]

    def test_an_unknown_name_is_not_turned_into_a_call(self):
        p, e = norm({"tool": "summon_daemon", "path": "t.py"}, self.SCHEMAS)
        assert not p.get("actions") and e is None
