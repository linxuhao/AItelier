"""The relay acknowledgement gate: reading, not recall.

The pass line must not grow with the brief the relay rides on, must not be
blind to non-ASCII remaining work, and a faithful restatement must clear it
on the first try. Mutant polarity: any non-empty entry passing is a red.
"""
import json

from core.dpe_pipeline import (
    _relay_acknowledgement,
    _relay_progress_context,
    _relay_work_items,
)


TASKS = [
    "Validate the unvalidated draft: rerun the new render-queue tests "
    "(tests/unit/test_harness_render_queue.py) and the harness test family "
    "on this worktree, and confirm the numbers",
    "Run the full `pytest tests/` with a .txt log in logs/ and record "
    "passed/skipped and the bare RC.",
    "Finish section 3 of final/delivery_notes_harness_render_queue.md "
    "(third and fourth criteria), writing the log file name next to every "
    "number.",
    "Call finish_step; do not rewrite parts already established (four mutant "
    "kills, each bare RC 1).",
]


def relay_instruction(total_chars: int, cjk: bool = False) -> str:
    header = "\n".join(f"{i}. {task}" for i, task in enumerate(TASKS, 1))
    filler = (
        "剩余工作以中文承载，只有完成了这些条目，本轮才算收尾。"
        if cjk
        else "The retained brief follows verbatim and carries the approved plan."
    )
    body = []
    size = 0
    while size < total_chars:
        chunk = filler
        body.append(chunk)
        size += len(chunk)
    return header + "\n\n" + " ".join(body)


def seed_context(instruction: str) -> dict:
    return {
        "instruction": instruction,
        "relay": {
            "run_id": "relay-gate-check",
            "error": "native turn budget exhausted (32/32)",
            "code_changes": {"files": {
                "core/dpe_pipeline.py": {"bytes": 195198},
                "core/write_scope.py": {"bytes": 8817},
            }},
        },
    }


def relay_for(instruction: str):
    return _relay_progress_context(seed_context(instruction))


RETAINED = 204015


def test_header_tasks_become_the_work_items_at_every_length():
    for total in (1024, 10240, 51200):
        items = _relay_work_items(relay_instruction(total))
        assert items == TASKS


def test_faithful_acknowledgement_passes_first_try_at_every_length():
    for total in (1024, 10240, 51200):
        relay = relay_for(relay_instruction(total))
        assert relay["incomplete_items"] == TASKS
        ok, result = _relay_acknowledgement(relay, {
            "retained_bytes": RETAINED,
            "incomplete_items": [
                "Rerun the render-queue tests and the harness test family, "
                "confirming the numbers",
                "Run pytest tests/ and record the bare RC in a .txt log under logs/",
                "Finish section 3 of the delivery notes, log file name next to "
                "every number",
                "Call finish_step; the four mutant kills each bare RC stay "
                "established, so no established part is rewritten",
            ],
        })
        assert ok, result
        assert result["status"] == "acknowledged"


def test_goal_verbatim_acknowledgement_is_accepted():
    relay = relay_for(relay_instruction(51200))
    ok, result = _relay_acknowledgement(relay, {
        "retained_bytes": RETAINED,
        "incomplete_items": list(TASKS),
    })
    assert ok, result


def test_wrong_bytes_or_partial_or_arbitrary_items_are_refused():
    for total in (1024, 10240, 51200):
        relay = relay_for(relay_instruction(total))
        wrong_bytes = {
            "retained_bytes": RETAINED + 1,
            "incomplete_items": list(TASKS),
        }
        ok, _ = _relay_acknowledgement(relay, wrong_bytes)
        assert not ok
        for supplied in (
            [],
            [""],
            ["banana"],
            ["banana", TASKS[0]],
            [TASKS[0], TASKS[1]],  # only part of the remaining work
        ):
            ok, denied = _relay_acknowledgement(
                relay,
                {"retained_bytes": RETAINED, "incomplete_items": supplied},
            )
            assert not ok, supplied
            assert denied["status"] == "denied"


def test_cjk_remaining_work_is_counted_not_ignored():
    instruction = relay_instruction(10240, cjk=True)
    relay = relay_for(instruction)
    assert relay["incomplete_items"] == TASKS
    ok, result = _relay_acknowledgement(relay, {
        "retained_bytes": RETAINED,
        "incomplete_items": list(TASKS),
    })
    assert ok, result

    unrelated = {
        "retained_bytes": RETAINED,
        "incomplete_items": ["重写已经写好的全部文件，然后直接提交一切。"],
    }
    ok, denied = _relay_acknowledgement(relay, unrelated)
    assert not ok
    assert denied["status"] == "denied"


CJK_TASKS = [
    "重跑新增的渲染队列测试并确认数字",
    "跑全套测试并把裸退出码写进日志",
    "补全交付说明第三节的最后两条判据",
    "调用 finish_step 收尾",
]


def _cjk_instruction(total_chars: int) -> str:
    header = "\n".join(f"{i}. {task}" for i, task in enumerate(CJK_TASKS, 1))
    body = ("原始简报正文延续，承载已批准的计划。" * ((total_chars // 18) + 1))
    return header + "\n\n" + body


def test_chinese_work_faithfully_restated_passes_once_at_every_length():
    for total in (1024, 10240, 51200):
        relay = relay_for(_cjk_instruction(total))
        assert relay["incomplete_items"] == CJK_TASKS
        paraphrase = [
            "重跑新增渲染队列测试，确认数字",
            "跑完整套测试，把裸退出码记录进日志",
            "补全交付说明第三节末尾两条判据",
            "调用 finish_step 完成收尾",
        ]
        ok, result = _relay_acknowledgement(
            relay, {"retained_bytes": RETAINED, "incomplete_items": paraphrase}
        )
        assert ok, result

        ok, denied = _relay_acknowledgement(
            relay,
            {"retained_bytes": RETAINED,
             "incomplete_items": ["随便说一点无关的中文内容"]},
        )
        assert not ok, denied
        assert denied["status"] == "denied"

        ok, denied = _relay_acknowledgement(
            relay,
            {"retained_bytes": RETAINED, "incomplete_items": CJK_TASKS[:2]},
        )
        assert not ok, denied


def test_short_unstructured_instruction_keeps_whole_instruction_fallback():
    instruction = "Finish dpe_pipeline wiring and add targeted tests."
    relay = relay_for(instruction)
    assert relay["incomplete_items"] == [instruction]


def test_relay_seed_from_state_goal_text_is_parsed():
    seed = "# State goal attempt\n\n" + json.dumps(
        seed_context(relay_instruction(2048))) + "\n\n## Relay\ncontinue\n"
    found = _relay_progress_context({"coding_impl/plan.md": seed})
    assert found["incomplete_items"] == TASKS
    assert found["retained_bytes"] == RETAINED
