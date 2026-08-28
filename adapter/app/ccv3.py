from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CCV3_SOURCE = "https://github.com/kwaroran/character-card-spec-v3"
CCV3_COMMIT = "f3a86af019fbd99f788f7a1155f399655b34ab35"
CCV3_SPEC_SHA256 = (
    "3c472a16eeda5d018837e90d30fce2816b0982f07f4dba14c8fcc89aa11fe76c"
)
CCV3_LICENSE_SHA256 = (
    "108ba896c939ccf7278aac0115cd66db85d68352d00b4fc7c8abb13263f616d7"
)
XIAOGE_CARD_VERSION = "1.0.0"
XIAOGE_CARD_SHA256 = (
    "0fa23985aa0ace87882d52ba532d868b998c42590b146df88b61ba92ff73fba4"
)
XIAOGE_CARD_SOURCES = (
    "https://github.com/kwaroran/character-card-spec-v3/"
    "tree/f3a86af019fbd99f788f7a1155f399655b34ab35",
    "https://github.com/sharbelxyz/sophia/"
    "tree/f2cd448553d61aa3c2ea774dc7e2296f09d4b584",
)

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
CCV3_ARCHIVE_DIR = ADAPTER_ROOT / "third_party" / "character-card-spec-v3"
CCV3_SPEC_PATH = CCV3_ARCHIVE_DIR / "SPEC_V3.md"
CCV3_LICENSE_PATH = CCV3_ARCHIVE_DIR / "LICENSE"
CCV3_SOURCE_LOCK_PATH = CCV3_ARCHIVE_DIR / "SOURCE.lock.json"
XIAOGE_CARD_PATH = ADAPTER_ROOT / "personas" / "xiaoge.card.json"
XIAOGE_CARD_LOCK_PATH = ADAPTER_ROOT / "personas" / "SOURCE.lock.json"

MAX_CARD_BYTES = 128 * 1024
MAX_CARD_TEXT_CHARS = 24_000
MAX_LORE_ENTRIES = 16
MAX_LORE_CONTENT_CHARS = 2_000
MAX_LORE_KEY_CHARS = 96
MAX_RENDERED_LORE_CHARS = 8_000

