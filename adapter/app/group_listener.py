from __future__ import annotations

from difflib import SequenceMatcher
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
_GREETING_ONLY_RE = re.compile(
    r"^(?:你好|您好|嗨|哈喽|hello|hi|hey|早上好|早安|晚安|晚上好|"
    r"在吗|有人吗|来了|回来了)(?:呀|啊|呢|哦|喽)?[!！。.,，~～?？]*$",
    re.IGNORECASE,
)
# Very short acknowledgements carry no new information.  Keeping this list
# explicit avoids suppressing legitimate concise answers such as "行，周五".
LOW_INFORMATION_REPLY_KEYS = frozenset(
    {
        "嗯来了",
        "嗯我来了",
        "我来了",
        "嗯来啦",
        "我来啦",
        "来了",
        "嗯在",
        "我在",
        "在呢",
        "在的",
        "到啦",
        "我到啦",
    }
)
# These are formatting/control characters that have appeared in old delivery
# correlation markers. They are not useful in a WeChat chat message and must
# not affect classification, repetition checks, or model-visible context.
# Preserve U+200D (zero-width joiner): it is part of normal composite emoji.
# The other entries are legacy delivery markers or directional controls.
_INTERNAL_FORMAT_RE = re.compile(
    r"[\u00ad\u061c\u200b\u200c\u200e\u200f\u202a-\u202e"
    r"\u2060-\u206f\ufeff]"
)
_LOW_INFORMATION_REPLY_RE = re.compile(
    r"^(?:嗯+)?(?:我)?(?:来啦?|在呢?|在的|到啦?|到了?)(?:呀|啊|呢|哦|喽)?$",
    re.IGNORECASE,
)
# A bare arrival acknowledgement adds no value before an actual reply.  Match
# it only when a delimiter proves that a substantive remainder follows, so
# normal wording such as "我在想这个问题" remains intact.
_LEADING_PRESENCE_CONFIRMATION_RE = re.compile(
    r"^\s*(?:嗯+[，,\s]*)?"
    r"(?:(?:我)?来(?:了|啦)|(?:我)?在(?:呢|的)?|(?:我)?到(?:了|啦))"
    r"(?:呀|啊|呢|哦|喽)?"
    r"(?=[，,。.!！?？:：~～\s])"
    r"[，,。.!！?？:：~～\s]+",
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
    return re.sub(
        r"\s+",
        "",
        strip_internal_format_chars(str(message or "").strip()),
    )


def strip_internal_format_chars(value: object) -> str:
    """Remove invisible delivery/control characters from user-visible text."""
    return _INTERNAL_FORMAT_RE.sub("", str(value or "").replace("\x00", ""))


def strip_leading_presence_confirmation(reply: object) -> str:
    """Remove a canned arrival prefix when it precedes a real reply.

    Whole acknowledgements stay unchanged so ``is_low_information_reply`` can
    suppress them.  This avoids turning a valid no-content response into an
    empty message while preventing old model/session habits from reaching the
    user or being retained as future conversation context.
    """
    value = strip_internal_format_chars(reply).strip()
    match = _LEADING_PRESENCE_CONFIRMATION_RE.match(value)
    if match is None:
        return value
    remainder = value[match.end() :].strip()
    return remainder if remainder else value


def _is_low_signal(text: str) -> bool:
    if not text:
        return True
    if not _MEANINGFUL_CHARACTER_RE.search(text):
        return True
    return bool(_LOW_SIGNAL_RE.fullmatch(text) or _GREETING_ONLY_RE.fullmatch(text))


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
先看短期群聊时间线中自己最近说过的话；不要连续解释自己的人格、语气或是否“正常”。
不要只回“嗯，来了”“来了”“我在”这类没有内容的存在确认；要么接具体话题，要么保持安静。
没有新内容时保持安静，不要换个说法重复上一句。
不适合插话时，严格只输出 %s，不能带标点、解释或其他文字。适合回复时直接像群成员一样说话。""" % (
        trigger,
        NO_REPLY_MARKER,
    )


def listener_reply_or_silence(reply: str) -> str:
    """Do not let the internal silence marker escape into a WeChat message."""
    value = strip_leading_presence_confirmation(reply)
    if value == NO_REPLY_MARKER:
        return ""
    value = value.replace(NO_REPLY_MARKER, "").strip()
    return "" if is_low_information_reply(value) else value


def _reply_repetition_key(value: object) -> str:
    return re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "",
        strip_internal_format_chars(value).casefold(),
    )


def is_low_information_reply(reply: str) -> bool:
    """Recognize canned presence pings that should not fill a group chat."""
    candidate = _reply_repetition_key(
        strip_internal_format_chars(reply).strip()
    )
    return bool(
        candidate in LOW_INFORMATION_REPLY_KEYS
        or _LOW_INFORMATION_REPLY_RE.fullmatch(candidate)
    )


def repeats_recent_listener_reply(
    reply: str,
    timeline: Iterable[Mapping[str, object]],
    *,
    limit: int = 4,
) -> bool:
    """Catch near-identical passive replies if the model ignores its silence cue."""
    candidate = _reply_repetition_key(reply)
    if not candidate:
        return False
    short_candidate = candidate in LOW_INFORMATION_REPLY_KEYS
    if len(candidate) < 12 and not short_candidate:
        return False
    checked = 0
    for item in reversed(list(timeline)):
        if str(item.get("direction") or "").strip().lower() != "outgoing":
            continue
        previous = _reply_repetition_key(item.get("text"))
        if not previous or (len(previous) < 12 and previous not in LOW_INFORMATION_REPLY_KEYS):
            continue
        checked += 1
        if candidate == previous:
            return True
        shorter = min(len(candidate), len(previous))
        if (
            shorter >= 16
            and SequenceMatcher(None, candidate, previous).ratio() >= 0.90
        ):
            return True
        if checked >= max(1, int(limit)):
            break
    return False
