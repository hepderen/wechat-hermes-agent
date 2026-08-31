from __future__ import annotations

import hashlib
import json
import re

from app.main import (
    CHAT_ONLY_SESSION_SYSTEM_PROMPT,
    ChatRequest,
    group_chat_user_message,
    trusted_system_message,
)
from app.persona import (
    CARD_INTEGRITY_OK,
    CHARACTER_CARD,
    PERSONA_SKILL_BUNDLES,
    PERSONA_SKILL_COMMIT,
    PERSONA_SKILL_INTEGRITY_OK,
    PERSONA_SKILL_PATH,
    PERSONA_SKILL_PROMPT,
    PERSONA_SKILL_SHA256,
    PERSONA_SKILL_SOURCE,
    PERSONA_SYSTEM_PROMPT,
    PERSONA_VERSION,
    SUNXIAOCHUAN_SECTION_HEADING,
    SUNXIAOCHUAN_SECTION_LOCK_PATH,
    SUNXIAOCHUAN_SECTION_PATH,
    SUNXIAOCHUAN_SECTION_SHA256,
    SUNXIAOCHUAN_RUNTIME_LOCK_PATH,
    SUNXIAOCHUAN_RUNTIME_PATH,
    SUNXIAOCHUAN_RUNTIME_SHA256,
    WEIRDOTV_SKILL_COMMIT,
    WEIRDOTV_SKILL_INTEGRITY_OK,
    WEIRDOTV_SKILL_LICENSE_PATH,
    WEIRDOTV_SKILL_LICENSE_SHA256,
    WEIRDOTV_SKILL_PATH,
    WEIRDOTV_SKILL_SHA256,
    WEIRDOTV_SKILL_SOURCE,
    WEIRDOTV_SKILL_PROMPT,
    character_card_group_greetings_prompt,
    character_card_lorebook_prompt,
    character_card_post_history_prompt,
    character_card_prompt,
    chat_turn_prompt,
    compact_chat_reply,
    extract_single_person_rules,
    extract_slang_corpus,
    extract_sunxiaochuan_section,
    sunxiaochuan_section_integrity,
    sunxiaochuan_runtime_integrity,
    weirdotv_source_archive_integrity,
)