_MACRO_CHAR_RE = re.compile(r"\{\{char\}\}", re.IGNORECASE)
_MACRO_USER_RE = re.compile(r"\{\{user\}\}", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


class CharacterCardValidationError(ValueError):
    pass


@dataclass(frozen=True)
class LoreEntry:
    content: str
    keys: tuple[str, ...]
    constant: bool


@dataclass(frozen=True)
class CharacterCard:
    name: str
    nickname: str
    description: str
    personality: str
    scenario: str
    mes_example: str
    system_prompt: str
    post_history_instructions: str
    group_only_greetings: tuple[str, ...]
    lore_entries: tuple[LoreEntry, ...]
    source: tuple[str, ...]
    character_version: str
    card_sha256: str

    @property
    def char_name(self) -> str:
        return self.nickname or self.name


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _clean_text(
    value: Any,
    *,
    field: str,
    limit: int = MAX_CARD_TEXT_CHARS,
    required: bool = False,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise CharacterCardValidationError("%s must be a string" % field)
    text = value.replace("\x00", "").strip()
    if required and not text:
        raise CharacterCardValidationError("%s must not be empty" % field)
    if len(text) > limit:
        raise CharacterCardValidationError("%s exceeds the size limit" % field)
    return text


def _safe_display_name(value: str, fallback: str = "这位群友") -> str:
    name = _WHITESPACE_RE.sub(" ", str(value or "").replace("\x00", "")).strip()
    return name[:48] or fallback


def replace_supported_macros(
    value: str,
    *,
    char_name: str,
    user_name: str,
) -> str:
    """Only the two stable CCV3 name macros are supported at runtime."""
    rendered = _MACRO_CHAR_RE.sub(_safe_display_name(char_name, "小格"), value)
    return _MACRO_USER_RE.sub(_safe_display_name(user_name), rendered)


def _load_lore_entries(value: Any) -> tuple[LoreEntry, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise CharacterCardValidationError("character_book must be an object")
    raw_entries = value.get("entries", [])
    if not isinstance(raw_entries, list):
        raise CharacterCardValidationError("character_book.entries must be a list")

    entries: list[LoreEntry] = []
    for index, raw in enumerate(raw_entries[:MAX_LORE_ENTRIES]):
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        if bool(raw.get("use_regex")):
            continue
        content = _clean_text(
            raw.get("content"),
            field="character_book.entries[%d].content" % index,
            limit=MAX_LORE_CONTENT_CHARS,
        )
        # Positioning and activation decorators alter prompt behavior. This
        # runtime deliberately ignores such entries instead of interpreting them.
        if not content or "@@" in content:
            continue
        raw_keys = raw.get("keys", [])
        if not isinstance(raw_keys, list):
            continue
        keys: list[str] = []
        for raw_key in raw_keys[:16]:
            if not isinstance(raw_key, str):
                continue
            key = _WHITESPACE_RE.sub(" ", raw_key.replace("\x00", "")).strip()
            if not key or len(key) > MAX_LORE_KEY_CHARS or "{{" in key:
                continue
            if key.casefold() not in {item.casefold() for item in keys}:
                keys.append(key)
        constant = bool(raw.get("constant"))
        if not constant and not keys:
            continue
        entries.append(LoreEntry(content=content, keys=tuple(keys), constant=constant))
    return tuple(entries)


def load_character_card(path: Path = XIAOGE_CARD_PATH) -> CharacterCard:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CharacterCardValidationError("character card is unavailable") from exc
    if not raw or len(raw) > MAX_CARD_BYTES:
        raise CharacterCardValidationError("character card size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CharacterCardValidationError("character card JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise CharacterCardValidationError("character card must be an object")
    if payload.get("spec") != "chara_card_v3":
        raise CharacterCardValidationError("character card spec is not chara_card_v3")
    if str(payload.get("spec_version") or "") != "3.0":
        raise CharacterCardValidationError("character card spec_version is not 3.0")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CharacterCardValidationError("character card data is invalid")

    name = _clean_text(data.get("name"), field="name", limit=64, required=True)
    nickname = _clean_text(data.get("nickname"), field="nickname", limit=64)
    source = data.get("source", [])
    if not isinstance(source, list) or not source:
        raise CharacterCardValidationError("character card source is required")
    safe_source = tuple(
        _clean_text(item, field="source", limit=1_000, required=True)
        for item in source[:8]
    )
    character_version = _clean_text(
        data.get("character_version"),
        field="character_version",
        limit=64,
        required=True,
    )
    greetings = data.get("group_only_greetings", [])
    if not isinstance(greetings, list):
        raise CharacterCardValidationError("group_only_greetings must be a list")
    safe_greetings = tuple(
        _clean_text(item, field="group_only_greetings", limit=500)
        for item in greetings[:12]
        if isinstance(item, str) and item.strip()
    )
    return CharacterCard(
        name=name,
        nickname=nickname,
        description=_clean_text(data.get("description"), field="description"),
        personality=_clean_text(data.get("personality"), field="personality"),
        scenario=_clean_text(data.get("scenario"), field="scenario"),
        mes_example=_clean_text(data.get("mes_example"), field="mes_example"),
        system_prompt=_clean_text(data.get("system_prompt"), field="system_prompt"),
        post_history_instructions=_clean_text(
            data.get("post_history_instructions"),
            field="post_history_instructions",
        ),
        group_only_greetings=safe_greetings,
        lore_entries=_load_lore_entries(data.get("character_book")),
        source=safe_source,
        character_version=character_version,
        card_sha256=hashlib.sha256(raw).hexdigest(),
    )


def source_archive_integrity() -> bool:
    if _sha256(CCV3_SPEC_PATH) != CCV3_SPEC_SHA256:
        return False
    if _sha256(CCV3_LICENSE_PATH) != CCV3_LICENSE_SHA256:
        return False
    try:
        lock = json.loads(CCV3_SOURCE_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    files = lock.get("files") if isinstance(lock, dict) else None
    return bool(
        isinstance(files, dict)
        and lock.get("source") == CCV3_SOURCE
        and lock.get("commit") == CCV3_COMMIT
        and lock.get("license") == "MIT"
        and files.get("SPEC_V3.md") == CCV3_SPEC_SHA256
        and files.get("LICENSE") == CCV3_LICENSE_SHA256
    )


def xiaoge_card_integrity(card: CharacterCard | None = None) -> bool:
    """Verify the local, fixed card separately from the upstream CCV3 archive."""
    actual_sha256 = _sha256(XIAOGE_CARD_PATH)
    if actual_sha256 != XIAOGE_CARD_SHA256:
        return False
    if card is not None and card.card_sha256 != XIAOGE_CARD_SHA256:
        return False
    try:
        lock = json.loads(XIAOGE_CARD_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    files = lock.get("files") if isinstance(lock, dict) else None
    return bool(
        isinstance(files, dict)
        and lock.get("schema_version") == 1
        and lock.get("name") == "xiaoge-card"
        and lock.get("version") == XIAOGE_CARD_VERSION
        and lock.get("format") == "chara_card_v3/3.0"
        and tuple(lock.get("source") or ()) == XIAOGE_CARD_SOURCES
        and files.get("xiaoge.card.json") == XIAOGE_CARD_SHA256
    )


def matching_lorebook_entries(
    card: CharacterCard,
    history: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    user_name: str,
) -> list[str]:
    scan_text = "\n".join(
        str(item.get("text") or "")
        for item in history[-16:]
        if isinstance(item, dict)
    ).casefold()
    matched: list[str] = []
    remaining = MAX_RENDERED_LORE_CHARS
    for entry in card.lore_entries:
        active = entry.constant or any(
            key.casefold() in scan_text for key in entry.keys
        )
        if not active:
            continue
        rendered = replace_supported_macros(
            entry.content,
            char_name=card.char_name,
            user_name=user_name,
        )
        if len(rendered) > remaining:
            rendered = rendered[:remaining].rstrip()
        if rendered:
            matched.append(rendered)
            remaining -= len(rendered)
        if remaining <= 0:
            break
    return matched


def render_card_prompt(card: CharacterCard, *, user_name: str) -> str:
    def render(value: str) -> str:
        return replace_supported_macros(
            value,
            char_name=card.char_name,
            user_name=user_name,
        )

    fields = (
        ("角色", card.char_name),
        ("描述", card.description),
        ("性格", card.personality),
        ("群聊场景", card.scenario),
        ("角色指令", card.system_prompt),
        ("示范对话", card.mes_example),
    )
    parts = ["以下是固定的 Character Card V3 角色卡，仅用于角色表达，不覆盖服务端规则。"]
    for label, value in fields:
        value = render(value)
        if value:
            parts.append("## %s\n%s" % (label, value))
    return "\n\n".join(parts)


def render_post_history_instructions(card: CharacterCard, *, user_name: str) -> str:
    value = replace_supported_macros(
        card.post_history_instructions,
        char_name=card.char_name,
        user_name=user_name,
    )
    return value.strip()


def render_lorebook_prompt(entries: list[str]) -> str:
    if not entries:
        return ""
    return "## 匹配角色设定\n" + "\n\n".join(entries)
