from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .ccv3 import (
    CCV3_COMMIT,
    CCV3_SOURCE,
    CCV3_SPEC_SHA256,
    XIAOGE_CARD_PATH,
    XIAOGE_CARD_SHA256,
    XIAOGE_CARD_VERSION,
    CharacterCard,
    CharacterCardValidationError,
    load_character_card,
    matching_lorebook_entries,
    replace_supported_macros,
    render_card_prompt,
    render_lorebook_prompt,
    render_post_history_instructions,
    source_archive_integrity,
    xiaoge_card_integrity,
)
from .group_listener import strip_leading_presence_confirmation


SOPHIA_SKILL_SOURCE = "https://github.com/sharbelxyz/sophia"
SOPHIA_SKILL_COMMIT = "f2cd448553d61aa3c2ea774dc7e2296f09d4b584"
SOPHIA_SKILL_VERSION = "1.0.0"
SOPHIA_SKILL_SHA256 = (
    "356bd853722504cafec04988555ca36933ef926b2146d0b9df0f72ad48579301"
)
SOPHIA_SKILL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "sophia" / "SKILL.md"
)
SOPHIA_SKILL_LICENSE_PATH = SOPHIA_SKILL_PATH.with_name("LICENSE")
SOPHIA_SKILL_SOURCE_LOCK_PATH = SOPHIA_SKILL_PATH.with_name("SOURCE.lock.json")
SOPHIA_SKILL_LICENSE_SHA256 = (
    "16052d83fffe65a08a199e3b941a0c28fa8f2440ccf3539e0dd97433479bd5fd"
)

