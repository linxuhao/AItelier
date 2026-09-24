import json, re, sys
from pathlib import Path
D = Path("final/logs/mutations")
FILE = "tests/skillflow/test_coding_impl_gate_absence.py"
MSGS = [r"deferral charged a retry: \d+", r"deferral released the claim: \d+",
        r"step re-claimed during deferral: \d+", r"test row status drifted: \w+",
        r"the run row moved while the gate was silent: status=\w+ node=\w+"]
for name in sys.argv[1:]:
    text = (D / f"{name}.txt").read_text()
    targeted = text.split("## full scope")[0]
    ign = json.loads(re.search(r"^IGNITION_TARGETED=(.*)$", text, re.M).group(1))
    lit = sorted(t for t in ign.get("igniting_tests", []) if t.startswith(FILE))
    red = sorted(set(re.findall(r"^FAILED (" + re.escape(FILE) + r"::\S+)", targeted, re.M)))
    print(f"{name} targeted_line_hits={ign.get('line_hits')} igniting_tests_in_absence_file={len(lit)} red_in_absence_file={len(red)}")
    for m in MSGS:
        n = len(re.findall(m, targeted))
        if n:
            print(f"  {name} row_or_run_assert_messages[{m}]={n}")
    for t in red:
        print(f"  {name} RED {t}")
