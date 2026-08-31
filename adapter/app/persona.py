from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .group_listener import (
    is_low_information_reply,
    strip_internal_format_chars,
    strip_leading_presence_confirmation,
)


WEIRDOTV_SKILL_SOURCE = "https://github.com/BeamusWayne/WeirdoTV-Skill"
WEIRDOTV_SKILL_COMMIT = "1635aceebf4e84b32db37ccd00244ca0dcc04574"
WEIRDOTV_SKILL_VERSION = "1.0.0"
WEIRDOTV_SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "weirdotv-sunxiaochuan"
    / "SKILL.md"
)
WEIRDOTV_SKILL_LICENSE_PATH = WEIRDOTV_SKILL_PATH.with_name("LICENSE")
WEIRDOTV_SKILL_SOURCE_LOCK_PATH = WEIRDOTV_SKILL_PATH.with_name("SOURCE.lock.json")
WEIRDOTV_SKILL_SHA256 = (
    "471af1edc7cf88f89549b9ff3d17952810d7e55eaafb647ac21584be96801305"
)
WEIRDOTV_SKILL_LICENSE_SHA256 = (
    "4b0120b81a3a308bb66761cd001fea4d1306fbd0d548e4714c8d878519ffd2c1"
)

SUNXIAOCHUAN_SECTION_PATH = (
    Path(__file__).resolve().parents[1]
    / "personas"
    / "sunxiaochuan.section.md"
)
SUNXIAOCHUAN_SECTION_LOCK_PATH = SUNXIAOCHUAN_SECTION_PATH.with_name(
    "SOURCE.lock.json"
)
SUNXIAOCHUAN_SECTION_SHA256 = (
    "b1fa3a4d08206c0210edd527dd2ef30e5ef36bd4eec7401881de873aa75fa922"
)
SUNXIAOCHUAN_SECTION_HEADING = "### 😂 孙笑川 Sun Xiaochuan"

# This release deliberately has one source of personality. The display name
# remains 小格 for WeChat routing, while the words below come only from the
# pinned upstream section.
PERSONA_VERSION = "weirdotv@1.0.0+sunxiaochuan@2.0.0"
PERSONA_SKILL_SOURCE = WEIRDOTV_SKILL_SOURCE
PERSONA_SKILL_COMMIT = WEIRDOTV_SKILL_COMMIT
PERSONA_SKILL_VERSION = WEIRDOTV_SKILL_VERSION
PERSONA_SKILL_PATH = SUNXIAOCHUAN_SECTION_PATH
PERSONA_SKILL_SHA256 = SUNXIAOCHUAN_SECTION_SHA256

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


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _normalized_source_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def extract_sunxiaochuan_section(source_text: str) -> str:
    """Extract one stable section from the pinned upstream Markdown archive."""
    normalized = _normalized_source_text(source_text)
    start = normalized.find(SUNXIAOCHUAN_SECTION_HEADING)
    if start < 0 or (
        start > 0 and normalized[start - 1] != "\n"
    ):
        return ""
    next_heading = re.search(r"^###\s+", normalized[start + 1 :], re.MULTILINE)
    end = start + 1 + next_heading.start() if next_heading else len(normalized)
    section = normalized[start:end].strip()
    # The upstream file uses a thematic separator between roster entries; it
    # belongs to the archive layout, not to the persona chapter itself.
    section = re.sub(r"\n\s*---\s*$", "", section).strip()
    return section


