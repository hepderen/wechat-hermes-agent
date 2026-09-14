"""Choose a timely group conversation to join, using trusted room records."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from .group_listener import classify_group_message, strip_internal_format_chars

_INVITATION = re.compile(r"有人|大家|你们|有谁|来不来|打不打|一起|有没有|求推荐")
_EXPERIENCE = re.compile(r"今天|刚刚|刚才|终于|居然|竟然|气死|烦死|好累|难受|失眠|开心|笑死我|离谱|翻车|抽象")
_CLOSING = re.compile(r"^(?:好[的吧]?|行|算了|嗯|哦|知道了|收到|哈哈+|晚安|拜拜|先这样|不聊了)[。!！~～\s]*$")


@dataclass(frozen=True)
class Participation:
    should_call: bool
    reason: str
    kind: str = "conversation"
    target_name: str = ""
    target_text: str = ""


def _name(raw: object) -> str:
    return re.sub(r"\s+", " ", strip_internal_format_chars(raw)).replace("：", ":")[:48].strip()


def choose_participation(
    message: str,
    message_type: str,
    names: Sequence[str],
    state: Mapping[str, object] | None,
    timeline: Sequence[Mapping[str, object]],
    *,
    sender_id: str,
    sender_name: str,
    timestamp: float | None = None,
    now: float | None = None,
    gap_seconds: float = 6,
    turns: int = 2,
) -> Participation:
    current = time.time() if now is None else float(now)
    kind, reason = classify_group_message(message, message_type, names)
    values = state or {}
    if kind == "unsupported":
        return Participation(False, reason, kind)
    if kind == "addressed":
        return Participation(True, reason, kind, _name(sender_name), message)
    # Do not resurrect a discussion from a catch-up batch after downtime.
    if timestamp and current - float(timestamp) > 120:
        return Participation(False, "stale_message", kind)
    if _CLOSING.fullmatch(message.strip()):
        if "哈" not in message:
            return Participation(False, "conversation_closed", kind)
        kind = "low_signal"

    recent = [
        item for item in timeline[-16:]
        if 0 <= current - float(item.get("message_timestamp") or item.get("timestamp") or 0) <= 120
    ]
    last_reply = float(values.get("last_reply_at") or 0)
    elapsed = max(0.0, current - last_reply) if last_reply else float("inf")
    anchor = next((item for item in reversed(recent)
        if item.get("direction") == "incoming"
        and item.get("local_id") == values.get("last_reply_local_id")), None)
    conversation_partner = bool(
        anchor and sender_id and anchor.get("sender_id") == sender_id
        and elapsed <= 120
    )
    target_name, target_text = _name(sender_name), message

    # An explicit address to another known participant belongs to their exchange.
    for item in recent:
        name = _name(item.get("sender_name"))
        if item.get("sender_id") != sender_id and name and name not in names:
            if message.strip().startswith(("@" + name, name + "，", name + ",")):
                return Participation(False, "other_member_addressed", kind)

    if kind == "low_signal":
        # A brief laugh after an unanswered question can be a useful moment to
        # speak to that question's author, not to the person who laughed.
        pending = []
        for item in reversed(recent[-4:]):
            if item.get("direction") == "outgoing":
                break
            if item.get("direction") == "incoming" and item.get("sender_id"):
                item_kind, _ = classify_group_message(str(item.get("text") or ""), "text", names)
                if item_kind == "question":
                    pending.append(item)
        if not pending:
            return Participation(False, "low_signal", kind)
        target = pending[0]
        target_name, target_text = _name(target.get("sender_name")), str(target.get("text") or "")
        kind = "open_question"
    elif conversation_partner and not _CLOSING.fullmatch(message.strip()):
        kind = "continuation"
    elif _INVITATION.search(message):
        kind = "invitation"
    elif _EXPERIENCE.search(message):
        kind = "experience"

    needed_turns = 1 if kind != "conversation" else max(1, turns)
    needed_gap = min(2.0, gap_seconds) if kind == "continuation" else gap_seconds
    if elapsed < needed_gap:
        return Participation(False, "time_gap", kind)
    if int(values.get("turns_since_reply") or 0) < needed_turns:
        return Participation(False, "turn_gap", kind)
    return Participation(True, kind, kind, target_name, target_text)


ACTIVE_CHAT_PROTOCOL = (
    "\n你在参与群聊，不必等别人@你。结合整个对话找值得接的话题，优先接正在和你聊的人，"
    "也可以自然接住另一位群友刚才没聊完的问题。最近发言者未必是每句话的回答对象。"
    "若转录提供‘可承接的近期发言’，直接回应那句话的作者，不催另一个人代答。"
    "需要区分对象时沿用转录中的昵称自然叫对方，别编昵称、别发@全体。"
    "有具体内容就说，也可顺着对方的经历问一句具体问题；别每条都以反问结尾、别查户口。"
    "篇幅按话题需要决定，不套用风格资料中的固定字数上限；短接话可以很短，"
    "聊开了可用一到三个短段落。无需报到、总结或解释接话策略。"
    "只依据群里实际说过的事；不冒充群友、不捏造线下共同经历。"
    "具体数字、地点不知道就明说，不为接梗编造答案。别附加无关广告、引流词或模板尾巴。"
    "如果这轮话题值得继续，可在主消息后另起一行写 [[FOLLOW_UP]] 再接一句自然的新想法；"
    "收尾、低信号或不确定时不要写这个标记。这个标记只给系统调度，不会原样发出。"
)


def focus_context(choice: Participation) -> str:
    """Render selected member data in the transcript, never as system authority."""
    if not choice.target_name:
        return ""
    text = re.sub(r"[\r\n]+", " ", strip_internal_format_chars(choice.target_text))[:400]
    return "\n可承接的近期发言（转录资料）：%s：%s" % (choice.target_name, text)
