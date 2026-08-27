from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Mapping


NO_REPLY_MARKER = "[[NO_REPLY]]"
SUPPORTED_LISTENER_MESSAGE_TYPES = frozenset({"", "text", "quoted_reply"})

_MEANINGFUL_CHARACTER_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_LOW_SIGNAL_RE = re.compile(
    r"^(?:"
    r"嗯+|嗯嗯+|哦+|噢+|啊+|哈+|哈哈+|呵+|嘿+|"
    r"6+|666+|ok+|okay+|好的?|行+|收到|知道了?|"
    r"y+|xswl|笑死(?:了)?|顶+|[捂笑哭赞强拳握手]+"
    r")$",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(?:[?？]|(?:怎么|为什么|为何|如何|谁|多少|几|哪儿|哪里|"
    r"能不能|可不可以|行不行|是否|是不是|咋办|咋样|怎样).{0,32}$|"
    r"(?:吗|嘛|呢)\s*$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GroupListenerDecision:
    should_call: bool
    reason: str
    kind: str


def normalize_listener_names(values: Iterable[str]) -> tuple[str, ...]:
    """Keep a small, deterministic set of plain-text names for direct address."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "").strip())
        if not value or len(value) > 48:
            continue
        marker = value.casefold()
        if marker in seen:
            continue
        names.append(value)
        seen.add(marker)
    return tuple(names)


def _normalized_text(message: str) -> str:
    return re.sub(r"\s+", "", str(message or "").strip())


def _is_low_signal(text: str) -> bool:
    if not text:
        return True
    if not _MEANINGFUL_CHARACTER_RE.search(text):
        return True
    return bool(_LOW_SIGNAL_RE.fullmatch(text))


def classify_group_message(
    message: str,
    message_type: str,
    listener_names: Iterable[str],
) -> tuple[str, str]:
    """Classify only enough to throttle passive group participation."""
    normalized_type = str(message_type or "").strip().lower()
    if normalized_type not in SUPPORTED_LISTENER_MESSAGE_TYPES:
        return "unsupported", "unsupported_message_type"

    text = _normalized_text(message)
    names = normalize_listener_names(listener_names)
    folded = text.casefold()
    if any(name.casefold() in folded for name in names):
        return "addressed", "plain_name"
    if _is_low_signal(text):
        return "low_signal", "low_signal"
    if _QUESTION_RE.search(text):
        return "question", "question"
    return "conversation", "conversation"


def decide_group_listener(
    message: str,
    message_type: str,
    listener_names: Iterable[str],
    state: Mapping[str, object] | None,
    *,
    min_reply_gap_seconds: float,
    min_turns_between_replies: int,
    now: float | None = None,
) -> GroupListenerDecision:
    """Apply deterministic local filters before a passive model turn.

    A plain-text name is treated as an intentional address. Other messages need
    both a time gap and intervening human turns after the bot's last reply.
    The model still decides whether a permitted turn deserves an actual reply.
    """
    kind, reason = classify_group_message(message, message_type, listener_names)
    if kind in {"unsupported", "low_signal"}:
        return GroupListenerDecision(False, reason, kind)
    if kind == "addressed":
        return GroupListenerDecision(True, "plain_name", kind)

    current = time.time() if now is None else float(now)
    values = state or {}
    turns_since_reply = max(0, int(values.get("turns_since_reply") or 0))
    base_turns = max(1, int(min_turns_between_replies))
    required_turns = max(1, base_turns - 1) if kind == "question" else base_turns
    required_gap = max(0.0, float(min_reply_gap_seconds))
    if kind == "question":
        required_gap /= 2

    # Normal conversation is sampled at predictable turn intervals while the
    # bot is silent. Questions can surface one turn earlier, but still honor
    # the room's reply gap after an actual bot message.
    if turns_since_reply < required_turns or (
        kind == "conversation" and turns_since_reply % required_turns
    ):
        return GroupListenerDecision(False, "turn_gap", kind)

    last_reply_at = values.get("last_reply_at")
    if last_reply_at is not None:
        elapsed = max(0.0, current - float(last_reply_at))
        if elapsed < required_gap:
            return GroupListenerDecision(False, "time_gap", kind)
    return GroupListenerDecision(True, reason, kind)


def passive_listener_turn_prompt(kind: str) -> str:
    if kind == "addressed":
        trigger = "有人直接叫了你的小名，正常接话并回答。"
    elif kind == "question":
        trigger = "群里出现了一个问题，但不一定是在问你。"
    else:
        trigger = "这是一段普通群聊，也不一定需要你插话。"
    return """当前是旁听式群聊，不是每条消息都在叫你。
%s
先判断你的回复能否补充具体观点、接住上下文，或让这段对话更自然；只有确实值得说时才回复。
不要为了刷存在感复述、附和、点评每个人，也不要说自己在监听或要求别人 @ 你。
不适合插话时，严格只输出 %s，不能带标点、解释或其他文字。适合回复时直接像群成员一样说话。""" % (
        trigger,
        NO_REPLY_MARKER,
    )


def listener_reply_or_silence(reply: str) -> str:
    """Do not let the internal silence marker escape into a WeChat message."""
    value = str(reply or "").strip()
    if value == NO_REPLY_MARKER:
        return ""
    return value.replace(NO_REPLY_MARKER, "").strip()
