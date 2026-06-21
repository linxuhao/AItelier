#!/usr/bin/env python3
"""Live DeepSeek prompt-cache probe.

Validates that (a) the Phase-0 telemetry captures cache hit/miss, and
(b) a shared prefix across two DIFFERENT step prompts produces cross-step
cache hits. Reads DEEPSEEK_API_KEY from .env. Costs a handful of cheap calls.

Modes:
  smoke              — 2 identical calls; 2nd must show cache_hit_tokens > 0
  crossstep A B      — send prompt A then prompt B (built from the assembler
                       against the real workspace); report B's cache hit tokens
"""
import sys, os, json, time
from pathlib import Path
import dotenv

dotenv.load_dotenv(Path(__file__).resolve().parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ai_router import AIGateway

MODEL = os.getenv("AITELIER_HOST_AGENT_MODEL", "deepseek/deepseek-v4-flash")


def call(system, user):
    gw = AIGateway(MODEL, temperature=0.0, max_output_tokens=16)
    gw.generate(system, user)
    return gw.last_usage


def smoke():
    # A long-enough shared prefix so the cache has >= a block to store.
    big = ("You are a meticulous software reviewer.\n" +
           "\n".join(f"Rule {i}: always check edge case {i}." for i in range(400)))
    print(f"model={MODEL}")
    u1 = call(big, "Summarize rule 1 in three words.")
    print("call 1 (cache write):", json.dumps(u1))
    time.sleep(2)
    u2 = call(big, "Summarize rule 2 in three words.")
    print("call 2 (cache read): ", json.dumps(u2))
    hit = u2.get("cache_hit_tokens", 0)
    print(f"\nRESULT: 2nd call cache_hit_tokens={hit} "
          f"({'CACHED ✓' if hit > 0 else 'NO CACHE ✗'})")


def crossstep():
    """Two DIFFERENT step prompts sharing the F1 preamble must cache-share it."""
    import yaml
    from core.prompt_assembler import PromptAssembler
    ROOT = Path(__file__).resolve().parent.parent
    WS = Path("/home/linxuhao/.AItelier/workspaces/aitelier-web-ui")
    TPL = ROOT / "templates"
    a = PromptAssembler()
    inc = "--design" in sys.argv
    preamble = a.build_shared_preamble(
        WS, WS / "dpe_default_v2" / "t_impl" / "web",
        graph_name="dpe_default_v2", preamble_steps=["1","2"], include_design=inc)
    # two distinct role templates (different steps) sharing the preamble
    ta = (TPL / "step1_5_researcher_red.md").read_text(encoding="utf-8")
    tb = (TPL / "step2_architect_red.md").read_text(encoding="utf-8")
    sysA = preamble + "\n\n" + ta
    sysB = preamble + "\n\n" + tb
    print(f"model={MODEL}  preamble~{len(preamble)//4} tok")
    uA = call(sysA, "Reply OK.")
    print("step A (researcher_reviewer) write:", json.dumps(uA))
    time.sleep(2)
    uB = call(sysB, "Reply OK.")
    print("step B (architect_reviewer)   read: ", json.dumps(uB))
    hit = uB.get("cache_hit_tokens", 0)
    pre_tok = len(preamble) // 4
    print(f"\nRESULT: step B cache_hit_tokens={hit} vs preamble ~{pre_tok} tok")
    print("  → cross-step cache on the shared preamble: "
          f"{'CONFIRMED ✓' if hit >= pre_tok * 0.7 else 'NOT cached ✗'}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if mode == "smoke":
        smoke()
    elif mode == "crossstep":
        crossstep()
    else:
        print("unknown mode; use: smoke | crossstep")
