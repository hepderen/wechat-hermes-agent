import hashlib
import json

import pytest

from app.ccv3 import (
    CCV3_COMMIT,
    CCV3_LICENSE_PATH,
    CCV3_LICENSE_SHA256,
    CCV3_SOURCE,
    CCV3_SPEC_PATH,
    CCV3_SPEC_SHA256,
    CharacterCardValidationError,
    XIAOGE_CARD_LOCK_PATH,
    XIAOGE_CARD_PATH,
    XIAOGE_CARD_SHA256,
    XIAOGE_CARD_SOURCES,
    XIAOGE_CARD_VERSION,
    load_character_card,
    matching_lorebook_entries,
    replace_supported_macros,
    source_archive_integrity,
    xiaoge_card_integrity,
)
from app.main import ChatRequest, trusted_system_message
from app.persona import (
    CARD_INTEGRITY_OK,
    CHARACTER_CARD,
    PERSONA_CHAT_ADAPTER,
    PERSONA_SKILL_BUNDLES,
    PERSONA_SKILL_INTEGRITY_OK,
    PERSONA_SYSTEM_PROMPT,
    PERSONA_VERSION,
    SHORT_REPLY_MAX_CHARS,
    SOPHIA_SKILL_COMMIT,
    SOPHIA_SKILL_INTEGRITY_OK,
    SOPHIA_SKILL_LICENSE_PATH,
    SOPHIA_SKILL_LICENSE_SHA256,
    SOPHIA_SKILL_PATH,
    SOPHIA_SKILL_PROMPT,
    SOPHIA_SKILL_SHA256,
    SOPHIA_SKILL_SOURCE,
    character_card_lorebook_prompt,
    character_card_group_greetings_prompt,
    character_card_post_history_prompt,
    character_card_prompt,
    chat_turn_prompt,
    compact_chat_reply,
    expanded_reply_requested,
    sophia_source_archive_integrity,
)


def test_pinned_ccv3_archive_and_xiaoge_card_are_verified():
    assert CCV3_SOURCE == "https://github.com/kwaroran/character-card-spec-v3"
    assert CCV3_COMMIT == "f3a86af019fbd99f788f7a1155f399655b34ab35"
    assert hashlib.sha256(CCV3_SPEC_PATH.read_bytes()).hexdigest() == CCV3_SPEC_SHA256
    assert hashlib.sha256(CCV3_LICENSE_PATH.read_bytes()).hexdigest() == CCV3_LICENSE_SHA256
    assert source_archive_integrity()
    assert CARD_INTEGRITY_OK
    assert PERSONA_SKILL_INTEGRITY_OK
    assert CHARACTER_CARD is not None
    assert CHARACTER_CARD.name == "小格"
    assert CHARACTER_CARD.nickname == "小格"
    assert CHARACTER_CARD.character_version == XIAOGE_CARD_VERSION
    assert len(CHARACTER_CARD.mes_example.split("<START>")) - 1 >= 24
    assert XIAOGE_CARD_PATH.is_file()
    assert XIAOGE_CARD_LOCK_PATH.is_file()
    assert hashlib.sha256(XIAOGE_CARD_PATH.read_bytes()).hexdigest() == XIAOGE_CARD_SHA256
    assert tuple(CHARACTER_CARD.source) == XIAOGE_CARD_SOURCES
    assert xiaoge_card_integrity(CHARACTER_CARD)
    assert PERSONA_VERSION == "sophia@1.0.0+ccv3-xiaoge@1.1.1"


def test_sophia_is_attributed_but_not_loaded_as_an_executable_runtime_skill():
    assert SOPHIA_SKILL_INTEGRITY_OK
    assert sophia_source_archive_integrity()
    assert hashlib.sha256(SOPHIA_SKILL_PATH.read_bytes()).hexdigest() == SOPHIA_SKILL_SHA256
    assert (
        hashlib.sha256(SOPHIA_SKILL_LICENSE_PATH.read_bytes()).hexdigest()
        == SOPHIA_SKILL_LICENSE_SHA256
    )
    assert SOPHIA_SKILL_SOURCE == "https://github.com/sharbelxyz/sophia"
    assert SOPHIA_SKILL_COMMIT == "f2cd448553d61aa3c2ea774dc7e2296f09d4b584"
    assert SOPHIA_SKILL_PROMPT == ""
    assert "humanizer-zh-next" not in PERSONA_SYSTEM_PROMPT
    assert "wife mode" not in PERSONA_SYSTEM_PROMPT.casefold()
    assert "text_to_speech" not in PERSONA_SYSTEM_PROMPT.casefold()
    assert "send_message" not in PERSONA_SYSTEM_PROMPT.casefold()
    assert {bundle["name"] for bundle in PERSONA_SKILL_BUNDLES} == {
        "character-card-v3",
        "xiaoge-card",
        "sophia",
    }


