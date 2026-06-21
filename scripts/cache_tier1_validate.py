#!/usr/bin/env python3
"""Tier-1 offline validation of F1 (shared system preamble).

Builds each step's SYSTEM message (preamble + role template) against the real
workspace and measures the universal common prefix across all step types — i.e.
the block the provider KV cache reuses across steps. Pre-F1 this was 0.
No LLM calls.
"""
import sys, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.prompt_assembler import PromptAssembler

WS = Path("/home/linxuhao/.AItelier/workspaces/aitelier-web-ui")
CODE = WS / "dpe_default_v2" / "t_impl" / "web"
TPL = ROOT / "templates"


def tok(s):
    return len(s) // 4


def cpl(a, b):
    n = min(len(a), len(b)); i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def role_templates():
    d = yaml.safe_load(open(ROOT / "agent_configs" / "dpe_default.yaml"))
    ag = d.get("agents", d)
    out = {}
    for name, cfg in ag.items():
        if not isinstance(cfg, dict):
            continue
        c = cfg.get("config", cfg)
        t = c.get("template") or cfg.get("template")
        if t and (TPL / t).exists():
            out[name] = (TPL / t).read_text(encoding="utf-8")
    return out


def universal_prefix(systems):
    vals = list(systems.values())
    u = vals[0]
    for v in vals[1:]:
        u = u[:cpl(u, v)]
    return u


def main():
    a = PromptAssembler()
    tpls = role_templates()

    for include_design in (False, True):
        pre = a.build_shared_preamble(WS, CODE, graph_name="dpe_default_v2", preamble_steps=["1","2"], include_design=include_design)
        # Post-F1: system = preamble + role template
        systems = {r: pre + "\n\n" + t for r, t in tpls.items()}
        u = universal_prefix(systems)
        # Sanity: pre-F1 systems were just the templates → universal prefix:
        pre_f1 = universal_prefix(tpls)
        label = "F1+F2 (design hoisted)" if include_design else "F1 only"
        print(f"=== {label} ===")
        print(f"  preamble size:                 ~{tok(pre)} tok ({len(pre)} chars)")
        print(f"  cross-step shared prefix BEFORE (templates only): "
              f"~{tok(pre_f1)} tok")
        print(f"  cross-step shared prefix AFTER  (preamble+template): "
              f"~{tok(u)} tok")
        print(f"  → shared prefix starts: {u[:60]!r}")
        # Per-run prize estimate: shared prefix re-billed across N steps.
        print(f"  per-run prize if reused across ~14 steps: "
              f"~{tok(u) * 13} tok become cache hits\n")


if __name__ == "__main__":
    main()