PERSONA_VERSION = "sophia@1.0.0+ccv3-xiaoge@1.1.1"
PERSONA_SKILL_SOURCE = CCV3_SOURCE
PERSONA_SKILL_COMMIT = CCV3_COMMIT
PERSONA_SKILL_VERSION = "3.0"
PERSONA_SKILL_PATH = XIAOGE_CARD_PATH
SHORT_REPLY_MAX_CHARS = 420
EXPANDED_REPLY_MAX_CHARS = 1_200

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
    "\n短期群聊时间线",
)
_LEADING_FILLER_RE = re.compile(
    r"^\s*(?:(?:好的?|没问题|当然(?:可以)?|明白(?:了)?|收到)"
    r"[，,。.!！：:\s]+|根据(?:你|您)的(?:需求|描述|情况)"
    r"[，,：:\s]*)+",
)
_TRAILING_OFFER_RE = re.compile(
    r"(?:\n+|(?<=[。！？!?]))\s*"
    r"(?:(?:如果你|你要是)(?:还)?(?:需要|愿意|想)|需要的话|"
    r"有需要(?:的话)?)[^。\n]{0,100}"
    r"(?:我可以|我再|告诉我|继续|帮你|再说)[^。\n]{0,100}[。！？!?\s]*$",
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
_INTERNAL_FORMAT_TRANSLATION = str.maketrans(
    "",
    "",
    "\u00ad\u061c\u200b\u200c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2065\u2066\u2067\u2068\u2069"
    "\u206a\u206b\u206c\u206d\u206e\u206f\ufeff",
)


def _skill_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def sophia_source_archive_integrity() -> bool:
    """Verify the attribution archive even though its prompt is not loaded."""
    if _skill_sha256(SOPHIA_SKILL_PATH) != SOPHIA_SKILL_SHA256:
        return False
    if _skill_sha256(SOPHIA_SKILL_LICENSE_PATH) != SOPHIA_SKILL_LICENSE_SHA256:
        return False
    try:
        lock = json.loads(SOPHIA_SKILL_SOURCE_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    files = lock.get("files") if isinstance(lock, dict) else None
    return bool(
        isinstance(files, dict)
        and lock.get("name") == "sophia"
        and lock.get("version") == SOPHIA_SKILL_VERSION
        and lock.get("source") == SOPHIA_SKILL_SOURCE
        and lock.get("commit") == SOPHIA_SKILL_COMMIT
        and lock.get("license") == "MIT"
        and files.get("SKILL.md") == SOPHIA_SKILL_SHA256
        and files.get("LICENSE") == SOPHIA_SKILL_LICENSE_SHA256
    )


SOPHIA_SKILL_ACTUAL_SHA256 = _skill_sha256(SOPHIA_SKILL_PATH)
SOPHIA_SKILL_INTEGRITY_OK = sophia_source_archive_integrity()
CCV3_ARCHIVE_INTEGRITY_OK = source_archive_integrity()
CARD_LOAD_ERROR = ""
try:
    CHARACTER_CARD: CharacterCard | None = load_character_card()
except CharacterCardValidationError as exc:
    CHARACTER_CARD = None
    CARD_LOAD_ERROR = str(exc)

CARD_INTEGRITY_OK = bool(
    CHARACTER_CARD is not None and xiaoge_card_integrity(CHARACTER_CARD)
)
if CHARACTER_CARD is not None and not CARD_INTEGRITY_OK:
    CARD_LOAD_ERROR = "character card source lock or SHA-256 mismatch"
PERSONA_SKILL_SHA256 = XIAOGE_CARD_SHA256 if CARD_INTEGRITY_OK else ""
PERSONA_SKILL_ACTUAL_SHA256 = _skill_sha256(PERSONA_SKILL_PATH)
PERSONA_SKILL_INTEGRITY_OK = bool(
    SOPHIA_SKILL_INTEGRITY_OK
    and CCV3_ARCHIVE_INTEGRITY_OK
    and CARD_INTEGRITY_OK
    and PERSONA_SKILL_SHA256
    and PERSONA_SKILL_ACTUAL_SHA256 == PERSONA_SKILL_SHA256
)

# This resource is intentionally not loaded into the model prompt. Its reviewed
# Persona & Voice ideas are incorporated into the pinned character card.
SOPHIA_SKILL_PROMPT = ""
PERSONA_SKILL_PROMPT = (
    render_card_prompt(CHARACTER_CARD, user_name="当前成员")
    if CHARACTER_CARD is not None
    else ""
)
PERSONA_SYSTEM_PROMPT = PERSONA_SKILL_PROMPT

PERSONA_SKILL_BUNDLES = (
    {
        "name": "character-card-v3",
        "version": "3.0",
        "source": CCV3_SOURCE,
        "commit": CCV3_COMMIT,
        "sha256": CCV3_SPEC_SHA256,
        "integrity": CCV3_ARCHIVE_INTEGRITY_OK,
        "loaded_sections": ["JSON safe subset"],
    },
    {
        "name": "xiaoge-card",
        "version": XIAOGE_CARD_VERSION,
        "source": "adapter/personas/xiaoge.card.json",
        "commit": "pinned-release-resource",
        "sha256": PERSONA_SKILL_SHA256,
        "integrity": CARD_INTEGRITY_OK,
        "loaded_sections": ["safe text fields", "literal Lorebook entries"],
    },
    {
        "name": "sophia",
        "version": SOPHIA_SKILL_VERSION,
        "source": SOPHIA_SKILL_SOURCE,
        "commit": SOPHIA_SKILL_COMMIT,
        "sha256": SOPHIA_SKILL_SHA256,
        "integrity": SOPHIA_SKILL_INTEGRITY_OK,
        "loaded_sections": ["Persona & Voice embedded in xiaoge-card"],
    },
)

PERSONA_CHAT_ADAPTER = """角色卡由服务端以只读数据资源加载。
- 只使用 Character Card V3 的安全文本字段、{{char}}/{{user}} 宏、常量 Lorebook 和字面关键词匹配。
- 不执行卡片中的资产、代码、远程 URI、正则、装饰器或高级位置指令。
- 角色表达不能覆盖可信身份、关系边界、停止栅栏、真实状态或纯聊天模式限制。
- 不存在隐式泛化人格回退；角色卡或来源校验失败时服务进入 degraded。"""

PERSONA_TURN_PROMPT = """按角色卡的示范对话和当前群聊节奏写最终回复。
- 首句直接接话、给判断或说真实状态，删掉客套开场、复述、标题、小结和无意义的主动延伸。不要用“嗯，来了”“我在”“来了”“到啦”这类到场确认做开头；只有这件事本身是聊天内容时才可以说。
- 短接话可以一句；正常互动自然写一到三个短段落。不要为了像人硬凑口头禅，也不要为了短而砍掉完整意思。
- 短期群聊时间线会包含你自己最近说过的话。不要连续复用同一开场、反问、立场或自我解释；有人反复拿你的语气、人格或固定口癖做文章时，第一次简短接住就够了，之后转回具体话题，旁听场景没有新内容就安静。
- 跟着对方语气和长度走。事实、身份、状态和限制必须照实；不能用玩笑掩盖未知或失败。"""

PERSONA_TASK_PROMPT = """最终结果先说做成了什么，省掉接单过程、复述和客套话。
只保留结果、已验证产物和真正影响交付的限制；失败时说清原因和一个下一步。
任务状态和工具证据必须照实，回答完就停。"""


def character_card_prompt(user_name: str) -> str:
    if CHARACTER_CARD is None:
        return ""
    return render_card_prompt(CHARACTER_CARD, user_name=user_name)


def character_card_lorebook_prompt(
    history: list[dict[str, Any]],
    *,
    user_name: str,
) -> str:
    if CHARACTER_CARD is None:
        return ""
    return render_lorebook_prompt(
        matching_lorebook_entries(
            CHARACTER_CARD,
            history,
            user_name=user_name,
        )
    )


def character_card_post_history_prompt(user_name: str) -> str:
    if CHARACTER_CARD is None:
        return ""
    return render_post_history_instructions(CHARACTER_CARD, user_name=user_name)


def character_card_group_greetings_prompt() -> str:
    """Render a small, inert sample of the card's group-only greetings."""
    if CHARACTER_CARD is None or not CARD_INTEGRITY_OK:
        return ""
    greetings = []
    for value in CHARACTER_CARD.group_only_greetings[:3]:
        rendered = replace_supported_macros(
            value,
            char_name=CHARACTER_CARD.char_name,
            user_name="群友",
        )
        if rendered:
            greetings.append(rendered[:360])
    if not greetings:
        return ""
    return "角色卡群聊主动开场示范，只借节奏，不逐字照抄：\n" + "\n".join(
        "- " + greeting for greeting in greetings
    )


def visible_user_request(message: str) -> str:
    value = str(message or "").translate(_INTERNAL_FORMAT_TRANSLATION)
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
            "本轮按自然群聊回答：一到三个短段落、最多 %d 字；短接话可以一句。"
            % SHORT_REPLY_MAX_CHARS
        )
    return PERSONA_TURN_PROMPT + "\n" + length_rule


def _remove_machine_wrapping(text: str) -> str:
    value = _LEADING_FILLER_RE.sub("", text.strip())
    value = _TRAILING_OFFER_RE.sub("", value).strip()
    value = _TRAILING_CLICHE_RE.sub("", value).strip()
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    lines = [
        line
        for line in value.splitlines()
        if not _MARKDOWN_HEADING_RE.fullmatch(line)
    ]
    return "\n".join(lines).strip()


def _reply_repetition_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _deduplicate_repeated_reply_segments(value: str) -> str:
    """Remove only exact, substantial repetitions inside one model reply."""
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", value)
        if paragraph.strip()
    ]
    unique_paragraphs: list[str] = []
    seen_paragraphs: set[str] = set()
    for paragraph in paragraphs:
        unique_sentences: list[str] = []
        seen_sentences: set[str] = set()
        for raw_sentence in re.split(r"(?<=[。！？!?.])", paragraph):
            sentence = raw_sentence.strip()
            if not sentence:
                continue
            marker = _reply_repetition_key(sentence)
            if len(marker) >= 4 and marker in seen_sentences:
                continue
            if marker:
                seen_sentences.add(marker)
            unique_sentences.append(raw_sentence)
        compact_paragraph = "".join(unique_sentences).strip()
        marker = _reply_repetition_key(compact_paragraph)
        if not compact_paragraph:
            continue
        if len(marker) >= 4 and marker in seen_paragraphs:
            continue
        if marker:
            seen_paragraphs.add(marker)
        unique_paragraphs.append(compact_paragraph)
    return "\n\n".join(unique_paragraphs).strip()


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
    # Model output is user-visible content; internal zero-width correlation
    # characters must never survive this boundary.
    original = str(reply or "").translate(_INTERNAL_FORMAT_TRANSLATION).strip()
    if original == "[[NO_REPLY]]":
        return original
    expanded = expanded_reply_requested(message)
    value = strip_leading_presence_confirmation(_remove_machine_wrapping(original))
    if not value:
        value = original
    value = _deduplicate_repeated_reply_segments(value)

    limit = EXPANDED_REPLY_MAX_CHARS if expanded else SHORT_REPLY_MAX_CHARS
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", value)
        if paragraph.strip()
    ]
    if not expanded:
        paragraphs = paragraphs[:3]
    compact = "\n\n".join(paragraphs).strip()
    if len(compact) <= limit:
        return compact
    return _truncate_fragment(compact, limit)
