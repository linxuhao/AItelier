"""The fixed text the backend writes for the owner to read, keyed by the owner's language.

One setting chooses the language: AITELIER_OWNER_LANGUAGE (`en` or `zh`; default `en`). It is
read where every other deployment setting is: the process environment first (the backend
container gets the repository `.env` through compose `env_file`), else the line in the
repository's own `.env` (the DSH host's systemd unit does not carry `.env`, and a backend
that was restarted rather than recreated has not re-read it), else `en`. So the one line in
`.env` reaches the backend and, through core.dsh_driver, every steward and director process.
A value is taken by its primary subtag, case-insensitively (`zh-CN` is `zh`); anything else
is `en`.

What is here is what code (never a model) writes to the owner, or into a confirmation the
owner or a group member answers: the owner-question pushes, reminders and relay receipts
(core.state_owner_questions), the outward-action confirmation text (core.state_steward_confirmations),
and what code posts into a group or tells the owner about one (core.state_groups: the bind,
quota and media notices, the withheld sentence, the confirmation line, the owner's withheld line).
Record data stays as stored (a group's default name, the 业主 / 成员X labels, message subjects
the steward's host looks messages up by). The channel layer's own lines (receipt,
truncation tail, channel-down alert, bind and approval lines) are in the plugin's table,
integrations/dsh-director/lib/owner-text.mjs, chosen by the same setting.

The `zh` entries are the literals this code wrote before the table existed, byte for byte
(tests/unit/test_owner_text.py holds the copies). The reply phrases are not translated per
language: the backend accepts both (core.state_channels APPROVE..., core.state_steward_confirmations
CONFIRM...); the table only chooses which one the text shows.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from core.dotenv_boot import DOTENV_PATH, parse_dotenv

SETTING = "AITELIER_OWNER_LANGUAGE"
LANGUAGES = ("en", "zh")
DEFAULT = "en"


def normalise(value) -> str:
    """`en` or `zh` from a setting's value: its primary subtag, case-insensitively; else DEFAULT."""
    primary = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return primary if primary in LANGUAGES else DEFAULT


def owner_language(environ: Mapping[str, str] | None = None, dotenv: str | os.PathLike | None = None) -> str:
    """The owner's language: the environment's AITELIER_OWNER_LANGUAGE, else the repository
    `.env`'s, else DEFAULT. Read on every call (the file is small and read only when a text is
    written), so a changed `.env` line needs no restart of this process. tests/conftest.py sets
    the variable, so no test run falls back to a checkout's `.env`."""
    env = os.environ if environ is None else environ
    value = env.get(SETTING)
    if value is None:
        try:
            value = parse_dotenv(Path(DOTENV_PATH if dotenv is None else dotenv).read_text()).get(SETTING)
        except (OSError, UnicodeDecodeError):
            value = None
    return normalise(value)


# A channel copy of a steward message is cut past this many characters (the plugin's
# MIRROR_CHARS, lib/owner-text.mjs): a push is split between two questions to stay under it.
PUSH_CHARS = MappingProxyType({"en": 1200, "zh": 600})

