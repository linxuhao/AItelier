#!/usr/bin/env python3
"""Which endpoint served which step, newest first — the localqwen trial's scoreboard.

`flash` carries two localqwen entries out of five, so localqwen should take ~40%
of flash steps. Its quality is only measured on REVIEWS so far (13 steps, all
reviews); this exists to watch what happens once it starts drawing t_impl.
"""
import collections, glob, json, os, sqlite3, sys

since = float(sys.argv[1]) if len(sys.argv) > 1 else 0
rows = []
for db in glob.glob(os.path.expanduser("~/.AItelier/workspaces/*/trace.db")):
    if os.path.getmtime(db) < since:
        continue
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for ca, sid, p in c.execute(
            "select created_at, step_id, payload_json from skillflow_trace "
            "where category='usage'"):
        try:
            d = json.loads(p)
        except ValueError:
            continue
        # turn 1 only: one row per step = the endpoint the step BOUND to.
        if (d.get("turn") or 1) == 1 and d.get("served_by"):
            rows.append((ca, sid, d["served_by"], d.get("prompt_tokens") or 0))
rows.sort(reverse=True)

pair = collections.Counter((s, e) for _, s, e, _ in rows)
eps = sorted({e for _, _, e, _ in rows})
print(f"{'step':16}" + "".join(f"{e[:14]:>16}" for e in eps))
for s in sorted({s for _, s, _, _ in rows}):
    tot = sum(pair[(s, e)] for e in eps)
    if tot < 3:
        continue
    print(f"{s:16}" + "".join(f"{pair[(s,e)]:>16}" for e in eps))

print("\n最近 12 个 step:")
for ca, sid, ep, pt in rows[:12]:
    print(f"  {ca}  {sid:16} {ep:32} pt={pt}")
