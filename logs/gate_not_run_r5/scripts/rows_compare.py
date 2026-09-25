"""Print candidate vs base values per row from two table logs' ROW lines.

usage: rows_compare.py <candidate_log> <base_log>
"""
import json
import re
import sys


def rows(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        m = re.search(r"ROW (\{.*\})\s*$", line)
        if m:
            d = json.loads(m.group(1))
            out.setdefault(d["row"], d)
    return out


cand, base = rows(sys.argv[1]), rows(sys.argv[2])
fmt = lambda d: (f'{d.get("attribution")}/{d["measured"]}/'
                 f'{str(d["repo_gate_absent"]).lower()}/{d["next"]}/'
                 f'{str(d["identity_error"]).lower()}') if d else "-"
print(f"{len(cand)} candidate rows, {len(base)} base rows")
for name in cand:
    c, b = cand[name], base.get(name)
    same = b is not None and all(c[k] == b[k] for k in
                                 ("measured", "repo_gate_absent", "next", "identity_error"))
    print(f"{name} | {fmt(c)} | {fmt(b)} | {'same' if same else 'DIFFERS'}")