TEXTS = MappingProxyType({
    "zh": MappingProxyType({
        # core.state_owner_questions: the push, reminders and closed notices
        "push_continued": "【待你回答】（续）",
        "push_new": "{n} 个新问题",
        "push_older": "{n} 个较早的问题",
        "push_head": ("【待你回答】{count}。回复时以编号开头，例如「Q{first} 用方案 A」，"
                      "你的原话会转给提问的导演；改主意就再发一条，以后来的为准。也可以在面板回答。\n"),
        "approval_phrase_push": "授权类问题（合并、重启、花钱）也这样答：「Q{code} 同意」。批准提议的那句口令是另一回事，不关问题，提醒照发。",
        "approval_phrase_note": "授权类问题也只认「Q编号 你的话」；批准提议的那句口令不关问题。",
        "reminder_head": "【提醒】还有 {n} 个问题等你回答：",
        "question_line": "Q{code} · {title}：｜{line}",
        "reminder_tail": "回复「Q{code} 你的回答」即可，也可以在面板回答。",
        "older_head": "较早的问题：",
        "more_on_dashboard": "另有 {n} 个，见面板。",
        "closed_dashboard": "已在面板回答",
        "closed_withdrawn": "已撤回",
        "closed_wechat": "已在微信回答",
        "closed_slack": "已在 Slack 回答",
        "closed_group": "{label}：{items}。",
        "closed_item": "Q{code}（{title}）",
        "closed_sep": "、",
        "closed_join": "",
        "block": "Q{code} · {title}\n问：\n{text}",
        "block_context": "\n背景：\n{context}",
        # core.state_owner_questions: the relay receipt
        "no_code_none": "没有认出编号，现在也没有等你回答的已推问题。\n",
        "no_code_head": "没有认出编号。你是在回答下面哪个问题？",
        "no_code_tail": "回「Q{code}」即把上一条原样转过去。\n",
        "unknown_code": "{label} 找不到这个编号",
        "withdrawn": "{label} 已撤回（{reason}），面板上仍可以答",
        "withdrawn_newer": "{label} 已撤回（{reason}）；导演改问了 Q{code}：{line}",
        "duplicate": "{label} 这条已经转过了",
        "superseded": "{label} 没有转：你之后又答过（{when}），以后来的为准",
        "no_recipients": "{label} 没有转：这个项目现在没有导演",
        "relayed": "已转达 {label} → {title}：{line}",
        "relayed_further": "已转达 {label}（补充回答）→ {title}：{line}",
        # core.state_steward_confirmations: the confirmation a requester answers
        "confirm_header": "请确认（{code}，10 分钟内有效）：用「{label}」执行 {tool}。回复「{at}{confirm}{code}」执行，「{at}{cancel}{code}」作废。",
        "confirm_phrase": "确认 ",
        "cancel_phrase": "取消 ",
        "confirm_short": "{key}：{text}",
        "confirm_long": "{key}（长文）：\n{text}",
        "confirm_footer": "—— 以上是全部内容 ——",
        # core.state_groups: the group's line once a reply decided a confirmation
        "group_confirmed": "已确认 {code}，正在执行。",
        "group_cancelled": "已取消 {code}，不会执行。",
        "group_expired": "{code} 已过期，未执行；请重新请求。",
        "group_refused": "{code} 未执行：消息时间不对，请重新回复。",
        # core.state_groups: the lines code posts into a group. group_withheld is also the
        # sentence the steward's prompt has the model use (steward.md; the plugin's English
        # prompt, lib/owner-text.mjs EN_STEWARD_PROMPT, puts the 'en' one in its place).
        "group_withheld": "这个我在群里答不了。",
        "group_quota": "问得太多了，过一会儿再问。",
        "group_bind": ("管家已加入本群。@ 我提问即可；我在群里只回答公开项目和一般问题，不能替任何人做决定，也不传话；私聊我不会回复。"
                       "@ 我的消息会保存 30 天，业主看得到；它们会交给外部模型服务处理，管家自己的会话记录里可能留得更久。"
                       "让我记住的事只在本群用，30 天后删。"),
        "group_media_on": ("本群已打开存图：群里没 @ 管家的图片、语音、文件，管家会先存下、不回话，存 1 天后自动删除。"
                           "只有之后在本群 @ 管家时，管家才会去看（看时会交给外部模型服务处理）；业主私聊和别的群都用不到。"
                           "业主在面板上看得到谁发过、是什么类型，看不到内容。关闭存图时，已存的一并删除。"),
        "group_media_off": "本群已关闭存图：管家不再存群里没 @ 它的图片、语音、文件，之前存下的已经删除。",
        # core.state_groups: the owner's one line when a group reply is withheld, and its kinds
        # (withheld_<core.state_groups.WITHHELD_REASONS key>)
        "withheld_owner_line": "{group}里有一条回答没发出去：{reason}。要问可以私聊我。",
        "withheld_live_code": "含有绑定码",
        "withheld_private_project": "涉及私有项目",
        "withheld_host_path": "含有主机上的路径",
        # core.state_owner_questions: in front of every line of a director's text, so no line
        # of it starts with a question code (parse_codes reads an NFKC copy: either bar is '|')
        "bar": "｜",
    }),
    "en": MappingProxyType({
        "push_continued": "[Waiting for your answer] (continued)",
        "push_new": "new questions: {n}",
        "push_older": "older questions: {n}",
        "push_head": ("[Waiting for your answer] {count}. Start your reply with the code, e.g. \"Q{first} go with plan A\"; "
                      "your words go to the director who asked, unchanged. Changed your mind? Send another one - the later "
                      "one counts. You can also answer on the dashboard.\n"),
        "approval_phrase_push": ("Authorization questions (merge, restart, spending) are answered the same way: \"Q{code} yes\". "
                                 "The proposal approval phrase is a different thing: it closes no question, and reminders still come."),
        "approval_phrase_note": ("Authorization questions also take only \"Q<code> your words\"; the proposal approval phrase "
                                 "closes no question."),
        "reminder_head": "[Reminder] questions still waiting for your answer: {n}",
        "question_line": "Q{code} · {title}: {line}",
        "reminder_tail": "Reply \"Q{code} your answer\", or answer on the dashboard.",
        "older_head": "Older questions:",
        "more_on_dashboard": "{n} more on the dashboard.",
        "closed_dashboard": "Answered on the dashboard",
        "closed_withdrawn": "Withdrawn",
        "closed_wechat": "Answered on WeChat",
        "closed_slack": "Answered on Slack",
        "closed_group": "{label}: {items}.",
        "closed_item": "Q{code} ({title})",
        "closed_sep": ", ",
        "closed_join": " ",
        "block": "Q{code} · {title}\nQuestion:\n{text}",
        "block_context": "\nBackground:\n{context}",
        "no_code_none": "No question code found, and no pushed question is waiting for your answer.\n",
        "no_code_head": "No question code found. Which of these are you answering?",
        "no_code_tail": "Reply \"Q{code}\" to pass your previous message on unchanged.\n",
        "unknown_code": "{label}: no such code",
        "withdrawn": "{label} was withdrawn ({reason}); you can still answer it on the dashboard",
        "withdrawn_newer": "{label} was withdrawn ({reason}); the director now asks Q{code}: {line}",
        "duplicate": "{label}: already passed on",
        "superseded": "{label} not passed on: you answered it again later ({when}); the later answer counts",
        "no_recipients": "{label} not passed on: this project has no director now",
        "relayed": "Passed on {label} → {title}: {line}",
        "relayed_further": "Passed on {label} (a further answer) → {title}: {line}",
        "confirm_header": ("Please confirm ({code}, valid for 10 minutes): run {tool} with \"{label}\". "
                           "Reply \"{at}{confirm}{code}\" to run it, \"{at}{cancel}{code}\" to drop it."),
        "confirm_phrase": "confirm ",
        "cancel_phrase": "cancel ",
        "confirm_short": "{key}: {text}",
        "confirm_long": "{key} (long):\n{text}",
        "confirm_footer": "-- that is everything --",
        "group_confirmed": "Confirmed {code}; running it.",
        "group_cancelled": "Cancelled {code}; it will not run.",
        "group_expired": "{code} expired; nothing was done - please ask again.",
        "group_refused": "{code} not done: the message's time is wrong - please reply again.",
        "group_withheld": "I cannot answer that in this group.",
        "group_quota": "Too many questions; please ask again in a while.",
        "group_bind": ("The steward has joined this group. @ me to ask; here I answer only about public projects and "
                       "general questions, I decide nothing for anyone and pass no messages on; I do not answer private "
                       "messages. Messages that @ me are kept for 30 days and the owner can see them; they are processed "
                       "by an external model service, and the steward's own conversation record may keep them longer. "
                       "What you ask me to remember is used only in this group and deleted after 30 days."),
        "group_media_on": ("Media keeping is on in this group: images, voice messages and files posted here without "
                           "@ the steward are stored silently and deleted automatically after 1 day. The steward looks at "
                           "them only when someone later @ mentions it in this group (it then hands them to an external "
                           "model service); the owner's private chat and other groups never use them. The owner sees on "
                           "the dashboard who posted what kind of file, never its content. Turning media keeping off "
                           "deletes what was stored."),
        "group_media_off": ("Media keeping is off in this group: the steward no longer stores images, voice messages "
                            "and files posted here without @ it, and what was stored has been deleted."),
        "withheld_owner_line": "A reply in {group} was not sent: {reason}. Ask me privately if you need it.",
        "withheld_live_code": "it contained a binding code",
        "withheld_private_project": "it touched a private project",
        "withheld_host_path": "it contained a path on the host",
        "bar": "| ",
    }),
})


def texts(language: str | None = None) -> Mapping[str, str]:
    """The table for `language` (None: the owner's language now)."""
    return TEXTS[owner_language() if language is None else normalise(language)]