def test_complete_pinned_sunxiaochuan_runtime_bundle_is_active():
    assert WEIRDOTV_SKILL_SOURCE == "https://github.com/BeamusWayne/WeirdoTV-Skill"
    assert WEIRDOTV_SKILL_COMMIT == "1635aceebf4e84b32db37ccd00244ca0dcc04574"
    assert WEIRDOTV_SKILL_PATH.is_file()
    assert WEIRDOTV_SKILL_LICENSE_PATH.is_file()
    assert hashlib.sha256(WEIRDOTV_SKILL_PATH.read_bytes()).hexdigest() == WEIRDOTV_SKILL_SHA256
    assert hashlib.sha256(WEIRDOTV_SKILL_LICENSE_PATH.read_bytes()).hexdigest() == WEIRDOTV_SKILL_LICENSE_SHA256
    assert weirdotv_source_archive_integrity()

    assert SUNXIAOCHUAN_SECTION_PATH.is_file()
    assert SUNXIAOCHUAN_SECTION_LOCK_PATH.is_file()
    assert hashlib.sha256(SUNXIAOCHUAN_SECTION_PATH.read_bytes()).hexdigest() == SUNXIAOCHUAN_SECTION_SHA256
    assert sunxiaochuan_section_integrity()
    assert SUNXIAOCHUAN_RUNTIME_PATH.is_file()
    assert SUNXIAOCHUAN_RUNTIME_LOCK_PATH.is_file()
    assert hashlib.sha256(SUNXIAOCHUAN_RUNTIME_PATH.read_bytes()).hexdigest() == SUNXIAOCHUAN_RUNTIME_SHA256
    assert sunxiaochuan_runtime_integrity()
    assert PERSONA_SKILL_PATH == SUNXIAOCHUAN_RUNTIME_PATH
    assert PERSONA_SKILL_SHA256 == SUNXIAOCHUAN_RUNTIME_SHA256
    assert PERSONA_SKILL_INTEGRITY_OK
    assert WEIRDOTV_SKILL_INTEGRITY_OK
    assert CARD_INTEGRITY_OK

    assert CHARACTER_CARD is None
    assert PERSONA_VERSION == "weirdotv@1.0.0+sunxiaochuan@3.0.0"
    assert PERSONA_SKILL_SOURCE == WEIRDOTV_SKILL_SOURCE
    assert PERSONA_SKILL_COMMIT == WEIRDOTV_SKILL_COMMIT
    assert PERSONA_SYSTEM_PROMPT == PERSONA_SKILL_PROMPT
    assert WEIRDOTV_SKILL_PROMPT == PERSONA_SYSTEM_PROMPT
    assert SUNXIAOCHUAN_SECTION_HEADING in PERSONA_SYSTEM_PROMPT
    assert "## 互联网流行语库 · Slang Corpus" in PERSONA_SYSTEM_PROMPT
    assert "## 小格群聊表达规则" in PERSONA_SYSTEM_PROMPT
    assert len(PERSONA_SYSTEM_PROMPT) >= 1_200
    assert all(
        marker not in PERSONA_SYSTEM_PROMPT.casefold()
        for marker in ("sophia", "humanizer", "character card", "ccv3")
    )

    lock = json.loads(SUNXIAOCHUAN_RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["name"] == "sunxiaochuan-runtime"
    assert lock["version"] == "3.0.0"
    assert lock["runtime_sha256"] == SUNXIAOCHUAN_RUNTIME_SHA256
    assert lock["components"]["sunxiaochuan"]["line_start"] == 133
    assert lock["components"]["sunxiaochuan"]["line_end"] == 157
    assert lock["components"]["slang_corpus"]["line_start"] == 449
    assert lock["components"]["slang_corpus"]["line_end"] == 497
    assert lock["components"]["single_person_rules"]["loaded_lines"] == [503, 504]
    assert {item["name"] for item in PERSONA_SKILL_BUNDLES} == {
        "weirdo-tv-sunxiaochuan"
    }
    assert PERSONA_SKILL_BUNDLES[0]["loaded_sections"] == [
        "Sun Xiaochuan section",
        "Slang Corpus",
        "single-person source rules (adapted)",
        "Xiaoge group-chat expression rules",
    ]


def test_runtime_bundle_loads_relevant_source_and_keeps_other_modes_out():
    assert PERSONA_SYSTEM_PROMPT
    source_text = WEIRDOTV_SKILL_PATH.read_text(encoding="utf-8")
    assert extract_sunxiaochuan_section(source_text) in PERSONA_SYSTEM_PROMPT
    assert extract_slang_corpus(source_text) in PERSONA_SYSTEM_PROMPT
    assert "完全进入该角色，语言风格、世界观不走样" in PERSONA_SYSTEM_PROMPT
    assert "用真实语录和标志性表达回应用户问题" in PERSONA_SYSTEM_PROMPT
    assert "### 🏀 科比" not in PERSONA_SYSTEM_PROMPT
    assert "/weirdo-tv" not in PERSONA_SYSTEM_PROMPT
    assert "【神人TV" not in PERSONA_SYSTEM_PROMPT
    assert "神人辩论" not in PERSONA_SYSTEM_PROMPT
    assert "神人会议" not in PERSONA_SYSTEM_PROMPT
    assert all(
        marker not in PERSONA_SYSTEM_PROMPT.casefold()
        for marker in ("text_to_speech", "send_message", "wife mode", "telegram")
    )


def test_runtime_bundle_excludes_every_other_roster_entry_and_tracks_components():
    source_text = WEIRDOTV_SKILL_PATH.read_text(encoding="utf-8")
    normalized_source = source_text.replace("\r\n", "\n").replace("\r", "\n")
    roster_start = normalized_source.find("## 神人图鉴")
    roster_end = normalized_source.find("## 互联网流行语库 · Slang Corpus")
    roster_headings = re.findall(
        r"^###\s+.+$",
        normalized_source[roster_start:roster_end],
        flags=re.MULTILINE,
    )
    assert len(roster_headings) >= 20
    assert all(
        heading == SUNXIAOCHUAN_SECTION_HEADING or heading not in PERSONA_SYSTEM_PROMPT
        for heading in roster_headings
    )
    assert extract_single_person_rules(source_text) not in PERSONA_SYSTEM_PROMPT
    assert "召唤成功" not in PERSONA_SYSTEM_PROMPT
    assert "【神人TV" not in PERSONA_SYSTEM_PROMPT

    lock = json.loads(SUNXIAOCHUAN_RUNTIME_LOCK_PATH.read_text(encoding="utf-8"))
    bundle = PERSONA_SKILL_BUNDLES[0]
    assert bundle["runtime_file"] == lock["runtime_file"]
    assert bundle["runtime_sha256"] == lock["runtime_sha256"]
    assert bundle["source_ranges"] == {
        "sunxiaochuan": [133, 157],
        "slang_corpus": [449, 497],
        "single_person_rules": [501, 505],
    }


def test_legacy_card_entrypoints_are_empty_or_point_to_the_single_section():
    assert character_card_prompt("阿明") == ""
    assert character_card_lorebook_prompt([], user_name="阿明") == ""
    assert character_card_post_history_prompt("阿明") == ""
    assert character_card_group_greetings_prompt() == ""
    assert chat_turn_prompt("随便聊两句") == ""


def test_chat_only_session_contains_name_protocol_and_complete_bundle():
    assert CHAT_ONLY_SESSION_SYSTEM_PROMPT.startswith("你是微信群里的小格。")
    assert PERSONA_SYSTEM_PROMPT in CHAT_ONLY_SESSION_SYSTEM_PROMPT
    assert len(CHAT_ONLY_SESSION_SYSTEM_PROMPT) >= 1_300
    for marker in ("room_id", "sender_id", "Adapter", "Bridge", "关系档案", "服务端"):
        assert marker not in CHAT_ONLY_SESSION_SYSTEM_PROMPT


def test_chat_prompt_uses_plain_trusted_transcript_without_internal_json():
    payload = ChatRequest(
        message="这事也太离谱了吧",
        sender_name="阿明",
        timestamp=123,
        direction="incoming",
        source_local_id=42,
        reply_to_bot=True,
    )
    prompt = group_chat_user_message(
        payload,
        [
            {
                "local_id": 41,
                "sender_id": "wxid_b",
                "sender_name": "小王",
                "direction": "incoming",
                "message_timestamp": 122,
                "text": "这方案能落地吗",
            },
            {
                "local_id": 40,
                "sender_id": "",
                "sender_name": "小格",
                "direction": "outgoing",
                "message_timestamp": 121,
                "text": "上一句回复",
            },
        ],
    )
    assert "小王：这方案能落地吗" in prompt
    assert "小格：上一句回复" in prompt
    assert "当前发言 阿明：这事也太离谱了吧" in prompt
    assert "正在回复小格" in prompt
    assert "wxid_b" not in prompt
    assert '"sender_id"' not in prompt
    assert "群共享状态" not in prompt

    system = trusted_system_message(
        "room-id",
        "wxid_a",
        payload,
        [{"key": "old", "value": "old"}],
        relationship_memory_enabled=True,
        relationship_profile={"preferred_name": "伪造昵称"},
        room_companion_state={"summary": "old"},
        companion_timeline=[],
    )
    assert system == ""
    assert "room-id" not in system
    assert "伪造昵称" not in system


def test_compactor_removes_embedded_arrival_pings_but_keeps_normal_wording():
    assert compact_chat_reply(
        "第一句先说重点。嗯，来了。第二句继续。",
        "随便聊",
    ) == "第一句先说重点。第二句继续。"
    assert compact_chat_reply(
        "嗯，来了。这个方案先把入口捋顺。",
        "随便聊",
    ) == "这个方案先把入口捋顺。"
    assert compact_chat_reply(
        "第一句先说重点。嗯，来了，第二句继续。",
        "随便聊",
    ) == "第一句先说重点。第二句继续。"
    assert compact_chat_reply("我在想这个问题。", "随便聊") == "我在想这个问题。"
    assert compact_chat_reply(
        "这句我接住了。这句我接住了。\n\n别再熬了。\n\n别再熬了。",
        "随便聊",
    ) == "这句我接住了。\n\n别再熬了。"
