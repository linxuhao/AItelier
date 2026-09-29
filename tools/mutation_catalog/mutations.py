"""Named-mutation catalog for the "a gate that never ran is not a failing
gate" card. Every entry is ONE concrete edit (one or more hunks) against a REAL
file: an anchor that must match exactly once, and its replacement.

What is in here, and where each name comes from:

* the goal table, VERBATIM (`GOAL_TABLE`: N9, M21, M21b, G2, G2b, DUPIMPL,
  RESTART). Only their `targeted` selections are this catalog's choice;
* the nine names round 8 re-used for other edits (S1, VALVEREACH, ABSTERM,
  ABSTERM2, D1, D4, M2, M19, TAILONLY2), restored to the edits an independent
  review defined them with, minus the review's ignition counter call (the
  runner counts ignition itself); SCHEDROWRETRY and SCHEDROWSTATUS are that
  review's edits too;
* every other name, with the edit it was entered in this catalog with. A
  different edit gets a different name: round 8's edits for the nine names
  now carry new names (TIMEOUT60, ABSTERMTEXT, TERMREASONCYCLE, VALVEOFF,
  NEVEREXPIRE, READNONDICT, DECLNONE), except where one is the same edit as a
  restored name (round 8's "M2" is TAILONLY2's edit, round 8's "TAILONLY2" is
  M2's). TICK_HOLD_INERT edits `core/scheduler.py`, as the round-10 brief
  requires; its round-9 edit, in `gate_deferral.observe_run`, is OBSERVENONE.

`run_mutations.py` applies each entry to its own detached `git worktree` of
the committed tree, checks every anchor hits exactly once BEFORE editing (a
miss is an anchor error, never a kill), runs the `targeted` selection and
`FULL_SCOPE`, and records bare exit codes, ignition and red tests.

`kind` is "behaviour" unless stated: a red test that read the mutated file's
source text and executed none of the mutated lines is then a source-text
witness, not a killer. "text" marks an edit whose guarded property IS the
text (a duplicated line, a deleted contract sentence), where the reader of
the text is the witness.
"""

IMPL = "aitelier/tools/run_tests/impl.py"
SCHED = "core/scheduler.py"
HOST = "core/skillflow_host.py"
GD = "core/gate_deferral.py"
CHK = "tests/unit/test_no_verbatim_previous_line_dup.py"

DECL = "tests/unit/test_run_tests_unmeasured_declaration.py"
ABSENCE = "tests/skillflow/test_coding_impl_gate_absence.py"
EXEC_POINTS = "tests/unit/test_gate_deferral_execution_points.py"
ACCOUNTED = "tests/unit/test_gate_deferral_is_accounted.py"

GOAL_TABLE = ("N9", "M21", "M21b", "G2", "G2b", "DUPIMPL", "RESTART")

_TICK_SILENT = "    if deferral[\"state\"] == \"silent\":\n"


def _sched_row_charge(column_sql):
    """The review's SCHEDROW* shape: the tick's silent branch charges the
    parked `test` row while it holds the run."""
    return [{
        "file": SCHED,
        "anchor": _TICK_SILENT,
        "replacement": _TICK_SILENT +
        "        sf._conn.execute(\"UPDATE skillflow_steps SET " + column_sql +
        " WHERE run_id=? AND step_id='test'\", (run_id,))\n",
    }]


