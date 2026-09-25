"""Apply one r3 mutation to a COPY of the tree.

usage: python mutate.py <tree copy> <mutation id> <ignition log>

Each mutation replaces one anchor that must occur exactly once, and carries an
ignition probe: one "x" is appended to <ignition log> every time the mutated
code runs, so a mutation no test executed is told apart from one the tests
executed and did not notice. CONTROL changes nothing and writes no probe.
"""
import pathlib
import sys

tree = pathlib.Path(sys.argv[1])
mid = sys.argv[2]
LOG = sys.argv[3]


def probe(indent):
    return " " * indent + 'open(%r, "a").write("x")\n' % LOG


IMPL = "aitelier/tools/run_tests/impl.py"
ADM = "aitelier/gate_admission.py"
DEF = "core/gate_deferral.py"

M = {
    "CONTROL": [],
    # a retained finding no longer outranks the three sources
    "N1_findings_first": [(IMPL,
        '    if gate.get("retained_findings"):\n        return REPO_GATE_MEASURED_FAIL\n',
        probe(4))],
    # the absence flag no longer looks at the rest of the report
    "N2_absent_whatever_else_failed": [(IMPL,
        '                report["repo_gate_absent"] = not report["failures"]\n',
        probe(16) + '                report["repo_gate_absent"] = True\n')],
    # the scheduler reads the r2 flag again
    "N3_read_absence_r2": [(DEF,
        '    absent = (data.get("repo_gate_absent") if "repo_gate_absent" in data\n'
        '              else data.get("repo_gate_unmeasured"))\n',
        probe(4) + '    absent = data.get("repo_gate_unmeasured")\n')],
    # no assertion row is read: every assertion falls back to a text hash
    "N4_no_assert_rows": [(IMPL,
        '    out: dict = {}\n    behavior = report.get("behavior") if isinstance(report, dict) else None\n',
        probe(4) + '    return {}\n')],
    # findings with the same id collapse into one id
    "N5_no_dedupe": [(IMPL,
        '        if counts[case_id] > 1:\n            case_id = f"{case_id}~{counts[case_id]}"\n',
        probe(8))],
    # the keepalive writes nothing
    "N6_keepalive_writes_nothing": [(ADM,
        '                self._handler.wfile.write(b"HTTP/1.1 100 Continue\\r\\n\\r\\n")\n'
        '                self._handler.wfile.flush()\n',
        probe(16) + '                pass\n')],
    # the keepalive goes on after admission, through the render
    "N7_keepalive_past_admission": [(ADM,
        '                self._send()\n                return\n            if time.monotonic() - last',
        probe(16) + '                self._send()\n            if time.monotonic() - last')],
    # a keepalive with no owner table to read admission from
    "N8_keepalive_without_lifecycle": [(ADM,
        '            before = self._owner_ids(watched_operation)\n',
        probe(12) + '            before = self._owner_ids(watched_operation) or set()\n'),
        (ADM,
        '            if owners is None:\n                # Admission can no longer be seen',
        '            if owners is None:\n                owners = set()\n'
        '            if False:\n                # Admission can no longer be seen')],
    # the lap limit is never read
    "N9_laps_never_spent": [(DEF,
        '    return max_loop is not None and count is not None and count >= max_loop\n',
        probe(4) + '    return False\n')],
    # the last lap granted is taken away (the lap check on any node)
    "N10_laps_on_any_node": [(DEF,
        '    if node != ABSENCE_GATE:\n        return False\n    reader =',
        probe(4) + '    reader =')],
    # review gnr2 R5: the host no longer notes the latest report
    "N11_host_skips_latest_report": [(DEF,
        '    if sf is not None and run_id in book.episodes:\n',
        probe(4) + '    if False:\n')],
    # a report above the whole-parse bound is not read
    "N12_no_tail_parse": [(IMPL,
        "            at = m.rfind(b'\"behavior\":')\n",
        probe(12) + '            return None\n')],
}

for path, old, new in M[mid]:
    p = tree / path
    text = p.read_text()
    n = text.count(old)
    if n != 1:
        print(f"MUTATION {mid}: anchor count {n} != 1 in {path}")
        sys.exit(3)
    p.write_text(text.replace(old, new))
    print(f"MUTATION {mid}: applied to {path}")
pathlib.Path(LOG).write_text("")
