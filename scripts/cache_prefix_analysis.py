#!/usr/bin/env python3
"""Analyze cross-step prompt-cache potential from real trace prompts.

Reconstructs each step's request as [system + user] (the order the native loop
builds messages) and measures:
  1. The universal common prefix across ALL step types (what caches today).
  2. Whether the project brief / design blocks are byte-identical across steps
     (the cross-step prize, if hoisted to the front).
"""
import sqlite3, json
from collections import OrderedDict

DB = "/home/linxuhao/.AItelier/skillflow.db"


def approx_tokens(s):
    return len(s) // 4  # rough char→token


def common_prefix_len(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def load_one_per_step():
    db = sqlite3.connect(DB)
    q = ("select step_id,payload_json from skillflow_trace "
         "where category='prompt' and event='user_prompt' order by id")
    reps = OrderedDict()
    for step_id, pj in db.execute(q):
        if step_id in reps:
            continue
        p = json.loads(pj)
        sysp = p.get("system", "") or ""
        user = p.get("user") or p.get("content") or ""
        if sysp or user:
            reps[step_id] = (sysp, user)
    return reps


def extract_block(user, header):
    """Extract a '[Header]...' section up to the next '\n\n[' marker."""
    i = user.find(header)
    if i < 0:
        return ""
    j = user.find("\n\n[", i + len(header))
    return user[i:j if j > 0 else len(user)]


def main():
    reps = load_one_per_step()
    steps = list(reps)
    print(f"Step types with prompts: {len(steps)}\n")

    # 1. Universal common prefix across ALL requests (system+user).
    full = {s: (sysp + "\n\n" + user) for s, (sysp, user) in reps.items()}
    base = full[steps[0]]
    uni = base
    for s in steps[1:]:
        uni = uni[:common_prefix_len(uni, full[s])]
    print(f"1. Universal common prefix across all {len(steps)} step types:")
    print(f"   {len(uni)} chars (~{approx_tokens(uni)} tok)")
    print(f"   starts: {uni[:80]!r}\n")

    # 1b. Common prefix of SYSTEM prompts only.
    sysmap = {s: sysp for s, (sysp, _) in reps.items()}
    sbase = sysmap[steps[0]]
    suni = sbase
    for s in steps[1:]:
        suni = suni[:common_prefix_len(suni, sysmap[s])]
    uniq_sys = len(set(sysmap.values()))
    print(f"1b. System prompts: {uniq_sys} distinct of {len(steps)}; "
          f"common prefix = {len(suni)} chars (~{approx_tokens(suni)} tok)\n")

    # 2. Is brief / design identical across steps? (the cross-step prize)
    for header in ("[Project Brief]", "[Project Design]"):
        blocks = {s: extract_block(user, header) for s, (_, user) in reps.items()}
        present = {s: b for s, b in blocks.items() if b}
        if not present:
            print(f"2. {header}: not present in any step prompt\n")
            continue
        sizes = {len(b) for b in present.values()}
        distinct = len(set(present.values()))
        sample = next(iter(present.values()))
        print(f"2. {header}: present in {len(present)}/{len(steps)} steps, "
              f"{distinct} distinct value(s), ~{approx_tokens(sample)} tok each")
        if distinct == 1:
            tot = approx_tokens(sample) * (len(present) - 1)
            print(f"   → IDENTICAL across steps. If hoisted to a shared front "
                  f"prefix, ~{tot} tok/run become cross-step cache hits "
                  f"(re-billed full today).")
        print()


if __name__ == "__main__":
    main()