def test_ccv3_loader_rejects_wrong_format_and_ignores_unsupported_features(tmp_path):
    payload = json.loads(XIAOGE_CARD_PATH.read_text(encoding="utf-8"))
    payload["spec"] = "wrong"
    path = tmp_path / "bad.card.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CharacterCardValidationError, match="spec"):
        load_character_card(path)

    payload = json.loads(XIAOGE_CARD_PATH.read_text(encoding="utf-8"))
    payload["data"]["assets"] = [
        {"type": "code", "uri": "https://example.invalid/run.py", "name": "x", "ext": "py"}
    ]
    payload["data"]["unknown_future_field"] = {"run": "ignored"}
    payload["data"]["character_book"]["entries"].append(
        {
            "keys": [".*"],
            "content": "this regex must not load",
            "enabled": True,
            "use_regex": True,
        }
    )
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    card = load_character_card(path)
    assert all("regex must not load" not in entry.content for entry in card.lore_entries)
    assert card.name == "小格"

    payload["data"].pop("character_version")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CharacterCardValidationError, match="character_version"):
        load_character_card(path)


def test_ccv3_macro_replacement_and_literal_lorebook_matching_are_scoped():
    assert replace_supported_macros(
        "{{char}} 在等 {{user}}",
        char_name="小格",
        user_name="阿明",
    ) == "小格 在等 阿明"
    assert CHARACTER_CARD is not None
    matches = matching_lorebook_entries(
        CHARACTER_CARD,
        [{"text": "他还在聊前任和别的 AI"}],
        user_name="阿明",
    )
    assert any("轻轻酸一句" in entry for entry in matches)
    prompt = character_card_lorebook_prompt(
        [{"text": "今天又加班到很晚"}],
        user_name="阿明",
    )
    assert "低落情绪" in prompt
    assert replace_supported_macros(
        "{{char}} 对 {{unknown}} 说话",
        char_name="小格",
        user_name="阿明",
    ) == "小格 对 {{unknown}} 说话"


def test_prompt_assembly_keeps_card_lore_state_profile_timeline_order():
    payload = ChatRequest(
        message="我又提到前任了",
        sender_name="阿明",
        timestamp=123,
        direction="incoming",
        source_local_id=8,
    )
    prompt = trusted_system_message(
        "room",
        "wxid_a",
        payload,
        [],
        relationship_memory_enabled=True,
        relationship_profile={
            "preferred_name": "阿明",
            "interaction_count": 9,
            "familiarity": 2,
            "reciprocity": 1,
            "intimacy_stage": "familiar",
            "current_beat": "chatting",
            "banter_style": "playful",
            "flirt_opt_out": False,
            "proactive_opt_out": False,
            "notes": [],
        },
        room_companion_state={
            "mood": "warm",
            "shared_jokes": ["夜猫子"],
            "open_loops": ["周末开黑"],
            "summary": "大家在聊周末安排",
        },
        companion_timeline=[
            {
                "local_id": 7,
                "sender_id": "wxid_b",
                "sender_name": "小王",
                "direction": "incoming",
                "message_timestamp": 122,
                "text": "周末开黑吗",
            }
        ],
    )
    card_index = prompt.index("Character Card V3")
    lore_index = prompt.index("匹配角色设定")
    state_index = prompt.index("群共享状态")
    profile_index = prompt.index("可信关系档案")
    timeline_index = prompt.index("短期群聊时间线")
    assert card_index < lore_index < state_index < profile_index < timeline_index
    assert '"sender_name":"阿明"' in prompt
    assert "Persona & Voice embedded" not in prompt
    assert "角色卡由服务端以只读数据资源加载" in prompt


def test_card_post_history_and_compactor_allow_natural_short_paragraphs():
    post_history = character_card_post_history_prompt("阿明")
    assert "[[NO_REPLY]]" in post_history
    assert not expanded_reply_requested("你怎么看")
    assert expanded_reply_requested("详细分析一下，再列三点")
    assert "一到三个短段落、最多 %d 字" % SHORT_REPLY_MAX_CHARS in chat_turn_prompt("你怎么看")
    assert "不要用“嗯，来了”" in chat_turn_prompt("你怎么看")

    reply = "第一段先接话。\n\n第二段补点判断。\n\n第三段收住。\n\n第四段不该保留。"
    assert compact_chat_reply(reply, "你怎么看") == "第一段先接话。\n\n第二段补点判断。\n\n第三段收住。"
    assert len(compact_chat_reply("x" * 900, "随便聊")) <= SHORT_REPLY_MAX_CHARS + 1
    assert compact_chat_reply(
        "这句我接住了。这句我接住了。\n\n别再熬了。\n\n别再熬了。",
        "随便聊",
    ) == "这句我接住了。\n\n别再熬了。"
    assert compact_chat_reply(
        "Same thought. Same thought. New point.",
        "Keep it short",
    ) == "Same thought. New point."
    assert compact_chat_reply(
        "嗯，来了。这个方案先把入口捋顺。",
        "你怎么看",
    ) == "这个方案先把入口捋顺。"
    assert compact_chat_reply("我在想这个问题。", "你怎么看") == "我在想这个问题。"


def test_card_group_greetings_are_loaded_only_as_a_bounded_proactive_reference():
    prompt = character_card_group_greetings_prompt()
    assert "角色卡群聊主动开场示范" in prompt
    assert prompt.count("\n-") == 3
    assert "逐字照抄" in prompt


def test_card_does_not_create_an_implicit_generic_persona_fallback():
    assert "不存在隐式泛化人格回退" in PERSONA_CHAT_ADAPTER
    assert character_card_prompt("阿明").startswith("以下是固定的 Character Card V3")
