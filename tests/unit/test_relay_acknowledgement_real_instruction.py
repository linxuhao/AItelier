"""The relay acknowledgement gate against the recorded f835fa35 instruction.

rev 3 criteria, one test group each:

* ``a-faithful-acknowledgement-passes-once`` - every recorded ``must_accept``
  restatement is accepted on its first call, on the real instruction and on
  the 1K / 10K / 50K synthetic tiers.
* ``an-arbitrary-acknowledgement-is-still-refused`` - wrong retained bytes,
  empty list, empty entry, ``banana``, a count-matching set of unrelated
  English sentences, another card's four closing tasks, and each single
  dropped item of a faithful restatement.
* ``non-ascii-work-is-counted`` - Chinese-only remaining work: a faithful
  Chinese restatement passes, unrelated Chinese (including a sentence built
  from the instruction's own high-frequency characters) does not.

The polarity mutants this file must go red on: per-item half-recall (the r2
pass line) must name a ``must_accept`` entry; count-matching alone and
any-non-empty-entry must name their own shape.
"""

import json
from pathlib import Path

from core.dpe_pipeline import (
    _relay_acknowledgement,
    _relay_progress_context,
    _relay_work_items,
)


FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "relay_ack_f835fa35.json")
    .read_text(encoding="utf-8")
)
REAL_INSTRUCTION = FIXTURE["instruction"]
REAL_RETAINED = FIXTURE["retained_bytes"]
MUST_ACCEPT = FIXTURE["must_accept"]


def real_relay() -> dict:
    return _relay_progress_context({
        "instruction": REAL_INSTRUCTION,
        "relay": {
            "run_id": "f835fa35",
            "error": "native turn budget exhausted (200/200)",
            "code_changes": {"files": {
                "docker/godot/godot_harness.py": {"bytes": REAL_RETAINED},
            }},
        },
    })


# --- the synthetic tiers, same shape r2 measured ---------------------------

SYN_RETAINED = 195198 + 8817
SYN_TIERS = (1024, 10240, 51200)