def weirdotv_source_archive_integrity() -> bool:
    """Verify the pinned upstream archive kept for provenance."""
    if _sha256(WEIRDOTV_SKILL_PATH) != WEIRDOTV_SKILL_SHA256:
        return False
    if _sha256(WEIRDOTV_SKILL_LICENSE_PATH) != WEIRDOTV_SKILL_LICENSE_SHA256:
        return False
    try:
        lock = json.loads(
            WEIRDOTV_SKILL_SOURCE_LOCK_PATH.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    files = lock.get("files") if isinstance(lock, dict) else None
    return bool(
        isinstance(files, dict)
        and lock.get("name") == "weirdo-tv-sunxiaochuan"
        and lock.get("version") == WEIRDOTV_SKILL_VERSION
        and lock.get("source") == WEIRDOTV_SKILL_SOURCE
        and lock.get("commit") == WEIRDOTV_SKILL_COMMIT
        and lock.get("license") == "MIT"
        and files.get("SKILL.md") == WEIRDOTV_SKILL_SHA256
        and files.get("LICENSE") == WEIRDOTV_SKILL_LICENSE_SHA256
        and lock.get("loaded_section") == SUNXIAOCHUAN_SECTION_HEADING
    )


def sunxiaochuan_section_integrity() -> bool:
    """Verify that the runtime resource contains only the pinned section."""
    if _sha256(SUNXIAOCHUAN_SECTION_PATH) != SUNXIAOCHUAN_SECTION_SHA256:
        return False
    try:
        text = SUNXIAOCHUAN_SECTION_PATH.read_text(encoding="utf-8")
        source_text = WEIRDOTV_SKILL_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    normalized = _normalized_source_text(text)
    if extract_sunxiaochuan_section(source_text) != normalized:
        return False
    headings = re.findall(r"^###\s+.+$", normalized, flags=re.MULTILINE)
    if headings != [SUNXIAOCHUAN_SECTION_HEADING]:
        return False
    try:
        lock = json.loads(
            SUNXIAOCHUAN_SECTION_LOCK_PATH.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    files = lock.get("files") if isinstance(lock, dict) else None
    return bool(
        isinstance(files, dict)
        and lock.get("schema_version") == 2
        and lock.get("name") == "sunxiaochuan"
        and lock.get("version") == "2.0.0"
        and lock.get("source") == WEIRDOTV_SKILL_SOURCE
        and lock.get("commit") == WEIRDOTV_SKILL_COMMIT
        and lock.get("license") == "MIT"
        and lock.get("section") == SUNXIAOCHUAN_SECTION_HEADING
        and files.get("sunxiaochuan.section.md") == SUNXIAOCHUAN_SECTION_SHA256
        and files.get("upstream.SKILL.md") == WEIRDOTV_SKILL_SHA256
        and files.get("LICENSE") == WEIRDOTV_SKILL_LICENSE_SHA256
    )


WEIRDOTV_SKILL_ACTUAL_SHA256 = _sha256(WEIRDOTV_SKILL_PATH)
SUNXIAOCHUAN_SECTION_ACTUAL_SHA256 = _sha256(SUNXIAOCHUAN_SECTION_PATH)
WEIRDOTV_SKILL_INTEGRITY_OK = weirdotv_source_archive_integrity()
SUNXIAOCHUAN_SECTION_INTEGRITY_OK = sunxiaochuan_section_integrity()
PERSONA_SKILL_ACTUAL_SHA256 = SUNXIAOCHUAN_SECTION_ACTUAL_SHA256
PERSONA_SKILL_INTEGRITY_OK = bool(
    WEIRDOTV_SKILL_INTEGRITY_OK and SUNXIAOCHUAN_SECTION_INTEGRITY_OK
)

CARD_LOAD_ERROR = ""
if not WEIRDOTV_SKILL_INTEGRITY_OK:
    CARD_LOAD_ERROR = "pinned WeirdoTV source archive integrity failed"
elif not SUNXIAOCHUAN_SECTION_INTEGRITY_OK:
    CARD_LOAD_ERROR = "Sun Xiaochuan section integrity failed"

# Compatibility aliases for callers from the previous card release. They do
# not represent a loaded Character Card and are never added to a model prompt.
CHARACTER_CARD = None
CARD_INTEGRITY_OK = SUNXIAOCHUAN_SECTION_INTEGRITY_OK

try:
    PERSONA_SKILL_PROMPT = (
        SUNXIAOCHUAN_SECTION_PATH.read_text(encoding="utf-8").strip()
        if PERSONA_SKILL_INTEGRITY_OK
        else ""
    )
except (OSError, UnicodeDecodeError):
    PERSONA_SKILL_PROMPT = ""
WEIRDOTV_SKILL_PROMPT = PERSONA_SKILL_PROMPT
PERSONA_SYSTEM_PROMPT = PERSONA_SKILL_PROMPT

# Kept as an empty compatibility symbol for integrations importing the old
# adapter layer. It is intentionally absent from all runtime prompts.
PERSONA_CHAT_ADAPTER = ""

PERSONA_SKILL_BUNDLES = (
    {
        "name": "weirdo-tv-sunxiaochuan",
        "version": PERSONA_SKILL_VERSION,
        "source": PERSONA_SKILL_SOURCE,
        "commit": PERSONA_SKILL_COMMIT,
        "sha256": PERSONA_SKILL_SHA256,
        "archive_sha256": WEIRDOTV_SKILL_SHA256,
        "integrity": PERSONA_SKILL_INTEGRITY_OK,
        "loaded_sections": ["Sun Xiaochuan section only"],
    },
)


def visible_user_request(message: str) -> str:
    value = str(message or "").translate(_INTERNAL_FORMAT_TRANSLATION)
    for marker in _CONTEXT_MARKERS:
        value = value.split(marker, 1)[0]
    return value.strip()


def expanded_reply_requested(message: str) -> bool:
    return bool(_EXPANDED_REQUEST_RE.search(visible_user_request(message)))


# Legacy names remain importable for old diagnostic helpers. Returning an empty
# value keeps the old card, Lorebook and turn-instruction layers out of every
# new model request.
def character_card_prompt(user_name: str = "小格") -> str:
    del user_name
    return ""


def character_card_lorebook_prompt(
    history: list[dict[str, Any]],
    *,
    user_name: str,
) -> str:
    del history, user_name
    return ""


def character_card_post_history_prompt(user_name: str = "小格") -> str:
    del user_name
    return ""


def character_card_group_greetings_prompt() -> str:
    return ""


def chat_turn_prompt(message: str) -> str:
    del message
    return ""


PERSONA_TASK_PROMPT = ""


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


def _remove_embedded_presence_confirmations(value: str) -> str:
    """Drop standalone arrival pings even when they occur after real text."""
    chunks = re.split(r"(?<=[。！？!?])", value)
    if len(chunks) <= 1:
        return value
    kept: list[str] = []
    for index, chunk in enumerate(chunks):
        candidate = chunk.strip().strip("，,。！？!?；;:：~～")
        if index > 0 and candidate and is_low_information_reply(candidate):
            continue
        kept.append(chunk)
    return "".join(kept).strip()


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
    original = strip_internal_format_chars(reply).strip()
    if original == "[[NO_REPLY]]":
        return ""
    if "[[NO_REPLY]]" in original:
        original = original.replace("[[NO_REPLY]]", "").strip()
    if not original:
        return ""
    expanded = expanded_reply_requested(message)
    value = strip_leading_presence_confirmation(_remove_machine_wrapping(original))
    if not value:
        value = original
    value = _remove_embedded_presence_confirmations(value)
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
