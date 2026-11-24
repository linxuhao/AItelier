"""The owner's language (core/owner_text.py; overfit review 重-17, 重-18): one setting, the 'zh'
table byte for byte what a1416299ee wrote, English by default, and the same limit the plugin
cuts a channel copy at."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import owner_text, state_groups, state_steward_confirmations as sconf

REPO = Path(__file__).resolve().parents[2]
CJK = re.compile("[　-〿㐀-鿿＀-￯]")

# ---- a1416299ee, verbatim: each expression as the code wrote it, over the same values.
# core/state_owner_questions.py (push, reminders, notices, receipts) ...
APPROVAL_PHRASE_PUSH = "授权类问题（合并、重启、花钱）也这样答：「Q{code} 同意」。批准提议的那句口令是另一回事，不关问题，提醒照发。"
APPROVAL_PHRASE_NOTE = "授权类问题也只认「Q编号 你的话」；批准提议的那句口令不关问题。"
TODAY = {
    "push_continued": lambda v: "【待你回答】（续）",
    "push_new": lambda v: f"{v['n']} 个新问题",
    "push_older": lambda v: f"{v['n']} 个较早的问题",
    "push_head": lambda v: (f"【待你回答】{v['count']}。回复时以编号开头，例如「Q{v['first']} 用方案 A」，"
                            "你的原话会转给提问的导演；改主意就再发一条，以后来的为准。也可以在面板回答。\n"),
    "approval_phrase_push": lambda v: APPROVAL_PHRASE_PUSH.format(code=v["code"]),
    "approval_phrase_note": lambda v: APPROVAL_PHRASE_NOTE,
    "reminder_head": lambda v: f"【提醒】还有 {v['n']} 个问题等你回答：",
    "question_line": lambda v: f"Q{v['code']} · {v['title']}：｜{v['line']}",
    "reminder_tail": lambda v: f"回复「Q{v['code']} 你的回答」即可，也可以在面板回答。",
    "older_head": lambda v: "较早的问题：",
    "more_on_dashboard": lambda v: f"另有 {v['n']} 个，见面板。",
    "closed_dashboard": lambda v: "已在面板回答",
    "closed_withdrawn": lambda v: "已撤回",
    "closed_wechat": lambda v: "已在微信回答",
    "closed_slack": lambda v: "已在 Slack 回答",
    "closed_group": lambda v: f"{v['label']}：{v['items']}。",
    "closed_item": lambda v: f"Q{v['code']}（{v['title']}）",
    "closed_sep": lambda v: "、",
    "closed_join": lambda v: "",
    "block": lambda v: f"Q{v['code']} · {v['title']}\n问：\n{v['text']}",
    "block_context": lambda v: f"\n背景：\n{v['context']}",
    "no_code_none": lambda v: "没有认出编号，现在也没有等你回答的已推问题。\n",
    "no_code_head": lambda v: "没有认出编号。你是在回答下面哪个问题？",
    "no_code_tail": lambda v: f"回「Q{v['code']}」即把上一条原样转过去。\n",
    "unknown_code": lambda v: f"{v['label']} 找不到这个编号",
    "withdrawn": lambda v: f"{v['label']} 已撤回（{v['reason']}），面板上仍可以答",
    "withdrawn_newer": lambda v: f"{v['label']} 已撤回（{v['reason']}）；导演改问了 Q{v['code']}：{v['line']}",
    "duplicate": lambda v: f"{v['label']} 这条已经转过了",
    "superseded": lambda v: f"{v['label']} 没有转：你之后又答过（{v['when']}），以后来的为准",
    "no_recipients": lambda v: f"{v['label']} 没有转：这个项目现在没有导演",
    "relayed": lambda v: f"已转达 {v['label']}{' →'} {v['title']}：{v['line']}",
    "relayed_further": lambda v: f"已转达 {v['label']}{'（补充回答）→'} {v['title']}：{v['line']}",
    # ... core/state_steward_confirmations.py confirmation_text ...
    "confirm_header": lambda v: (f"请确认（{v['code']}，10 分钟内有效）：用「{v['label']}」执行 {v['tool']}。"
                                 f"回复「{v['at']}{'确认 '}{v['code']}」执行，「{v['at']}{'取消 '}{v['code']}」作废。"),
    "confirm_phrase": lambda v: "确认 ",
    "cancel_phrase": lambda v: "取消 ",
    "confirm_short": lambda v: f"{v['key']}：{v['text']}",
    "confirm_long": lambda v: f"{v['key']}（长文）：\n{v['text']}",
    "confirm_footer": lambda v: "—— 以上是全部内容 ——",
    # ... and core/state_groups.py CONFIRMATION_LINES.
    "group_confirmed": lambda v: "已确认 {code}，正在执行。".format(code=v["code"]),
    "group_cancelled": lambda v: "已取消 {code}，不会执行。".format(code=v["code"]),
    "group_expired": lambda v: "{code} 已过期，未执行；请重新请求。".format(code=v["code"]),
    "group_refused": lambda v: "{code} 未执行：消息时间不对，请重新回复。".format(code=v["code"]),
    # ... core/state_groups.py: the group's fixed lines and the owner's line about a withheld reply ...
    "group_withheld": lambda v: "这个我在群里答不了。",
    "group_quota": lambda v: "问得太多了，过一会儿再问。",
    "group_bind": lambda v: ("管家已加入本群。@ 我提问即可；我在群里只回答公开项目和一般问题，不能替任何人做决定，也不传话；私聊我不会回复。"
                             "@ 我的消息会保存 30 天，业主看得到；它们会交给外部模型服务处理，管家自己的会话记录里可能留得更久。"
                             "让我记住的事只在本群用，30 天后删。"),
    "group_media_on": lambda v: ("本群已打开存图：群里没 @ 管家的图片、语音、文件，管家会先存下、不回话，存 1 天后自动删除。"
                                 "只有之后在本群 @ 管家时，管家才会去看（看时会交给外部模型服务处理）；业主私聊和别的群都用不到。"
                                 "业主在面板上看得到谁发过、是什么类型，看不到内容。关闭存图时，已存的一并删除。"),
    "group_media_off": lambda v: "本群已关闭存图：管家不再存群里没 @ 它的图片、语音、文件，之前存下的已经删除。",
    "withheld_owner_line": lambda v: "{group}里有一条回答没发出去：{reason}。要问可以私聊我。".format(
        group=v["group"], reason=v["reason"]),
    "withheld_live_code": lambda v: "含有绑定码",
    "withheld_private_project": lambda v: "涉及私有项目",
    "withheld_host_path": lambda v: "含有主机上的路径",
    # ... and core/state_owner_questions.py _bar.
    "bar": lambda v: "｜",
}
# ---- end of the copy

VALUES = [
    {"n": 3, "count": "3 个新问题", "first": 12, "code": 12, "title": "alpha", "line": "用哪个方案？", "label": "Q12",
     "items": "Q1（a）、Q2（b）", "text": "｜第一行\n｜第二行", "context": "｜背景", "reason": "不需要了", "when": "11-20 11:00",
     "tool": "send_message", "at": "", "key": "to", "confirm": "确认 ", "cancel": "取消 ", "group": "家庭群"},
    {"n": 0, "count": "", "first": 1, "code": "K7Q2A", "title": "{x} 带括号 {0}", "line": "", "label": "Q1", "items": "",
     "text": "", "context": "", "reason": "", "when": "", "tool": "{tool}", "at": "<@管家> ", "key": "{k}",
     "confirm": "确认 ", "cancel": "取消 ", "group": "{group} 老家"},
]


@pytest.fixture(autouse=True)
def _no_ambient_setting(monkeypatch, tmp_path):
    """The table's own tests never read this machine's .env or environment."""
    monkeypatch.delenv(owner_text.SETTING, raising=False)
    monkeypatch.setattr(owner_text, "DOTENV_PATH", tmp_path / "absent.env")


def test_zh_is_byte_for_byte_what_a1416299ee_wrote():
    zh = owner_text.TEXTS["zh"]
    assert set(zh) == set(TODAY)
    for key, today in TODAY.items():
        for values in VALUES:
            assert zh[key].format(**values) == today(values), key
    assert owner_text.PUSH_CHARS["zh"] == 600


def test_en_has_every_key_and_not_one_chinese_character():
    en, zh = owner_text.TEXTS["en"], owner_text.TEXTS["zh"]
    assert set(en) == set(zh)
    for key, template in en.items():
        assert not CJK.search(template), key
        # The same placeholders, so a caller formats either language with the same values.
        assert set(re.findall(r"{(\w+)}", template)) == set(re.findall(r"{(\w+)}", zh[key])), key
    assert en["confirm_phrase"] == sconf.CONFIRM_EN and en["cancel_phrase"] == sconf.CANCEL_EN
    assert zh["confirm_phrase"] == sconf.CONFIRM and zh["cancel_phrase"] == sconf.CANCEL


@pytest.mark.parametrize("value,expected", [
    (None, "en"), ("", "en"), ("en", "en"), ("EN-us", "en"), ("zh", "zh"), ("ZH", "zh"), ("zh-CN", "zh"),
    ("zh_Hans", "zh"), (" zh ", "zh"), ("fr", "en"), ("chinese", "en")])
def test_one_setting_by_its_primary_subtag_unknown_is_english(value, expected):
    assert owner_text.normalise(value) == expected


def test_the_setting_is_the_environment_else_the_repository_env_else_english(tmp_path):
    dotenv = tmp_path / ".env"
    assert owner_text.owner_language({}, dotenv) == "en", "no .env: the default"
    dotenv.write_text("# a comment\nAITELIER_PORT=4444\nexport AITELIER_OWNER_LANGUAGE=\"zh\"\nAITELIER_OWNER_LANGUAGE=en\n")
    assert owner_text.owner_language({}, dotenv) == "zh", ".env, first assignment wins (core.dotenv_boot)"
    assert owner_text.owner_language({"AITELIER_OWNER_LANGUAGE": "en"}, dotenv) == "en", "the environment wins"
    assert owner_text.owner_language({"AITELIER_OWNER_LANGUAGE": ""}, dotenv) == "en", "set but empty is the default"
    dotenv.write_bytes(b"AITELIER_OWNER_LANGUAGE=\xff\n")
    assert owner_text.owner_language({}, dotenv) == "en", "an unreadable .env is the default, never a crash"
    assert owner_text.owner_language({}, tmp_path) == "en", "a directory is no .env"


def test_texts_follows_the_setting_at_call_time(monkeypatch):
    assert owner_text.texts() is owner_text.TEXTS["en"]
    monkeypatch.setenv(owner_text.SETTING, "zh-CN")
    assert owner_text.texts() is owner_text.TEXTS["zh"]
    assert owner_text.texts("en") is owner_text.TEXTS["en"]


def test_the_withheld_sentence_is_the_one_each_languages_steward_prompt_teaches():
    """The group gets the same words the model is told to use for "cannot say here" (7.7.7): the
    'zh' words are steward.md's, the 'en' words the plugin's English prompt puts in their place."""
    steward_md = (REPO / "integrations/dsh-director/steward.md").read_text(encoding="utf-8")
    source = (REPO / "integrations/dsh-director/lib/owner-text.mjs").read_text(encoding="utf-8")
    zh, en = owner_text.TEXTS["zh"]["group_withheld"], owner_text.TEXTS["en"]["group_withheld"]
    assert f"「{zh}」" in steward_md
    assert f"['「{zh}」', '\"{en}\"']" in source, "lib/owner-text.mjs EN_STEWARD_PROMPT"


def test_the_push_limit_is_the_plugins_channel_limit_for_each_language():
    source = (REPO / "integrations/dsh-director/lib/owner-text.mjs").read_text()
    found = re.search(r"export const MIRROR_CHARS = Object\.freeze\(\{ en: (\d+), zh: (\d+) \}\);", source)
    assert found, "lib/owner-text.mjs MIRROR_CHARS"
    assert {"en": int(found[1]), "zh": int(found[2])} == dict(owner_text.PUSH_CHARS)


def test_the_confirmation_text_and_the_group_line_follow_the_language(monkeypatch):
    args = {"to": "a@example.com", "body": "line one\nline two"}
    monkeypatch.setenv(owner_text.SETTING, "zh")
    zh = sconf.confirmation_text("K7Q2A", "家庭公共邮箱", "send_message", args)
    assert zh.split("\n")[0] == ("请确认（K7Q2A，10 分钟内有效）：用「家庭公共邮箱」执行 send_message。"
                                 "回复「确认 K7Q2A」执行，「取消 K7Q2A」作废。")
    assert zh.endswith("\n—— 以上是全部内容 ——") and "to：a@example.com" in zh and "body（长文）：\nline one" in zh
    assert state_groups.confirmation_line("confirmed", "K7Q2A") == "已确认 K7Q2A，正在执行。"
    monkeypatch.setenv(owner_text.SETTING, "en")
    en = sconf.confirmation_text("K7Q2A", "Family mail", "send_message", args, mention="<@U0BOT>")
    assert en.split("\n")[0] == ('Please confirm (K7Q2A, valid for 10 minutes): run send_message with "Family mail". '
                                 'Reply "<@U0BOT> confirm K7Q2A" to run it, "<@U0BOT> cancel K7Q2A" to drop it.')
    assert not CJK.search(en)
    for outcome in state_groups.CONFIRMATION_OUTCOMES:
        assert "K7Q2A" in state_groups.confirmation_line(outcome, "K7Q2A")
        assert not CJK.search(state_groups.confirmation_line(outcome, "K7Q2A"))