#: name -> {"edits": [{file, anchor, replacement}], "targeted": [test ids],
#:          optional "kind", optional "file_note"}
MUTATIONS = {
    # ── goal table, verbatim ──────────────────────────────────────────────
    "N9": {
        "kind": "text",
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
            DECL + "::test_the_tool_doc_names_the_no_verdict_gate_as_absent_not_a_loop",
        ],
    },
    "M21": {
        "file_note": "_unmeasured_declaration: startswith -> `in`; the "
                     "line[len(prefix):] slice is LEFT UNCHANGED (verbatim).",
        "edits": [{
            "file": IMPL,
            "anchor": "        if not line.startswith(_REPO_GATE_UNMEASURED_PREFIX):\n",
            "replacement": "        if not (_REPO_GATE_UNMEASURED_PREFIX in line):\n",
        }],
        "targeted": [
            DECL + "::test_the_r4_shaped_witness_flips_under_the_literal_m21",
        ],
    },
    "M21b": {
        "file_note": "_repo_gate_failure_cases: startswith -> `in`; slice "
                     "unchanged (verbatim).",
        "edits": [{
            "file": IMPL,
            "anchor": "        if not line.startswith(_REPO_GATE_CASE_PREFIX):\n",
            "replacement": "        if not (_REPO_GATE_CASE_PREFIX in line):\n",
        }],
        "targeted": [
            DECL + "::test_the_r4_shaped_witness_flips_under_the_literal_m21b",
        ],
    },
    "G2": {
        "file_note": "gate_deferral module constant GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9.",
        "edits": [{
            "file": GD,
            "anchor": "GATE_DEFERRAL_EPISODE_MAX_SECONDS = float(\n"
                      "    os.getenv(\"AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS\", \"10800\"))\n",
            "replacement": "GATE_DEFERRAL_EPISODE_MAX_SECONDS = 1e9\n",
        }],
        "targeted": [
            EXEC_POINTS + "::test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded",
        ],
    },
    "G2b": {
        "file_note": "gate_deferral module constant GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9.",
        "edits": [{
            "file": GD,
            "anchor": "GATE_DEFERRAL_EPISODE_MAX_CEILING = float(\n"
                      "    os.getenv(\"AITELIER_GATE_DEFERRAL_EPISODE_MAX_CEILING\", str(6 * 3600)))\n",
            "replacement": "GATE_DEFERRAL_EPISODE_MAX_CEILING = 1e9\n",
        }],
        "targeted": [
            EXEC_POINTS + "::test_the_effective_episode_ceiling_is_ten_thousand_eight_hundred_as_loaded",
        ],
    },
    "DUPIMPL": {
        "kind": "text",
        "file_note": "write the `_ERROR_RE = ...` line a second time, "
                     "immediately after itself.",
        "edits": [{
            "file": IMPL,
            "anchor": "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n",
            "replacement": "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n"
                           "_ERROR_RE = re.compile(r\"^ERROR\\s+(\\S+)\")\n",
        }],
        "targeted": [
            CHK + "::test_no_verbatim_previous_line_dup_in_non_test_code",
        ],
    },
    "RESTART": {
        "file_note": "persist the deferral ledger so a restarted in-process "
                     "ledger re-reads the episode — the behaviour the deleted "
                     "`episode_seconds(...) == 0.0` assert guarded.",
        "edits": [
            {
                "file": GD,
                "anchor": "@dataclass\nclass DeferralLedger:",
                "replacement": "# RESTART mutation: a process-wide store every fresh\n"
                               "# ledger reads back, i.e. the ledger IS persisted.\n"
                               "_PERSISTED_EPISODES: dict = {}\n\n\n@dataclass\n"
                               "class DeferralLedger:",
            },
            {
                "file": GD,
                "anchor": "    episodes: dict[str, _Episode] = field(default_factory=dict)\n",
                "replacement": "    episodes: dict[str, _Episode] = field("
                               "default_factory=lambda: _PERSISTED_EPISODES)\n",
            },
        ],
        "targeted": [
            EXEC_POINTS + "::test_the_deferral_ledger_is_not_persisted_so_a_restart_re_measures",
        ],
    },
    # ── the M21 pair (round 8/9 names) ────────────────────────────────────
    "M21PAIR": {
        "file_note": "`in` AND the `line.index(prefix)` slice together on the "
                     "declaration channel: a second, blunter mutation than "
                     "the goal table's M21.",
        "edits": [
            {
                "file": IMPL,
                "anchor": "        if not line.startswith(_REPO_GATE_UNMEASURED_PREFIX):\n",
                "replacement": "        if not (_REPO_GATE_UNMEASURED_PREFIX in line):\n",
            },
            {
                "file": IMPL,
                "anchor": "        raw = line[len(_REPO_GATE_UNMEASURED_PREFIX):]\n",
                "replacement": "        raw = line[line.index(_REPO_GATE_UNMEASURED_PREFIX) + len(_REPO_GATE_UNMEASURED_PREFIX):]\n",
            },
        ],
        "targeted": [
            DECL + "::test_a_mid_line_echo_kills_the_in_operator_on_the_declaration_channel",
        ],
    },
    "M21BPAIR": {
        "file_note": "The same pair on the case channel (M21b + its slice).",
        "edits": [
            {
                "file": IMPL,
                "anchor": "        if not line.startswith(_REPO_GATE_CASE_PREFIX):\n",
                "replacement": "        if not (_REPO_GATE_CASE_PREFIX in line):\n",
            },
            {
                "file": IMPL,
                "anchor": "        raw = line[len(_REPO_GATE_CASE_PREFIX):]\n",
                "replacement": "        raw = line[line.index(_REPO_GATE_CASE_PREFIX) + len(_REPO_GATE_CASE_PREFIX):]\n",
            },
        ],
        "targeted": [
            DECL + "::test_a_mid_line_echo_kills_the_in_operator_on_the_case_channel",
        ],
    },
    # ── review-defined names, the review's edits ──────────────────────────
    "M2": {
        "file_note": "a truncated gate's retained fragment alone decides the "
                     "declaration again (the truncation branch is skipped).",
        "edits": [{
            "file": IMPL,
            "anchor": "        return True\n    if gate.get(\"output_truncated\"):",
            "replacement": "        return True\n    if False and gate.get(\"output_truncated\"):",
        }],
        "targeted": [
            DECL + "::test_a_bounded_fragment_is_not_a_record_on_its_own",
        ],
    },
    "M19": {
        "file_note": "the truncation branch answers True instead of False.",
        "edits": [{
            "file": IMPL,
            "anchor": "        return False\n    return _unmeasured_declaration(str(gate.get(\"output\", \"\"))) is not None",
            "replacement": "        return True\n    return _unmeasured_declaration(str(gate.get(\"output\", \"\"))) is not None",
        }],
        "targeted": [
            DECL + "::test_a_bounded_fragment_is_not_a_record_on_its_own",
        ],
    },
    "TAILONLY2": {
        "file_note": "`_run_node_cmd` scans only the retained tail out[-2000:] "
                     "for the declaration.",
        "edits": [{
            "file": IMPL,
            "anchor": "\"unmeasured_declaration\": _unmeasured_declaration(out)}",
            "replacement": "\"unmeasured_declaration\": _unmeasured_declaration(out[-2000:])}",
        }],
        "targeted": [
            DECL + "::test_a_declaration_survives_output_far_past_the_retention_bound",
        ],
    },
    "D1": {
        "file_note": "a declared absence no longer sets repo_gate_unmeasured.",
        "edits": [{
            "file": IMPL,
            "anchor": "                report[\"repo_gate_unmeasured\"] = True",
            "replacement": "                report[\"repo_gate_unmeasured\"] = False",
        }],
        "targeted": [DECL, ABSENCE],
    },
    "D4": {
        "file_note": "a declared absence no longer sets repo_gate_absent.",
        "edits": [{
            "file": IMPL,
            "anchor": "                report[\"repo_gate_absent\"] = True",
            "replacement": "                report[\"repo_gate_absent\"] = False",
        }],
        "targeted": [DECL, ABSENCE],
    },
    "ABSTERM": {
        "file_note": "the terminal-wording checker answers True for any "
                     "sentence.",
        "edits": [{
            "file": GD,
            "anchor": "    lowered = str(reason).lower()\n"
                      "    if ABSENCE_TERMINAL.lower() not in lowered:\n"
                      "        return False",
            "replacement": "    lowered = str(reason).lower()\n"
                           "    if True:\n"
                           "        return True\n"
                           "    if ABSENCE_TERMINAL.lower() not in lowered:\n"
                           "        return False",
        }],
        "targeted": [ACCOUNTED],
    },
    "ABSTERM2": {
        "file_note": "drop the plural-stem widening of the forbidden words.",
        "edits": [{
            "file": GD,
            "anchor": "    words |= {w[:-1] for w in words if w.endswith(\"s\")}",
            "replacement": "    words |= set()",
        }],
        "targeted": [ACCOUNTED],
    },
    "S1": {
        "file_note": "the tick's observe_run result is replaced by `none`: "
                     "the scheduler tick hold never sees the absence.",
        "edits": [{
            "file": SCHED,
            "anchor": "    deferral = gate_deferral.observe_run(sf, run_id)\n" + _TICK_SILENT,
            "replacement": "    deferral = {\"state\": \"none\", \"remaining\": 0.0, "
                           "\"gate\": \"\", \"reason\": \"\"}\n" + _TICK_SILENT,
        }],
        "targeted": [ABSENCE, EXEC_POINTS],
    },
    "VALVEREACH": {
        "file_note": "the tick's silent branch logs and falls through instead "
                     "of returning.",
        "edits": [{
            "file": SCHED,
            "anchor": "                 remaining=f\"{deferral['remaining']:.0f}s\")\n"
                      "        return\n"
                      "    if deferral[\"state\"] == \"expired\":",
            "replacement": "                 remaining=f\"{deferral['remaining']:.0f}s\")\n"
                           "        pass\n"
                           "    if deferral[\"state\"] == \"expired\":",
        }],
        "targeted": [ABSENCE, EXEC_POINTS],
    },
    "SCHEDROWRETRY": {
        "file_note": "the tick's silent branch charges the parked `test` "
                     "row's retry_count.",
        "edits": _sched_row_charge("retry_count=retry_count+1"),
        "targeted": [ABSENCE],
    },
    "SCHEDROWSTATUS": {
        "file_note": "the tick's silent branch sets the parked `test` row "
                     "back to pending.",
        "edits": _sched_row_charge("status='pending'"),
        "targeted": [ABSENCE],
    },
    "H2": {
        "file_note": "hold_blocks_advance always answers False.",
        "edits": [{
            "file": GD,
            "anchor": "    book = LEDGER if ledger is None else ledger\n    return book.deferring(run_id, now=now)\n",
            "replacement": "    book = LEDGER if ledger is None else ledger\n    return False\n",
        }],
        "targeted": [
            ABSENCE,
            EXEC_POINTS,
        ],
    },
    "RC2": {
        "edits": [{
            "file": IMPL,
            "anchor": "    return (REPO_GATE_MEASURED_PASS if gate.get(\"returncode\") == 0\n            else REPO_GATE_MEASURED_FAIL)\n",
            "replacement": "    return (REPO_GATE_MEASURED_PASS if gate.get(\"returncode\") == 0\n            else REPO_GATE_UNMEASURED)\n",
        }],
        "targeted": [
            DECL + "::test_no_exit_code_alone_is_ever_unmeasured",
        ],
    },
    "TIMEOUT_NOT_UNMEASURED": {
        "edits": [{
            "file": IMPL,
            "anchor": "    if gate.get(\"timed_out\") is True or gate.get(\"runner_error\") is True:\n",
            "replacement": "    if False:\n",
        }],
        "targeted": [
            DECL + "::test_a_killed_gate_is_unmeasured_and_its_returncode_is_not_why",
        ],
    },
    "VALVEBYPASS": {
        "edits": [{
            "file": GD,
            "anchor": "    return claims > max_claims\n",
            "replacement": "    if ledger is not None and ledger.deferring(run_id, now=now):\n        return False\n    return claims > max_claims\n",
        }],
        "targeted": [EXEC_POINTS, ACCOUNTED],
    },
    "ABSCEIL": {
        "edits": [{
            "file": GD,
            "anchor": "_ABSOLUTE_EPISODE_CEILING = 6 * 3600.0\n",
            "replacement": "_ABSOLUTE_EPISODE_CEILING = 1e9\n",
        }],
        "targeted": [EXEC_POINTS],
    },
    "ABSENCEWORD": {
        "edits": [{
            "file": GD,
            "anchor": "_FORBIDDEN_WORDS = (\"failed\", \"failure\", \"fail\", \"regression\", \"red\", \"error\")\n",
            "replacement": "_FORBIDDEN_WORDS = ()\n",
        }],
        "targeted": [ACCOUNTED],
    },
    "READABSENCE": {
        "edits": [{
            "file": GD,
            "anchor": "    if not (data.get(\"repo_gate_absent\") or data.get(\"repo_gate_unmeasured\")):\n        return None\n",
            "replacement": "    if False:\n        return None\n",
        }],
        "targeted": [ACCOUNTED],
    },
    "ABSREL": {
        "edits": [{
            "file": IMPL,
            "anchor": "    report[\"passed_relative\"] = (state in BASELINE_MEASURED\n                                 and not absent\n                                 and not report[\"new_failures\"])\n",
            "replacement": "    report[\"passed_relative\"] = (state in BASELINE_MEASURED\n                                 and not report[\"new_failures\"])\n",
        }],
        "targeted": [DECL],
    },
    "ABSSTATE": {
        "edits": [{
            "file": IMPL,
            "anchor": "        if absent and (path is None or not path.is_file()):\n            state = \"unmeasured\"\n",
            "replacement": "        pass\n",
        }],
        "targeted": [DECL],
    },
    "ABSWRITE": {
        "edits": [{
            "file": IMPL,
            "anchor": "    if path is None or state in (\"unreadable\", \"unmeasured\"):\n        return\n",
            "replacement": "    if path is None or state == \"unreadable\":\n        return\n",
        }],
        "targeted": [DECL],
    },
    "ABSSEED": {
        "edits": [{
            "file": IMPL,
            "anchor": "    if (path is not None and not path.is_file() and state == \"compared\"\n            and not absent):\n",
            "replacement": "    if (path is not None and not path.is_file() and state == \"compared\"):\n",
        }],
        "targeted": [DECL],
    },
    "RETFLAG": {
        "edits": [{
            "file": IMPL,
            "anchor": "            \"repo_gate_absent\": bool(report.get(\"repo_gate_absent\")),\n",
            "replacement": "            \"repo_gate_absent\": False,\n",
        }],
        "targeted": [
            "tests/skillflow/test_config_loading.py",
            ABSENCE,
        ],
    },
    "ATTEMPTS3": {
        "edits": [{
            "file": IMPL,
            "anchor": "REPO_GATE_UNMEASURED_ATTEMPTS = 1\n",
            "replacement": "REPO_GATE_UNMEASURED_ATTEMPTS = 3\n",
        }],
        "targeted": [EXEC_POINTS],
    },
    "TIMEOUTBIG": {
        "edits": [{
            "file": IMPL,
            "anchor": "REPO_GATE_TIMEOUT = 5400\n",
            "replacement": "REPO_GATE_TIMEOUT = 1e9\n",
        }],
        "targeted": [EXEC_POINTS],
    },
    "WAITCLAMP": {
        "edits": [{
            "file": GD,
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0),\n               max(1.0, _positive_seconds(GATE_DEFERRAL_WAIT_MAX, 900.0)))\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0)\n",
        }],
        "targeted": [
            ACCOUNTED + "::test_the_wait_cannot_be_widened_until_the_ceiling_disappears",
        ],
    },
    "CEILCLAMP": {
        "edits": [{
            "file": GD,
            "anchor": "    return min(_positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0),\n               ceiling)\n",
            "replacement": "    return _positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0)\n",
        }],
        "targeted": [EXEC_POINTS],
    },
    "DUPYIELD": {
        "kind": "text",
        "edits": [{
            "file": CHK,
            "anchor": "    return non_test, test\n",
            "replacement": "    return non_test, test\n    return non_test, test\n",
        }],
        "targeted": [
            CHK + "::test_the_checker_file_is_clean_under_its_own_rule",
        ],
    },
    "DUPHITS": {
        "kind": "text",
        "edits": [{
            "file": CHK,
            "anchor": "    assert non_test == []\n",
            "replacement": "    assert non_test == []\n    assert non_test == []\n",
        }],
        "targeted": [
            CHK + "::test_the_checker_file_is_clean_under_its_own_rule",
        ],
    },
    # ── round 8's own edits under their own names ─────────────────────────
    "TIMEOUT60": {
        "file_note": "round 8's `S1` edit: REPO_GATE_TIMEOUT = 60.",
        "edits": [{
            "file": IMPL,
            "anchor": "REPO_GATE_TIMEOUT = 5400\n",
            "replacement": "REPO_GATE_TIMEOUT = 60\n",
        }],
        "targeted": [EXEC_POINTS],
    },
    "ABSTERMTEXT": {
        "file_note": "round 8's `ABSTERM` edit: the absence sentence names a "
                     "code failure.",
        "edits": [{
            "file": GD,
            "anchor": "ABSENCE_TERMINAL = \"gate did not run: no verdict was measured\"\n",
            "replacement": "ABSENCE_TERMINAL = \"gate failed: the code did not pass\"\n",
        }],
        "targeted": [EXEC_POINTS, ACCOUNTED],
    },
    "TERMREASONCYCLE": {
        "file_note": "round 8's `ABSTERM2` edit: terminal_reason says "
                     "`Cycle limit exceeded`.",
        "edits": [{
            "file": GD,
            "anchor": "        return f\"{ABSENCE_TERMINAL} ({gate}, {runs} gate run(s))\"\n",
            "replacement": "        return (\"Cycle limit exceeded: the code \"\n                \"regressed and gate did not run: no verdict was measured\")\n",
        }],
        "targeted": [EXEC_POINTS, ACCOUNTED],
    },
    "VALVEOFF": {
        "file_note": "round 8's `VALVEREACH` edit: the per-instance valve "
                     "never fires.",
        "edits": [{
            "file": GD,
            "anchor": "    return claims > max_claims\n",
            "replacement": "    return False\n",
        }],
        "targeted": [EXEC_POINTS, ACCOUNTED],
    },
    "NEVEREXPIRE": {
        "file_note": "round 8's `D1` edit: observe_run never reports expired.",
        "edits": [{
            "file": GD,
            "anchor": "    if book.expired(run_id, now=moment):\n",
            "replacement": "    if False:\n",
        }],
        "targeted": [EXEC_POINTS, ABSENCE],
    },
    "READNONDICT": {
        "file_note": "round 8's `D4` edit: read_absence drops its non-dict "
                     "guard.",
        "edits": [{
            "file": GD,
            "anchor": "    if not isinstance(data, dict):\n        return None\n",
            "replacement": "    pass\n",
        }],
        "targeted": [ACCOUNTED],
    },
    "DECLNONE": {
        "file_note": "round 8's `M19` edit: `_run_node_cmd` never carries a "
                     "declaration.",
        "edits": [{
            "file": IMPL,
            "anchor": "                \"unmeasured_declaration\": _unmeasured_declaration(out)}\n",
            "replacement": "                \"unmeasured_declaration\": None}\n",
        }],
        "targeted": [
            DECL + "::test_a_declaration_survives_output_far_past_the_retention_bound",
        ],
    },
    # ── the two production holds, one hold-breaking edit each ─────────────
    "HOST_HOLD_INERT": {
        "file_note": "AItelierSkillFlow.advance_run no longer checks "
                     "hold_blocks_advance — the host-level hold is inert; "
                     "any caller of advance_run (run_driver, API, tests) "
                     "can advance a deferred run.",
        "edits": [{
            "file": HOST,
            "anchor": "        from core import gate_deferral\n        if gate_deferral.hold_blocks_advance(run_id):\n            return None\n",
            "replacement": "        pass\n",
        }],
        "targeted": [ABSENCE],
    },
    "TICK_HOLD_INERT": {
        "file_note": "The scheduler tick's deferral check is bypassed: the "
                     "tick never returns early for a silent gate "
                     "(core/scheduler.py, the `silent` branch never taken).",
        "edits": [{
            "file": SCHED,
            "anchor": _TICK_SILENT,
            "replacement": "    if False:\n",
        }],
        "targeted": [ABSENCE],
    },
    "OBSERVENONE": {
        "file_note": "round 9's `TICK_HOLD_INERT` edit, which is in "
                     "gate_deferral.observe_run, not in the tick: observe_run "
                     "never reports an absence.",
        "edits": [{
            "file": GD,
            "anchor": "    book = LEDGER if ledger is None else ledger\n    absence = last_gate_absence(sf, run_id, report_path=report_path)\n    if absence is None:\n        book.clear(run_id)\n        return {\"state\": \"none\", \"remaining\": 0.0, \"gate\": \"\", \"reason\": \"\"}\n",
            "replacement": "    book = LEDGER if ledger is None else ledger\n    absence = last_gate_absence(sf, run_id, report_path=report_path)\n    if True:\n        book.clear(run_id)\n        return {\"state\": \"none\", \"remaining\": 0.0, \"gate\": \"\", \"reason\": \"\"}\n",
        }],
        "targeted": [ABSENCE],
    },
}

FULL_SCOPE = ["tests/unit", "tests/skillflow"]
