"""A native program's prose is not its name.

Measured 2026-10-07 23:28Z: six audited-override measurements in a row were
refused because two open Claude Code desktop sessions (ccd-cli, ~52 KB of argv
carrying a system prompt with "review" and "judge") were reported as
unregistered evaluators. Their exe link read '<path> (deleted)' after a CLI
update, which also defeated the existing native-launch narrowing.

Each claim has its opposite pole: a native program NAMED like an evaluator, a
binary whose exe link does not corroborate argv, and interpreter/shell
launches carrying the same prose all still block.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from core import deployment_quiescence as dq

PROSE = ["--append-system-prompt", "no human review; the judge decides; benchmark it"]


def _probe(*lines):
    def runner(command):
        out = "" if command[0] == "docker" else "\n".join(lines) + "\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return runner


def _proc(root, pid, words, exe, comm):
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(w.encode() for w in words) + b"\0")
    (entry / "comm").write_text(comm + "\n")
    os.symlink(exe, entry / "exe")
    return f"{pid} 1 {' '.join(words)}"


def _measure(tmp_path, monkeypatch, words, exe=None, comm=None):
    root = tmp_path / "proc"
    monkeypatch.setattr(dq, "PROC_ROOT", root)
    line = _proc(root, 4242, words, exe or words[0], comm or os.path.basename(words[0])[:15])
    return dq.external_owners(runner=_probe(line))


def _flagged(result):
    owners, errors = result
    return (any(o.get("resource") == "external_measurement" and o["active"] for o in owners),
            len(errors))


def test_an_agent_cli_with_prompt_prose_is_not_an_evaluator(tmp_path, monkeypatch):
    words = ["/home/u/.claude/remote/ccd-cli/2.1.284", "--model", "claude-opus-5-5", *PROSE]
    assert _flagged(_measure(tmp_path, monkeypatch, words)) == (False, 0)


def test_a_binary_replaced_on_disk_is_still_the_program_it_launched(tmp_path, monkeypatch):
    words = ["/home/u/.claude/remote/ccd-cli/2.1.284", *PROSE]
    result = _measure(tmp_path, monkeypatch, words, exe=words[0] + " (deleted)")
    assert _flagged(result) == (False, 0)


def test_a_native_program_named_like_an_evaluator_still_blocks(tmp_path, monkeypatch):
    words = ["/opt/eval/grader_worker", "--shard", "3"]
    assert _flagged(_measure(tmp_path, monkeypatch, words)) == (True, 1)


def test_an_uncorroborated_exe_link_keeps_the_full_scan(tmp_path, monkeypatch):
    words = ["/home/u/bin/cli", *PROSE]
    result = _measure(tmp_path, monkeypatch, words, exe="/usr/bin/something-else")
    assert _flagged(result) == (True, 1)


def test_shell_node_and_python_inline_launches_keep_the_full_scan(tmp_path, monkeypatch):
    cases = [
        (["/bin/bash", "-c", "run " + PROSE[1]], "bash"),
        (["/usr/bin/node", "agent.js", *PROSE], "node"),
        (["/usr/bin/python3", "-c", "print('" + PROSE[1] + "')"], "python3"),
    ]
    for i, (words, comm) in enumerate(cases):
        sub = tmp_path / str(i)
        sub.mkdir()
        assert _flagged(_measure(sub, monkeypatch, words, comm=comm)) == (True, 1), words[0]


def test_long_gate_on_a_native_program_still_blocks(tmp_path, monkeypatch):
    words = ["/home/u/bin/cli", "--long-gate"]
    assert _flagged(_measure(tmp_path, monkeypatch, words)) == (True, 1)


def test_a_native_godot_render_is_still_an_active_render(tmp_path, monkeypatch):
    words = ["/usr/local/bin/godot", "--headless", "--script", "render_shots.gd"]
    owners, errors = _measure(tmp_path, monkeypatch, words)
    assert [(o["active"], o["resource"]) for o in owners] == [(True, "render")]
    assert errors == []