SYN_TASKS = [
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

FAITHFUL_EN = [
    "Rerun the render-queue tests and the harness test family, confirming "
    "the numbers",
    "Run pytest tests/ and record the bare RC in a .txt log under logs/",
    "Finish section 3 of the delivery notes, log file name next to every "
    "number",
    "Call finish_step; the four mutant kills each bare RC stay established, "
    "so no established part is rewritten",
]

SYN_CJK_TASKS = [
    "重跑新增的渲染队列测试并确认数字",
    "跑全套测试并把裸退出码写进日志",
    "补全交付说明第三节的最后两条判据",
    "调用 finish_step 收尾",
]

SYN_CJK_FAITHFUL = [
    "重跑新增渲染队列测试，确认数字",
    "跑完整套测试，把裸退出码记录进日志",
    "补全交付说明第三节末尾两条判据",
    "调用 finish_step 完成收尾",
]


def synthetic_instruction(total_chars: int, tasks: list[str]) -> str:
    header = "\n".join(f"{i}. {task}" for i, task in enumerate(tasks, 1))
    body = "原始简报正文延续，承载已批准的计划。" * ((total_chars // 18) + 1)
    return header + "\n\n" + body


def synthetic_relay(instruction: str) -> dict:
    return _relay_progress_context({
        "instruction": instruction,
        "relay": {
            "run_id": "relay-r3-synthetic",
            "error": "native turn budget exhausted (32/32)",
            "code_changes": {"files": {
                "core/dpe_pipeline.py": {"bytes": 195198},
                "core/write_scope.py": {"bytes": 8817},
            }},
        },
    })


# --- entries that are not the remaining work ------------------------------

UNRELATED_EN = [
    "Update README.md with a usage example for the new flag",
    "Rewrite the changelog entry for the previous release",
    "Translate the glossary and fix its spelling mistakes",
    "Reorder the imports in the packaging script",
]

OTHER_CARD_CJK = [
    "改 README 的用法示例",
    "跑另一族的冒烟测试",
    "把变更日志补到上一版",
    "整理词汇表并把拼写错误改掉",
]

HIGH_FREQUENCY_CJK = "把一个与在的用法写清楚，再讲一个无关的苹果与天气预报。"

# --- 中文承载的剩余工作（判据 non-ascii-work-is-counted） -------------------

CJK_ONLY_TASKS = [
    "把渲染队列的等待时间记进日志文件",
    "重跑引擎家族的回归测试",
    "补全交付说明第三节的判据",
    "收尾时调用提交步骤",
]

CJK_ONLY_FAITHFUL = [
    "渲染队列等待时间已记进日志",
    "引擎家族回归测试已重跑",
    "交付说明第三节判据已补全",
    "提交步骤已调用收尾",
]

CJK_ONLY_UNRELATED = [
    "把一个与在的用法写成例子，再讲一个无关的苹果。",
    "天气预报说有雨，记得把伞带上，与工作无关。",
    "把苹果与香蕉放在一起，然后说一个无关的笑话。",
    "整理无关的词汇表，把拼写错误全部改掉。",
]


def _refusal_shapes(relay: dict, faithful: list[str]) -> list[tuple[str, dict]]:
    retained = relay["retained_bytes"]
    shapes = [
        ("wrong retained_bytes",
         {"retained_bytes": retained + 1, "incomplete_items": list(faithful)}),
        ("empty list", {"retained_bytes": retained, "incomplete_items": []}),
        ("empty entry", {"retained_bytes": retained, "incomplete_items": [""]}),
        ("banana", {"retained_bytes": retained,
                    "incomplete_items": ["banana"]}),
        ("unrelated english sentences",
         {"retained_bytes": retained, "incomplete_items": list(UNRELATED_EN)}),
        ("another card's closing tasks",
         {"retained_bytes": retained, "incomplete_items": list(OTHER_CARD_CJK)}),
        ("high-frequency chinese",
         {"retained_bytes": retained,
          "incomplete_items": [HIGH_FREQUENCY_CJK]}),
    ]
    for drop in range(len(faithful)):
        shapes.append((
            f"dropped item {drop + 1}",
            {"retained_bytes": retained,
             "incomplete_items": [i for n, i in enumerate(faithful) if n != drop]},
        ))
    return shapes


# --- a-faithful-acknowledgement-passes-once -------------------------------


def test_recorded_acknowledgements_pass_on_the_real_instruction():
    relay = real_relay()
    assert len(relay["incomplete_items"]) == 4, relay["incomplete_items"]
    for index, entry in enumerate(MUST_ACCEPT):
        ok, result = _relay_acknowledgement(relay, dict(entry))
        assert ok, f"must_accept[{index}] was refused: {result}"
        assert result["status"] == "acknowledged"


def test_the_real_brief_is_not_what_the_pass_line_weighs():
    items = _relay_work_items(REAL_INSTRUCTION)
    assert len(items) == 4, items
    for item in items:
        assert item in REAL_INSTRUCTION
    assert len("".join(items)) < len(REAL_INSTRUCTION) // 5


def test_synthetic_tiers_still_accept_faithful_restatements():
    for total in SYN_TIERS:
        relay = synthetic_relay(synthetic_instruction(total, SYN_TASKS))
        assert relay["incomplete_items"] == SYN_TASKS
        for label, items in (("paraphrase", FAITHFUL_EN),
                             ("verbatim", SYN_TASKS)):
            ok, result = _relay_acknowledgement(
                relay, {"retained_bytes": SYN_RETAINED,
                        "incomplete_items": list(items)})
            assert ok, (total, label, result)


def test_synthetic_cjk_tier_accepts_faithful_restatement():
    for total in SYN_TIERS:
        relay = synthetic_relay(synthetic_instruction(total, SYN_CJK_TASKS))
        assert relay["incomplete_items"] == SYN_CJK_TASKS
        ok, result = _relay_acknowledgement(
            relay, {"retained_bytes": SYN_RETAINED,
                    "incomplete_items": list(SYN_CJK_FAITHFUL)})
        assert ok, (total, result)


# --- an-arbitrary-acknowledgement-is-still-refused -------------------------


def test_arbitrary_acknowledgements_are_refused_on_every_tier():
    tiers = [("real", real_relay(), MUST_ACCEPT[2]["incomplete_items"])]
    for total in SYN_TIERS:
        tiers.append((
            f"synthetic-{total}",
            synthetic_relay(synthetic_instruction(total, SYN_TASKS)),
            FAITHFUL_EN,
        ))
    for tier, relay, faithful in tiers:
        for shape, params in _refusal_shapes(relay, faithful):
            ok, result = _relay_acknowledgement(relay, params)
            assert not ok, f"{tier} / {shape} escaped: {result}"
            assert result["status"] == "denied"


def test_another_card_and_unrelated_chinese_are_refused_on_cjk_only_work():
    relay = synthetic_relay(synthetic_instruction(2048, CJK_ONLY_TASKS))
    assert relay["incomplete_items"] == CJK_ONLY_TASKS
    for shape, items in (("faithful", CJK_ONLY_FAITHFUL),
                         ("another card", OTHER_CARD_CJK),
                         ("unrelated", CJK_ONLY_UNRELATED)):
        ok, result = _relay_acknowledgement(
            relay, {"retained_bytes": SYN_RETAINED,
                    "incomplete_items": list(items)})
        if shape == "faithful":
            assert ok, result
        else:
            assert not ok, f"{shape} escaped: {result}"
            assert result["status"] == "denied"


def test_cjk_only_work_refuses_each_dropped_item():
    relay = synthetic_relay(synthetic_instruction(2048, CJK_ONLY_TASKS))
    for drop in range(len(CJK_ONLY_FAITHFUL)):
        items = [i for n, i in enumerate(CJK_ONLY_FAITHFUL) if n != drop]
        ok, result = _relay_acknowledgement(
            relay, {"retained_bytes": SYN_RETAINED, "incomplete_items": items})
        assert not ok, f"dropped item {drop + 1} escaped: {result}"
