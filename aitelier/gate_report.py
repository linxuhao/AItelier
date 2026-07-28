"""Persist a forge gate's verdict where the maker can actually read it.

`feedback: true` on a tool-gate edge is supposed to hand `tool_result["error"]` to
the step the gate loops back to. It does not survive a backward loop-back:
`_inject_feedback_in_tx` updates rows `WHERE status = 'pending'`, and when a gate
routes back to a COMPLETED maker its next instance does not exist yet — the claim
path inserts it afterwards with a fresh `inputs_json`. The update matches zero rows
and the error is dropped. Measured on `forge-mcp-server-builder-95991a`: four
`emit_graph` step instances, not one of them carrying `_feedback`, and three
identical smoke failures in a row.

So the gates write their verdict to a FILE in their own step dir, and the maker
reads it with an ordinary `{source: {step: <gate>, file: gate_error.md}}` context
entry — the same rule the palette already gives for agent reviewers ("the maker
MUST read the reviewer's verdict"). Tool gates were the exception; the exception is
what broke. This keeps working regardless of the framework fix.

The log ACCUMULATES. The first version deleted the file when a gate passed, to
avoid showing a stale complaint — and immediately caused the failure mode that
choice was meant to be safer than: `forge-mcp-server-builder-a063e2` emit 2 fixed
the registry findings, emit 3 fixed the smoke and REINTRODUCED the registry ones,
because `v_registry`'s report had been deleted the moment it passed. skillflow's own
feedback log accumulates for exactly this reason — "a revision that silently reverts
an earlier round's fix gets rejected instead of passing blind". A pass is recorded
as a round too, so the maker reads "these were fixed — keep them fixed" rather than
silence.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

GATE_REPORT_FILE = "gate_error.md"
_ROUND_MARK = "## round "
# Bound the file so a long convergence cannot crowd the emitter's prompt. Trimming
# from the FRONT keeps the most recent rounds, which are the ones still in play.
_MAX_CHARS = 16000


def _next_round(text: str) -> int:
    return text.count(_ROUND_MARK) + 1


def write_gate_report(out_dir: str, gate: str, passed: bool, error: str) -> None:
    """Append this round's verdict to ``out_dir/gate_error.md``.

    Best-effort — a gate must never fail because it could not write its own report.
    """
    if not out_dir:
        return
    try:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        f = d / GATE_REPORT_FILE
        prior = f.read_text(encoding="utf-8") if f.exists() else ""
        if not prior:
            prior = (f"# Gate `{gate}` — findings log\n\n"
                     f"Every round this gate ran, oldest first. Findings marked FAILED "
                     f"must be fixed; findings from earlier rounds that now say PASSED "
                     f"were fixed and must STAY fixed — re-introducing one sends the "
                     f"run straight back here.\n")
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        n = _next_round(prior)
        if passed:
            body = ("PASSED — nothing to fix. Everything flagged above is resolved; "
                    "keep it that way.")
        else:
            body = (error or "no detail reported").strip()
        text = f"{prior}\n{_ROUND_MARK}{n} · {stamp} · {'PASSED' if passed else 'FAILED'}\n\n{body}\n"
        if len(text) > _MAX_CHARS:
            text = ("# Gate `%s` — findings log\n\n…[earlier rounds dropped]\n%s"
                    % (gate, text[-_MAX_CHARS:]))
        f.write_text(text, encoding="utf-8")
    except Exception:
        pass
