#!/usr/bin/env python3
"""Replay a REAL pipeline step against one or more endpoints, and diff the answers.

Why this exists
---------------
Model choices here have been argued from vendor benchmark tables — Qwen publishes
Flash-Next at +16.5 over the 27B on DeepSWE, and that is the number a hardware
purchase rests on. But a benchmark score is not this pipeline: our steps carry a
100k-token assembled prompt, our own tool schemas, and a reviewer that decides
pass/fail on repo idiom. The only honest question is whether a model is better
ON OUR PROMPTS, and the prompts are already on disk — `skillflow_steps.inputs_json`
holds every input a step actually received.

So this rebuilds the prompt through the REAL `PromptAssembler`, not a
reconstruction: a hand-rolled approximation would test a prompt no agent has ever
been given, and the whole point is fidelity. `trace` truncates at 20k, which is
why `inputs_json` is the source.

Usage
-----
    python scripts/replay_step.py --list
    python scripts/replay_step.py --step-row 1234 --endpoints localqwen/qwen3 qwen/qwen3.8-flash

Writes each answer plus the exact shared prompt to --out, so a later run against
an endpoint that was rate-limited today compares against a BYTE-IDENTICAL prompt
rather than a freshly assembled one.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = Path.home() / ".AItelier" / "skillflow.db"
WS = Path.home() / ".AItelier" / "workspaces"


def traced(step_row: int, project_id: str) -> dict:
    """What the endpoint that ORIGINALLY ran this step was sent, and did.

    `trace.step_instance_id` is `skillflow_steps.id`, so a step already served by
    an endpoint we cannot call today (an exhausted plan) still has its answer on
    disk. That is a better comparison arm than a fresh call: it is the real
    production answer, not a replay.

    The traced PROMPT is truncated at 20k, so it cannot be replayed byte-exactly
    — but it is exactly enough to verify that a reassembly matches what was sent.
    """
    db = WS / project_id / "trace.db"
    if not db.exists():
        return {}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {"served_by": None, "system": "", "user": "", "tool_calls": [], "text": []}
    row = c.execute("""SELECT payload_json FROM skillflow_trace
                       WHERE step_instance_id=? AND category='prompt'
                       ORDER BY seq LIMIT 1""", (step_row,)).fetchone()
    if row:
        d = json.loads(row[0])
        out["system"], out["user"] = d.get("system", ""), d.get("user", "")
    for (pj,) in c.execute("""SELECT payload_json FROM skillflow_trace
                              WHERE step_instance_id=? AND category='usage'
                              ORDER BY seq""", (step_row,)):
        sb = (json.loads(pj) or {}).get("served_by")
        if sb:
            out["served_by"] = sb
            break
    # `response` carries the whole answer (text, reasoning, tool_calls); the
    # separate `tool_call` category records only params, not which tool.
    for (pj,) in c.execute("""SELECT payload_json FROM skillflow_trace
                              WHERE step_instance_id=? AND category='response'
                              ORDER BY seq""", (step_row,)):
        d = json.loads(pj)
        out["attempt"] = d.get("attempt")
        if d.get("turn") == 1:
            out["text"] = d.get("text") or ""
            out["reasoning_chars"] = len(d.get("reasoning_content") or "")
            for tc in (d.get("tool_calls") or []):
                fn = tc.get("function") or tc
                out["tool_calls"].append(
                    f"{fn.get('name')}({str(fn.get('arguments'))[:110]})")
    return out


def rows(step_like: str, min_len: int, limit: int):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    return list(c.execute(
        """SELECT s.id, s.step_id, r.graph_name, r.project_id,
                  LENGTH(s.inputs_json), substr(s.completed_at,1,16)
           FROM skillflow_steps s JOIN skillflow_runs r ON r.id = s.run_id
           WHERE s.step_id LIKE ? AND s.status='completed'
             AND LENGTH(s.inputs_json) > ?
           ORDER BY s.completed_at DESC LIMIT ?""", (step_like, min_len, limit)))


def load(step_row: int):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = c.execute("""SELECT s.step_id, s.inputs_json, r.graph_name,
                            s.step_config_json, r.project_id
                     FROM skillflow_steps s JOIN skillflow_runs r ON r.id = s.run_id
                     WHERE s.id=?""", (step_row,)).fetchone()
    if not r:
        sys.exit(f"no step row {step_row}")
    try:
        step_cfg = json.loads(r[3] or "{}")
    except ValueError:
        step_cfg = {}
    return r[0], json.loads(r[1]), r[2], step_cfg, r[4]


def code_path_for(project_id: str):
    """An existing-repo run's code lives outside the workspace; the path is in
    aitelier.db's `runs`, not in the step. Without it the assembler emits no
    repo lines in [Workspace Directory Tree]."""
    db = Path.home() / ".AItelier" / "aitelier.db"
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        r = c.execute("SELECT repo_path FROM runs WHERE project_id=? AND repo_path IS NOT NULL "
                      "AND repo_path != '' LIMIT 1", (project_id,)).fetchone()
        return Path(r[0]) if r and r[0] else None
    except Exception:                                        # noqa: BLE001
        return None


def build_prompt(step_id: str, inputs: dict, graph_name: str,
                 step_cfg: dict, code_path, preamble_steps=None, drop_ctx=None) -> tuple[str, str, dict]:
    """The prompt exactly as the pipeline built it. Returns (system, user, cfg).

    Mirrors dpe_pipeline's native path: a project-global preamble in front of the
    role template, then the assembled user message with the globals hoisted out
    of it. Getting this wrong silently tests a prompt no agent ever saw.
    """
    from core.prompt_assembler import PromptAssembler

    cfg = inputs.get("_agent_config") or {}
    # `_output_dir` is <workspace>/<config>/<step>.tmp — the workspace is two up.
    out_dir = Path(inputs.get("_output_dir") or ".")
    project_path = out_dir.parent.parent
    asm = PromptAssembler()
    resolved = dict(inputs.get("_resolved_context") or {})
    # Addon prompt fragments: the runner merges these into the resolved context
    # from the step's own config, so guidance reaches the prompt only when its
    # addon is applied. Omitting them dropped the whole
    # "Addon guidance (game_harness/implementer.md)" block from the replay.
    # Removing a step from `preamble_steps` alone does NOT remove its content:
    # drop_preamble_steps then stops filtering it, so it reappears in the USER
    # message — moved from the cached prefix into the volatile part, which is
    # worse. To test "readable but not present" it must go from BOTH.
    for k in (drop_ctx or []):
        resolved.pop(k, None)
    frags = step_cfg.get("extra_templates") if isinstance(step_cfg, dict) else None
    if frags:
        from core.addon_registry import read_fragments
        resolved.update(read_fragments(frags))
    schemas = inputs.get("_tool_schemas") or {}

    root = Path(__file__).resolve().parent.parent
    tmpl = ((cfg.get("config") or {}).get("template") or "").strip()
    role_prompt = ""
    if tmpl:
        f = root / "templates" / tmpl
        if f.exists():
            role_prompt = f.read_text(encoding="utf-8")

    # Default matches dpe_default/dpe_game's x-aitelier.preamble_steps. Override
    # to test what the step does when a doc is READABLE but not PRESENT: the
    # preamble is byte-identical for the prefix cache, which makes its tokens
    # cheap — but never free of WINDOW, and the window is what forces the spill.
    preamble_steps = ["1", "2"] if preamble_steps is None else preamble_steps
    preamble = asm.build_shared_preamble(
        project_path, code_path, graph_name=graph_name,
        preamble_steps=preamble_steps, include_design=True)
    # The pipeline DROPS the preamble's steps from resolved_context before
    # assembling, or the prompt carries them twice. Skipping this put a whole
    # "### Step 2" block into the replay that the real prompt never had — the
    # fidelity check caught it at char 4,583.
    resolved = asm.drop_preamble_steps(resolved, preamble_steps)
    system = f"{preamble}\n\n{role_prompt}" if role_prompt else preamble
    # feedback="" on purpose: replaying a retry's feedback would score the model
    # on recovering from ANOTHER model's mistake, not on doing the task.
    user = asm.assemble(step_id, project_path, "", "", code_path=code_path,
                        resolved_context=resolved, tool_schemas=schemas,
                        native=True, hoist_globals=True, hoist_design=True)
    # The pipeline appends two blocks AFTER assemble(), past the cache-prefix
    # boundary: the turn budget and the language instruction. Omitting them
    # left the replay a correct but 6k-char-short prefix — and a model that
    # does not know its turn budget paces exploration differently.
    max_turns = (cfg.get("config") or {}).get("max_tool_turns", 24)
    user += (
        f"\n\n[Turn Budget: {max_turns} turns total, then forced output]\n"
        "You are a workflow automation step, not a chat assistant: your "
        "visible reply text is never shown to anyone and is discarded — "
        "only tool calls have any effect, and a reply with no tool call "
        "ends the step immediately.\n"
        "Plan your exploration, then call write_*/create_* to produce the "
        "required output. The moment all required files are written, call "
        "finish_step immediately. Do NOT re-read, re-list, search, or "
        "otherwise re-verify files you just wrote — writes are trusted and "
        "already staged, so re-verifying them only burns turns. Never end "
        "a turn with a plain-text 'done' / 'written successfully' note; "
        "your final action must be a tool call (the write tool, then "
        "finish_step). Do not exhaust all turns on exploration — leave at "
        "least 1 turn for writing.\n"
        # DUPLICATED from dpe_pipeline's native path. It has to be, because the
        # text is inline in a method rather than a shared constant — and a copy
        # that drifts makes the replay test a prompt production never sends.
        # It already did: this block lagged one edit behind and the first
        # template-fix run measured only half the change.
        "Read narrowly: `search` with a `glob` and `context_lines` to "
        "FIND, `read` with `start_line`/`end_line` to read a known "
        "region. Reading a large file whole spends the context you need "
        "for the edit itself."
    )
    from core.prompt_assembler import build_language_instruction
    lang = build_language_instruction("zh-CN")
    if lang:
        user += "\n\n" + lang
    return system, user, cfg


def ask(endpoint: str, system: str, user: str, cfg: dict,
        schemas: dict | None = None) -> dict:
    from core.ai_router import AIGateway
    from core.dpe_pipeline import PipelineEngine
    c = cfg.get("config") or {}
    # Native tool calling, through the pipeline's OWN converter. Without tools a
    # t_impl agent cannot act, so it writes prose instead of calling create/edit
    # — which scores the models on commentary rather than on the work.
    tools = PipelineEngine._to_openai_tools(schemas) if schemas else None
    th = c.get("thinking") or {}
    gw = AIGateway(endpoint,
                   enable_thinking=bool(th.get("enable")),
                   thinking_effort=th.get("effort"),
                   temperature=c.get("temperature", 0.2),
                   max_output_tokens=c.get("max_output_tokens", 8192))
    t = time.time()
    try:
        turn = gw.generate_native([{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                                  tools=tools)
        u = gw.last_usage or {}
        return {"ok": True, "endpoint": endpoint, "served_by": u.get("served_by"),
                "seconds": round(time.time() - t, 1),
                "prompt_tokens": u.get("prompt_tokens"),
                "completion_tokens": u.get("completion_tokens"),
                "reasoning_chars": len(turn.reasoning_content or ""),
                "truncated": turn.truncated, "text": turn.text,
                "tool_calls": [{"name": t["function"]["name"],
                                "args": t["function"]["arguments"][:600]}
                               for t in (turn.tool_calls or [])]}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "endpoint": endpoint,
                "seconds": round(time.time() - t, 1),
                "error": f"{type(e).__name__}: {str(e)[:300]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--step-like", default="t_impl")
    ap.add_argument("--min-len", type=int, default=50000)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--step-row", type=int)
    ap.add_argument("--endpoints", nargs="+", default=["localqwen/qwen3"])
    ap.add_argument("--out", default="/tmp/replay")
    ap.add_argument("--preamble-steps", nargs="*", default=None,
                    help='steps hoisted into the cached preamble (default 1 2); '
                         'pass e.g. "1" to drop the architecture and see whether '
                         'the agent reads it on demand instead')
    ap.add_argument("--drop-context", nargs="*", default=None,
                    help='resolved_context keys to remove entirely, e.g. "Step 2" — '
                         'use WITH --preamble-steps so the content is absent rather '
                         'than relocated')
    ap.add_argument("--nudge", action="store_true",
                    help="append a tool-discipline block; tests whether narrow "
                         "reads are a capability limit or a prompting gap")
    ap.add_argument("--compare-trace", action="store_true",
                    help="verify the reassembly against what was really sent, and "
                         "show what the original endpoint actually did")
    a = ap.parse_args()

    if a.list or not a.step_row:
        print(f"{'row':>7}  {'step':<14} {'config':<12} {'project':<20} {'chars':>8}  done")
        for r in rows(a.step_like + "%", a.min_len, a.limit):
            print(f"{r[0]:>7}  {r[1]:<14} {r[2]:<12} {r[3]:<20} {r[4]:>8}  {r[5]}")
        return 0

    step_id, inputs, graph_name, step_cfg, project_id = load(a.step_row)
    system, user, cfg = build_prompt(step_id, inputs, graph_name, step_cfg,
                                     code_path_for(project_id), a.preamble_steps,
                                     a.drop_context)
    if a.nudge:
        # Appended where the turn budget goes — past the cache-prefix boundary,
        # so testing it costs no cache churn. If the model complies, narrow
        # reads are a PROMPTING gap and this belongs in the role template; if it
        # ignores them, it is a capability limit and no template fixes it.
        user += (
            "\n\n[Tool Discipline — READ NARROWLY]\n"
            "Context is the scarce resource in this step. Before reading a file "
            "whole, ask whether you need all of it.\n"
            "- To FIND something, call `search` with a `glob` narrowing the files "
            "and `context_lines` for the surrounding lines. Do not read a file "
            "just to look for a symbol in it.\n"
            "- To READ a known region, pass `start_line` and `end_line`. Reading "
            "a 900-line script whole to change 40 lines spends context you will "
            "need for the edit itself.\n"
            "- Read a file whole only when you genuinely need all of it (a short "
            "config, a file you are about to rewrite end to end)."
        )
    out = Path(a.out) / f"row{a.step_row}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.system.txt").write_text(system, encoding="utf-8")
    (out / "prompt.user.txt").write_text(user, encoding="utf-8")
    print(f"  step {step_id} (row {a.step_row})  role={cfg.get('name')}  "
          f"system={len(system):,} user={len(user):,} chars")
    print(f"  prompt saved to {out} — later runs MUST reuse it, not reassemble")

    if a.compare_trace:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        pid = c.execute("""SELECT r.project_id FROM skillflow_steps s
                           JOIN skillflow_runs r ON r.id=s.run_id WHERE s.id=?""",
                        (a.step_row,)).fetchone()
        tr = traced(a.step_row, pid[0]) if pid else {}
        if tr.get("system"):
            # Two benign differences must be normalised out, or the check cries
            # wolf and a real drift hides behind the noise:
            #   * the trace appends its own "…[clipped N chars]" truncation tail
            #   * build_today_block() stamps the CURRENT date, so any replay on a
            #     later day differs by exactly that line
            import re as _re
            def _norm(t: str) -> str:
                t = _re.sub(r"\s*…\[clipped [0-9,]+ chars\].*$", "", t, flags=_re.S)
                return _re.sub(r"The current date is \d{4}-\d{2}-\d{2}", "DATE", t)
            ts, tu = _norm(tr["system"]), _norm(tr["user"])
            ok_s = _norm(system)[:len(ts)] == ts
            ok_u = _norm(user)[:len(tu)] == tu
            n, m = len(ts), len(tu)
            print(f"\n  FIDELITY vs what was really sent "
                  f"(compared over {n:,}/{m:,} chars, date + clip-marker normalised):")
            print(f"    system prefix matches: {ok_s}")
            print(f"    user   prefix matches: {ok_u}")
            if not (ok_s and ok_u):
                print("    -> reassembly DRIFTED; the workspace moved since. Any "
                      "comparison below is against a different prompt.")
        print(f"\n  ORIGINAL run: served_by={tr.get('served_by')} "
              f"attempt={tr.get('attempt')} reasoning={tr.get('reasoning_chars')} chars")
        if tr.get("text"):
            print(f"      said: {tr['text'][:150]}")
        for t in tr.get("tool_calls", [])[:8]:
            print(f"      tool: {t}")
        print()

    for ep in [e for e in a.endpoints if e.lower() != "none"]:
        r = ask(ep, system, user, cfg, inputs.get("_tool_schemas"))
        (out / f"answer.{ep.replace('/', '_')}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        if r["ok"]:
            print(f"  {ep:26} {r['seconds']:>6.1f}s  prompt={r['prompt_tokens']} "
                  f"completion={r['completion_tokens']} reasoning={r['reasoning_chars']} "
                  f"chars={len(r['text'])} truncated={r['truncated']}")
            for tc in r["tool_calls"]:
                print(f"      tool: {tc['name']}({tc['args'][:110]})")
        else:
            print(f"  {ep:26} {r['seconds']:>6.1f}s  {r['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
