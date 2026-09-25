"""Apply one named mutation of the rev 5 code to a tree, with an ignition probe.

usage: mutate.py <tree> <name>

Each mutation replaces one exact source text (it must occur exactly once) and
carries IGN, an expression that appends one byte to $GN5_IGN each time the
mutated code runs. CONTROL adds the probe and changes nothing else.
"""
import sys
from pathlib import Path

IGN = 'open(__import__("os").environ.get("GN5_IGN", "/dev/null"), "a").write("x")'
IMPL = "aitelier/tools/run_tests/impl.py"
EVID = "aitelier/gate_evidence.py"
CFG = "configs/coding_impl.yaml"

MUTANTS = {
    "CONTROL_ignition_only": (IMPL,
        '    reports: dict = {}\n    if not ticket_dir.is_dir():\n',
        f'    reports: dict = {{}}\n    {IGN}\n    if not ticket_dir.is_dir():\n'),
    "B1_unobserved_falls_back_to_the_name": (IMPL,
        '    if first is None:\n        why = "the order of creation under the ticket was not observed"\n',
        f'    if first is None and naming_repo:\n        {IGN}\n        own_dir = naming_repo[0].parent\n'
        '    elif first is None:\n        why = "the order of creation under the ticket was not observed"\n'),
    "B2_a_file_first_entry_is_the_report": (IMPL,
        '    elif not first.get("is_dir"):\n        why = (',
        f'    elif not first.get("is_dir"):\n        {IGN}\n'
        '        own_dir = (ticket_dir if any(p.parent == ticket_dir for p in naming_repo)\n'
        '                   else None)\n        why = ('),
    "B3_first_dir_without_manifest_takes_the_named_report": (IMPL,
        '            own_dir = candidate\n        else:\n',
        f'            own_dir = candidate\n        elif naming_repo:\n            {IGN}\n'
        '            own_dir = naming_repo[0].parent\n        else:\n'),
    "B4_first_dir_is_always_the_gates": (IMPL,
        '        if any(candidate in path.parents for path in naming_repo):\n',
        f'        if ({IGN}, True)[1]:\n'),
    "B5_unattributed_reds_not_reported": (IMPL,
        '        result["unattributed_reds"] = retained.unattributed\n',
        f'        {IGN}\n'),
    "B6_outcome_ignores_unattributed_reds": (IMPL,
        '    if gate.get("unattributed_reds"):\n        return REPO_GATE_UNATTRIBUTABLE\n',
        f'    if gate.get("unattributed_reds"):\n        {IGN}\n'),
    "B7_a_missing_runner_does_not_win": (IMPL,
        '                report["repo_gate_unattributable"] = not report.get(\n'
        '                    "infrastructure_unavailable")\n',
        f'                report["repo_gate_unattributable"] = ({IGN}, True)[1]\n'),
    "B8_no_identity_error_beside_unattributable_reds": (IMPL,
        '                report["failure_identity_error"] = sentence\n',
        f'                {IGN}\n'),
    "B9_findings_without_a_manifest_unread": (IMPL,
        '            elif isinstance(listed, list):\n                unattributed += [',
        f'            elif ({IGN}, False)[1]:\n                unattributed += ['),
    "B10_the_failing_call_is_not_recorded": (IMPL,
        '                self.failure = _errno_text("inotify_init1", ctypes.get_errno())\n',
        f'                {IGN}\n'),
    "B11_the_flag_is_not_returned": (IMPL,
        '            "repo_gate_unattributable": bool(\n                report.get("repo_gate_unattributable")),\n',
        f'            "repo_gate_unattributable": ({IGN}, False)[1],\n'),
    "B12_a_second_report_is_not_an_error": (IMPL,
        '    if len(naming_repo) > 1:\n        errors.append(f"repository gate retained',
        f'    if ({IGN}, False)[1]:\n        errors.append(f"repository gate retained'),
    "B13_release_reads_unattributable_as_failed": (EVID,
        '    if status in _UNATTRIBUTABLE_STATUSES:\n        return "unattributable"\n',
        f'    if status in _UNATTRIBUTABLE_STATUSES:\n        {IGN}\n'),
    "B14_no_edge_to_the_terminal": (CFG,
        '      - to: "test_gate_report_unattributable"\n'
        '        match: { field: "repo_gate_unattributable", value: true }\n',
        ''),
}


def main() -> int:
    tree, name = Path(sys.argv[1]), sys.argv[2]
    path, old, new = MUTANTS[name]
    target = tree / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        print(f"MUTANT {name}: expected 1 occurrence in {path}, found {count}")
        return 3
    target.write_text(text.replace(old, new), encoding="utf-8")
    print(f"MUTANT {name}: applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
