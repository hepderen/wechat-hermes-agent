from __future__ import annotations

import re


PERSONA_VERSION = "concise-old-friend-v3"
SHORT_REPLY_MAX_CHARS = 180
EXPANDED_REPLY_MAX_CHARS = 800

_EXPANDED_REQUEST_RE = re.compile(
    r"(?:详细|展开|细说|多说|深入|完整|全面|逐步|一步一步|教程|报告|长文|"
    r"列出|清单|步骤|对比|评估|复盘|"
    r"(?:给|出|写|做|整理).{0,6}(?:方案|代码|命令|正则|SQL)|"
    r"(?:[一二三四五六七八九十两\d]+)\s*(?:句|条|点|项|字)|"
    r"\b(?:in detail|step by step|full report|comprehensive)\b)",
    re.IGNORECASE,
)
_CONTEXT_MARKERS = (
    "\n被引用消息元数据",
    "\n附件元数据",
    "\n近期群聊上下文",
)
_LEADING_FILLER_RE = re.compile(
    r"^\s*(?:(?:好的?|没问题|当然(?:可以)?|明白(?:了)?|收到)"
    r"[，,。.!！：:\s]+|根据(?:你|您)的(?:需求|描述|情况)"
    r"[，,：:\s]*)+",
)
_TRAILING_OFFER_RE = re.compile(
    r"(?:\n+|(?<=[。！？!?]))\s*"
    r"(?:(?:如果你|你要是)(?:还)?(?:需要|愿意|想)|需要的话|"
    r"有需要(?:的话)?)"
    r".{0,100}(?:我可以|我再|告诉我|继续|帮你|再说)[。！？!?\s]*$",
    re.DOTALL,
)
_TRAILING_CLICHE_RE = re.compile(
    r"(?:\n+|(?<=[。！？!?]))\s*希望(?:以上|这些|这能)?.{0,40}"
    r"(?:有帮助|帮到你)[。！？!?\s]*$",
    re.DOTALL,
)
_MARKDOWN_HEADING_RE = re.compile(
    r"\s*(?:#{1,6}\s+.+|\*\*[^*]{1,24}\*\*[：:]?)\s*"
)


PERSONA_SYSTEM_PROMPT = f"""人格版本：{PERSONA_VERSION}

你是 Hermes，是微信群里脑子快、靠谱、肯干活的熟人。说话像接着聊天，
别演客服，别刻意装人，也别把每句话写成报告。

1. 直接回答。一句话说清就停；普通聊天通常一到两句。跟着用户的长度和语气，
   只有对方明确要详细分析、步骤、清单或报告时才展开。
2. 不复述问题，不写开场、标题、小结和“如果需要我还能……”式结尾。
   省掉“好的、没问题、根据你的需求、以下是、总体来说、值得注意的是”。
3. 有判断就直说，只留最关键的理由。默认自然一点，别每句都锐评或玩梗；
   对方明确说“锐评、吐槽、开喷、阴阳一下、贴吧老哥模式”时再加火力。
   对方说“正常点、认真点、退出老哥模式”时恢复。
4. 用日常中文和具体动词，少用抽象名词、套话和“首先其次最后”。
   不编造经历或情绪；问到身份时如实说自己是 Hermes。
5. 能执行就执行。工具结果、任务状态和限制照实；缺少证据就直说尚未完成。
   停止、取消、不要图片、只要文字等控制命令只回一句，不玩梗、不争辩。
"""

PERSONA_TURN_PROMPT = """把草稿改成微信群里的自然短回复：
- 首句直接给答案、判断或真实状态，删掉开场、复述、标题、小结和主动延伸。
- 普通问题最多两句；只保留结论和一个必要理由。回答完就停。
- 用户明确要详细内容时才列项。事实、证据、任务状态和限制必须照实。"""

PERSONA_TASK_PROMPT = """最终结果先说做成了什么，省掉接单过程、复述和客套话。
只保留结果、已验证产物和真正影响交付的限制；失败时说清原因和一个下一步。
任务状态和工具证据必须照实，回答完就停。"""


def visible_user_request(message: str) -> str:
    value = str(message or "")
    for marker in _CONTEXT_MARKERS:
        value = value.split(marker, 1)[0]
    return value.strip()


def expanded_reply_requested(message: str) -> bool:
    return bool(_EXPANDED_REQUEST_RE.search(visible_user_request(message)))


def chat_turn_prompt(message: str) -> str:
    if expanded_reply_requested(message):
        length_rule = (
            "本轮用户明确要求展开：最多 %d 字，只写对方点名要的内容。"
            % EXPANDED_REPLY_MAX_CHARS
        )
    else:
        length_rule = (
            "本轮按普通群聊回答：最多两句、%d 字；一句够用就只写一句。"
            % SHORT_REPLY_MAX_CHARS
        )
    return PERSONA_TURN_PROMPT + "\n" + length_rule


def _remove_machine_wrapping(text: str, *, expanded: bool) -> str:
    value = _LEADING_FILLER_RE.sub("", text.strip())
    value = _TRAILING_OFFER_RE.sub("", value).strip()
    value = _TRAILING_CLICHE_RE.sub("", value).strip()
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    if not expanded:
        lines = [
            line
            for line in value.splitlines()
            if not _MARKDOWN_HEADING_RE.fullmatch(line)
        ]
        value = re.sub(r"\s*\n+\s*", " ", "\n".join(lines)).strip()
    return value


def _truncate_fragment(value: str, limit: int) -> str:
    fragment = value[:limit].rstrip()
    floor = max(1, int(limit * 0.55))
    sentence_end = max(fragment.rfind(mark) for mark in "。！？!?")
    if sentence_end >= floor:
        return fragment[: sentence_end + 1].strip()
    soft_end = max(fragment.rfind(mark) for mark in "，,；;\n")
    if soft_end >= floor:
        return fragment[:soft_end].rstrip("，,；; \n") + "。"
    return fragment.rstrip("，,；;：: ") + "…"


def compact_chat_reply(reply: str, message: str) -> str:
    original = str(reply or "").strip()
    expanded = expanded_reply_requested(message)
    value = _remove_machine_wrapping(original, expanded=expanded)
    if not value:
        value = original

    limit = EXPANDED_REPLY_MAX_CHARS if expanded else SHORT_REPLY_MAX_CHARS
    sentence_limit = None if expanded else 2
    segments = [
        segment.strip()
        for segment in re.split(r"(?<=[。！？!?])\s*|\n+", value)
        if segment.strip()
    ]
    if not segments:
        return _truncate_fragment(value, limit) if len(value) > limit else value

    selected: list[str] = []
    for segment in segments:
        if sentence_limit is not None and len(selected) >= sentence_limit:
            break
        candidate = "".join(selected) + segment
        if len(candidate) > limit:
            if not selected:
                selected.append(_truncate_fragment(segment, limit))
            break
        selected.append(segment)

    compact = "".join(selected).strip()
    if not compact:
        compact = _truncate_fragment(value, limit)
    return compact
