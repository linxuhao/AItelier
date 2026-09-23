"""Named-mutation catalog for the "a gate that never ran is not a failing
gate" card (rev 8).  Every entry is ONE concrete edit against a REAL file:
an anchor that must match exactly once, and the replacement.  The 7 goal-table
mutations (N9, M21, M21b, G2, G2b, DUPIMPL, RESTART) are written VERBATIM per
the card's table — no self-authored "equivalent" stands in for them.

`run_mutations.py` applies each to a copy of the tree, checks the anchor hit
exactly once, runs the targeted tests and the full ``tests/unit tests/skillflow``
suite, reads the BARE exit code, records the tests that went red, and restores
the tree.  A mutation whose anchor does not hit exactly once is an ERROR, not a
kill (the card forbids counting a zero-hit anchor as killed).

Each edit is a list of ``{anchor, replacement}`` hunk(s) so a mutation can be
more than a one-line change (RESTART persists the ledger, which needs a global
plus the field)."""

#: name -> { "edits": [{file, anchor, replacement}], "targeted": [test ids] }
MUTATIONS = {
    # ── goal table, verbatim ──────────────────────────────────────────────
    "N9": {
        "file_note": "tool.yaml:52-56 — delete the sentence that states a "
                     "no-verdict gate does not loop back to implement.",
        "edits": [{
            "file": "aitelier/tools/run_tests/tool.yaml",
            "anchor":
                "  A gate that produced NO VERDICT does not loop back to "
                "`implement`: its\n"
                "  absence is stated on the report, with\n"
                "  `repo_gate_unmeasured: true` and `repo_gate_absent: true` "
                "(plus\n"
                "  `repo_gate.measured: \"unmeasured\"` and "
                "`repo_gate.attempts`, the number of\n"
                "  runs the reading cost), and "
                "`configs/coding_impl.yaml` routes\n",
            "replacement": "",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop",
        ],
    },
    "M21": {
        "file_note": "_unmeasured_declaration: startswith -> `in`; the "
                     "line[len(prefix):] slice is LEFT UNCHANGED (verbatim).",
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "        if not line.startswith(_REPO_GATE_UNMEASURED_PREFIX):\n",
            "replacement": "        if not (_REPO_GATE_UNMEASURED_PREFIX in line):\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_mid_line_echo_kills_the_in_operator_on_the_declaration_channel",
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_log_echo_of_the_prefix_mid_line_is_not_a_declaration",
        ],
    },
    "M21b": {
        "file_note": "_repo_gate_failure_cases: startswith -> `in`; slice "
                     "unchanged (verbatim).",
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "        if not line.startswith(_REPO_GATE_CASE_PREFIX):\n",
            "replacement": "        if not (_REPO_GATE_CASE_PREFIX in line):\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_mid_line_echo_kills_the_in_operator_on_the_case_channel",
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_log_echo_of_the_case_prefix_mid_line_is_not_a_case_record",
        ],
    },
    "M21PAIR": {
        "file_note": "The OBSERVABLE half of the M21 family: `in` AND the "
                     "`line.index(prefix)` slice together. Literal M21 is "
                     "behaviourally inert (see the inertness witness); this "
                     "pair is what a test can turn red on the declaration "
                     "channel.",
        "edits": [
            {
                "file": "aitelier/tools/run_tests/impl.py",
                "anchor": "        if not line.startswith(_REPO_GATE_UNMEASURED_PREFIX):\n",
                "replacement": "        if not (_REPO_GATE_UNMEASURED_PREFIX in line):\n",
            },
            {
                "file": "aitelier/tools/run_tests/impl.py",
                "anchor": "        raw = line[len(_REPO_GATE_UNMEASURED_PREFIX):]\n",
                "replacement": "        raw = line[line.index(_REPO_GATE_UNMEASURED_PREFIX) + len(_REPO_GATE_UNMEASURED_PREFIX):]\n",
            },
        ],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_mid_line_echo_kills_the_in_operator_on_the_declaration_channel",
        ],
    },
    "M21BPAIR": {
        "file_note": "The observable pair on the case channel (M21b + its slice).",
        "edits": [
            {
                "file": "aitelier/tools/run_tests/impl.py",
                "anchor": "        if not line.startswith(_REPO_GATE_CASE_PREFIX):\n",
                "replacement": "        if not (_REPO_GATE_CASE_PREFIX in line):\n",
            },
            {
                "file": "aitelier/tools/run_tests/impl.py",
                "anchor": "        raw = line[len(_REPO_GATE_CASE_PREFIX):]\n",
                "replacement": "        raw = line[line.index(_REPO_GATE_CASE_PREFIX) + len(_REPO_GATE_CASE_PREFIX):]\n",
            },
        ],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_mid_line_echo_kills_the_in_operator_on_the_case_channel",
        ],
    },
    "G2": {
        "file_note": "gate_deferral module constant GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9.",
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "GATE_DEFERRAL_EPISODE_MAX_SECONDS = float(\n"
                      "    os.getenv(\"AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS\", \"10800\"))\n",
            "replacement": "GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py::test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded",
        ],
    },
    "G2b": {
        "file_note": "gate_deferral module constant GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9.",
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "GATE_DEFERRAL_EPISODE_MAX_CEILING = float(\n"
                      "    os.getenv(\"AITELIER_GATE_DEFERRAL_EPISODE_MAX_CEILING\", str(6 * 3600)))\n",
            "replacement": "GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py::test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded",
        ],
    },
    "DUPIMPL": {
        "file_note": "write the `_ERROR_RE = ...` line a second time, "
                     "immediately after itself.",
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n",
            "replacement": "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n"
                           "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n",
        }],
        "targeted": [
            "tests/unit/test_no_verbatim_previous_line_dup.py::test_no_verbatim_previous_line_dup_in_non_test_code",
        ],
    },
    "RESTART": {
        "file_note": "persist the deferral ledger so a restarted in-process "
                     "ledger re-reads the episode — the behaviour the deleted "
                     "`episode_seconds(...) == 0.0` assert guarded.",
        "edits": [
            {
                "file": "core/gate_deferral.py",
                "anchor": "@dataclass\nclass DeferralLedger:",
                "replacement": "# RESTART mutation: a process-wide store every fresh\n"
                               "# ledger reads back, i.e. the ledger IS persisted.\n"
                               "_PERSISTED_EPISODES: dict = {}\n\n\n@dataclass\n"
                               "class DeferralLedger:",
            },
            {
                "file": "core/gate_deferral.py",
                "anchor": "    episodes: dict[str, _Episode] = field(default_factory=dict)\n",
                "replacement": "    episodes: dict[str, _Episode] = field("
                               "default_factory=lambda: _PERSISTED_EPISODES)\n",
            },
        ],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py::test_the_deferral_ledger_is_not_persisted_so_a_restart_re_measures",
        ],
    },
    # ── previously-killed named mutations, anchored to real source ─────────
    "M2": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "                \"unmeasured_declaration\": _unmeasured_declaration(out)}\n",
            "replacement": "                \"unmeasured_declaration\": _unmeasured_declaration(out[-2000:])}\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_declaration_survives_output_far_past_the_retention_bound",
        ],
    },
    "M19": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "                \"unmeasured_declaration\": _unmeasured_declaration(out)}\n",
            "replacement": "                \"unmeasured_declaration\": None}\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_declared_absence_is_unmeasured_whatever_the_exit_code_was",
        ],
    },
    "TAILONLY2": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    if gate.get(\"output_truncated\"):\n        # Only a fragment was retained",
            "replacement": "    if False:\n        # Only a fragment was retained",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_truncated_gate_with_no_record_at_all_is_a_red",
        ],
    },
    "RC2": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    return (REPO_GATE_MEASURED_PASS if gate.get(\"returncode\") == 0\n            else REPO_GATE_MEASURED_FAIL)\n",
            "replacement": "    return (REPO_GATE_MEASURED_PASS if gate.get(\"returncode\") == 0\n            else REPO_GATE_UNMEASURED)\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_no_exit_code_alone_is_ever_unmeasured",
        ],
    },
    "TIMEOUT_NOT_UNMEASURED": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    if gate.get(\"timed_out\") is True or gate.get(\"runner_error\") is True:\n",
            "replacement": "    if False:\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py::test_a_killed_gate_is_unmeasured_and_its_returncode_is_not_why",
        ],
    },
    "VALVEBYPASS": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return claims > max_claims\n",
            "replacement": "    if ledger is not None and ledger.deferring(run_id, now=now):\n        return False\n    return claims > max_claims\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
            "tests/unit/test_gate_deferral_is_accounted.py",
        ],
    },
    "VALVEREACH": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return claims > max_claims\n",
            "replacement": "    return False\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
            "tests/unit/test_gate_deferral_is_accounted.py",
        ],
    },
    "ABSCEIL": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "_ABSOLUTE_EPISODE_CEILING = 6 * 3600.0\n",
            "replacement": "_ABSOLUTE_EPISODE_CEILING = 1e9\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "H2": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    book = LEDGER if ledger is None else ledger\n    return book.deferring(run_id, now=now)\n",
            "replacement": "    book = LEDGER if ledger is None else ledger\n    return False\n",
        }],
        "targeted": [
            "tests/skillflow/test_coding_impl_gate_absence.py",
            "tests/skillflow/test_coding_impl_gate_absence.py::test_the_deferral_hold_leaves_real_step_rows_untouched",
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "ABSTERM": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "ABSENCE_TERMINAL = \"gate did not run: no verdict was measured\"\n",
            "replacement": "ABSENCE_TERMINAL = \"gate failed: the code did not pass\"\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "ABSTERM2": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "        return f\"{ABSENCE_TERMINAL} ({gate}, {self.episode_count(run_id)} attempt(s))\"\n",
            "replacement": "        return (\"Cycle limit exceeded: the code \"\n                \"regressed and gate did not run: no verdict was measured\")\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "ABSENCEWORD": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "_FORBIDDEN_WORDS = (\"failed\", \"failure\", \"fail\", \"regression\", \"red\", \"error\")\n",
            "replacement": "_FORBIDDEN_WORDS = ()\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "READABSENCE": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    if not (data.get(\"repo_gate_absent\") or data.get(\"repo_gate_unmeasured\")):\n        return None\n",
            "replacement": "    if False:\n        return None\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_is_accounted.py",
        ],
    },
    "ABSREL": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    report[\"passed_relative\"] = (state in BASELINE_MEASURED\n                                 and not absent\n                                 and not report[\"new_failures\"])\n",
            "replacement": "    report[\"passed_relative\"] = (state in BASELINE_MEASURED\n                                 and not report[\"new_failures\"])\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py",
        ],
    },
    "ABSSTATE": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "        if absent and (path is None or not path.is_file()):\n            state = \"unmeasured\"\n",
            "replacement": "        pass\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py",
        ],
    },
    "ABSWRITE": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    if path is None or state in (\"unreadable\", \"unmeasured\"):\n        return\n",
            "replacement": "    if path is None or state == \"unreadable\":\n        return\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py",
        ],
    },
    "RETFLAG": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "            \"repo_gate_absent\": bool(report.get(\"repo_gate_absent\")),\n",
            "replacement": "            \"repo_gate_absent\": False,\n",
        }],
        "targeted": [
            "tests/skillflow/test_config_loading.py",
            "tests/skillflow/test_coding_impl_gate_absence.py",
        ],
    },
    "ATTEMPTS3": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "REPO_GATE_UNMEASURED_ATTEMPTS = 1\n",
            "replacement": "REPO_GATE_UNMEASURED_ATTEMPTS = 3\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "TIMEOUTBIG": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "REPO_GATE_TIMEOUT = 5400\n",
            "replacement": "REPO_GATE_TIMEOUT = 1e9\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "WAITCLAMP": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0),\n               max(1.0, _positive_seconds(GATE_DEFERRAL_WAIT_MAX, 900.0)))\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0)\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "CEILCLAMP": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0),\n               ceiling)\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0)\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "G1b": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "GATE_DEFERRAL_WAIT_MAX = float(\n    os.getenv(\"AITELIER_GATE_DEFERRAL_WAIT_MAX\", \"900\"))\n",
            "replacement": "GATE_DEFERRAL_WAIT_MAX = 1e9\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "G3": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0),\n               ceiling)\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0)\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "G4": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0),\n               max(1.0, _positive_seconds(GATE_DEFERRAL_WAIT_MAX, 900.0)))\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0)\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "S1": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "REPO_GATE_TIMEOUT = 5400\n",
            "replacement": "REPO_GATE_TIMEOUT = 60\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "ABSSEED": {
        "edits": [{
            "file": "aitelier/tools/run_tests/impl.py",
            "anchor": "    if (path is not None and not path.is_file() and state == \"compared\"\n            and not absent):\n",
            "replacement": "    if (path is not None and not path.is_file() and state == \"compared\"):\n",
        }],
        "targeted": [
            "tests/unit/test_run_tests_unmeasured_declaration.py",
        ],
    },
    "D1": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    if book.expired(run_id, now=moment):\n",
            "replacement": "    if False:\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_execution_points.py",
        ],
    },
    "D4": {
        "edits": [{
            "file": "core/gate_deferral.py",
            "anchor": "    if not isinstance(data, dict):\n        return None\n",
            "replacement": "    pass\n",
        }],
        "targeted": [
            "tests/unit/test_gate_deferral_is_accounted.py",
        ],
    },
    "DUPYIELD": {
        "edits": [{
            "file": "tests/unit/test_no_verbatim_previous_line_dup.py",
            "anchor": "    return non_test, test\n",
            "replacement": "    return non_test, test\n    return non_test, test\n",
        }],
        "targeted": [
            "tests/unit/test_no_verbatim_previous_line_dup.py::test_the_checker_file_is_clean_under_its_own_rule",
        ],
    },
    "DUPHITS": {
        "edits": [{
            "file": "tests/unit/test_no_verbatim_previous_line_dup.py",
            "anchor": "    assert non_test == []\n",
            "replacement": "    assert non_test == []\n    assert non_test == []\n",
        }],
        "targeted": [
            "tests/unit/test_no_verbatim_previous_line_dup.py::test_the_checker_file_is_clean_under_its_own_rule",
        ],
    },
}

FULL_SCOPE = ["tests/unit", "tests/skillflow"]
