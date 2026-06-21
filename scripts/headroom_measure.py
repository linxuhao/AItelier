#!/usr/bin/env python3
"""Measure headroom compression on REAL AItelier prompts pulled from the
skillflow trace DB. Compression-only: POSTs to /v1/compress, no LLM call.

Usage: python3 scripts/headroom_measure.py [--probe]
"""
import sqlite3, json, sys, statistics, urllib.request
from collections import defaultdict

DB = "/home/linxuhao/.AItelier/skillflow.db"
URL = "http://127.0.0.1:8787/v1/compress"


def compress(messages):
    body = json.dumps({"messages": messages, "model": "gpt-4o"}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def load_prompts():
    db = sqlite3.connect(DB)
    q = ("select step_id,payload_json from skillflow_trace "
         "where category='prompt' and event='user_prompt'")
    out = []
    for step_id, pj in db.execute(q):
        p = json.loads(pj)
        msgs = []
        if p.get("system"):
            msgs.append({"role": "system", "content": p["system"]})
        user = p.get("user") or p.get("content")
        if user:
            msgs.append({"role": "user", "content": user})
        if msgs:
            out.append((step_id, msgs))
    return out


def main():
    prompts = load_prompts()
    if "--probe" in sys.argv:
        step_id, msgs = next(m for m in prompts if m[0] == "t_impl_review")
        res = compress(msgs)
        print("PROBE t_impl_review:")
        print(json.dumps({k: v for k, v in res.items() if k != "messages"},
                         indent=2)[:1500])
        return

    by_step = defaultdict(lambda: {"before": [], "after": [], "transforms": defaultdict(int)})
    tot_b = tot_a = 0
    for step_id, msgs in prompts:
        try:
            res = compress(msgs)
        except Exception as e:
            print(f"  ! {step_id}: {e}")
            continue
        b, a = res.get("tokens_before", 0), res.get("tokens_after", 0)
        by_step[step_id]["before"].append(b)
        by_step[step_id]["after"].append(a)
        for t in res.get("transforms_applied", []):
            by_step[step_id]["transforms"][t.split(":")[1] if ":" in t else t] += 1
        tot_b += b; tot_a += a

    print(f"\n{'step':<18}{'n':>3}{'tok_before':>11}{'tok_after':>10}{'saved%':>8}  transforms")
    print("-" * 80)
    for step in sorted(by_step):
        d = by_step[step]
        b, a = sum(d["before"]), sum(d["after"])
        pct = 100 * (b - a) / b if b else 0
        tr = ",".join(f"{k}×{v}" for k, v in d["transforms"].items()) or "-"
        print(f"{step:<18}{len(d['before']):>3}{b:>11}{a:>10}{pct:>7.1f}%  {tr}")
    print("-" * 80)
    pct = 100 * (tot_b - tot_a) / tot_b if tot_b else 0
    print(f"{'TOTAL':<18}{'':>3}{tot_b:>11}{tot_a:>10}{pct:>7.1f}%")


if __name__ == "__main__":
    main()
