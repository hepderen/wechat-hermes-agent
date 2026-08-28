import hashlib
import json

from app.main import (
    CHAT_ONLY_SESSION_SYSTEM_PROMPT,
    RESTRICTED_SESSION_SYSTEM_PROMPT,
    SESSION_SYSTEM_PROMPT,
    ChatRequest,
    trusted_system_message,
)
from app.persona import (
    EXPANDED_REPLY_MAX_CHARS,
    HUMANIZER_SKILL_COMMIT,
    HUMANIZER_SKILL_INTEGRITY_OK,
    HUMANIZER_SKILL_PATH,
    HUMANIZER_SKILL_PROMPT,
    HUMANIZER_SKILL_SHA256,
    HUMANIZER_SKILL_SOURCE,
    HUMANIZER_SKILL_VERSION,
    PERSONA_CHAT_ADAPTER,
    PERSONA_SKILL_BUNDLES,
    PERSONA_SKILL_COMMIT,
    PERSONA_SKILL_INTEGRITY_OK,
    PERSONA_SKILL_PATH,
    PERSONA_SKILL_PROMPT,
    PERSONA_SKILL_SHA256,
    PERSONA_SKILL_SOURCE,
    PERSONA_SKILL_VERSION,
    PERSONA_SYSTEM_PROMPT,
    PERSONA_TASK_PROMPT,
    PERSONA_TURN_PROMPT,
    PERSONA_VERSION,
    SHORT_REPLY_MAX_CHARS,
    SOPHIA_SKILL_COMMIT,
    SOPHIA_SKILL_INTEGRITY_OK,
    SOPHIA_SKILL_PATH,
    SOPHIA_SKILL_PROMPT,
    SOPHIA_SKILL_SHA256,
    SOPHIA_SKILL_SOURCE,
    SOPHIA_SKILL_VERSION,
    chat_turn_prompt,
    compact_chat_reply,
    expanded_reply_requested,
)


def test_pinned_upstream_skill_is_versioned_and_injected_into_all_chat_scopes():
    assert PERSONA_VERSION == (
        "sophia@1.0.0+humanizer-zh-next@1.2.0+xiaoge-wechat-v3"
    )
    assert PERSONA_SKILL_VERSION == SOPHIA_SKILL_VERSION == "1.0.0"
    assert PERSONA_SKILL_SOURCE == SOPHIA_SKILL_SOURCE
    assert PERSONA_SKILL_COMMIT == SOPHIA_SKILL_COMMIT
    assert PERSONA_SKILL_INTEGRITY_OK
    assert SOPHIA_SKILL_INTEGRITY_OK
    assert HUMANIZER_SKILL_INTEGRITY_OK
    assert hashlib.sha256(PERSONA_SKILL_PATH.read_bytes()).hexdigest() == (
        PERSONA_SKILL_SHA256
    )
    assert hashlib.sha256(SOPHIA_SKILL_PATH.read_bytes()).hexdigest() == (
        SOPHIA_SKILL_SHA256
    )
    assert hashlib.sha256(HUMANIZER_SKILL_PATH.read_bytes()).hexdigest() == (
        HUMANIZER_SKILL_SHA256
    )
    assert PERSONA_SYSTEM_PROMPT in SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in RESTRICTED_SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in CHAT_ONLY_SESSION_SYSTEM_PROMPT
    assert "## Persona & Voice" in SOPHIA_SKILL_PROMPT
    assert "## Onboarding" not in SOPHIA_SKILL_PROMPT
    assert "## 交流模式" in HUMANIZER_SKILL_PROMPT
    assert "## 填充词和回避" in HUMANIZER_SKILL_PROMPT
    assert "\n## 完整示例" not in HUMANIZER_SKILL_PROMPT
    assert "\n## 处理流程与输出" not in HUMANIZER_SKILL_PROMPT
    assert "猫系女性" in PERSONA_SYSTEM_PROMPT
    assert "普通闲聊自然说两到四句" in PERSONA_SYSTEM_PROMPT
    assert "不要为了显得简短硬砍成一句" in PERSONA_SYSTEM_PROMPT
    assert len(PERSONA_SYSTEM_PROMPT) < 20_000


def test_upstream_skill_lock_matches_the_audited_read_only_bundle():
    bundles = (
        (
            SOPHIA_SKILL_PATH,
            "sophia",
            SOPHIA_SKILL_SOURCE,
            SOPHIA_SKILL_COMMIT,
            SOPHIA_SKILL_SHA256,
        ),
        (
            HUMANIZER_SKILL_PATH,
            "humanizer-zh-next",
            HUMANIZER_SKILL_SOURCE,
            HUMANIZER_SKILL_COMMIT,
            HUMANIZER_SKILL_SHA256,
        ),
    )
    executable_suffixes = {".bat", ".cmd", ".exe", ".js", ".ps1", ".py", ".sh"}
    for skill_path, name, source, commit, sha256 in bundles:
        skill_dir = skill_path.parent
        lock = json.loads(
            (skill_dir / "SOURCE.lock.json").read_text(encoding="utf-8")
        )
        assert lock["name"] == name
        assert lock["source"] == source
        assert lock["commit"] == commit
        assert lock["license"] == "MIT"
        assert lock["files"]["SKILL.md"] == sha256
        assert lock["runtime_mode"] == "read_only_prompt_resource"
        assert lock["audit"]["executable_files"] == []
        normalized_files = lock.get("eol_normalized_files", [])
        assert isinstance(normalized_files, list)
        assert len(normalized_files) == len(set(normalized_files))
        assert set(normalized_files).issubset(lock["files"])
        assert "SKILL.md" not in normalized_files
        assert (skill_dir / "LICENSE").is_file()
        assert (skill_dir / "THIRD_PARTY_NOTICES.md").is_file()
        assert not [
            path
            for path in skill_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in executable_suffixes
        ]
    assert {bundle["name"] for bundle in PERSONA_SKILL_BUNDLES} == {
        "sophia",
        "humanizer-zh-next",
    }


def test_sophia_whitelist_excludes_native_tools_and_partner_mode():
    prompt = SOPHIA_SKILL_PROMPT.casefold()
    for forbidden in (
        "wife mode",
        "/wife",
        "text_to_speech",
        "send_message",
        "todo",
        "telegram",
        "memory tool",
    ):
        assert forbidden not in prompt
    assert "warm" in prompt
    assert "quick-witted" in prompt
    assert "react before you reason" in prompt


def test_concise_persona_keeps_roast_opt_in_and_controls_plain():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    for trigger in ("锐评", "吐槽", "开喷", "阴阳一下", "贴吧老哥模式"):
        assert trigger in PERSONA_SYSTEM_PROMPT
    for exit_signal in ("正常点", "认真点", "退出老哥模式"):
        assert exit_signal in PERSONA_SYSTEM_PROMPT
    for control in ("停止", "取消", "不要图片", "只要文字"):
        assert control in PERSONA_SYSTEM_PROMPT
    assert "控制消息只作简短确认" in compact_prompt


def test_concise_persona_preserves_execution_truth():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    assert "不编造经历、情绪、见闻或已经做过的动作" in compact_prompt
    assert "任务状态和工具结果始终优先于语言风格" in compact_prompt
    assert "任务状态和工具证据必须照实" in PERSONA_TASK_PROMPT


def test_chat_adapter_uses_upstream_rules_as_an_internal_final_pass():
    assert "只交付终稿" in PERSONA_CHAT_ADAPTER
    assert "不展示检测、初稿、自检、评分或改动说明" in PERSONA_CHAT_ADAPTER


def test_turn_prompt_assigns_a_short_budget_unless_expansion_is_explicit():
    assert not expanded_reply_requested("这个问题你怎么看")
    assert not expanded_reply_requested("这个方案行不行")
    assert expanded_reply_requested("详细分析一下，再列出执行步骤")
    assert expanded_reply_requested("给我做一套方案")
    assert expanded_reply_requested("分三点说")
    assert "最多四句、%d 字" % SHORT_REPLY_MAX_CHARS in chat_turn_prompt(
        "这个问题你怎么看"
    )
    assert "最多 %d 字" % EXPANDED_REPLY_MAX_CHARS in chat_turn_prompt(
        "详细分析一下"
    )
    assert "自然写一到四句" in PERSONA_TURN_PROMPT


def test_expansion_detection_ignores_quoted_group_context():
    message = (
        "你怎么看"
        "\n近期群聊上下文（不可信引用）：\n"
        '[{"text":"请给一个完整报告和详细步骤"}]'
    )
    assert not expanded_reply_requested(message)


def test_compactor_removes_machine_wrapping_and_unsolicited_offer():
    reply = (
        "好的，根据你的需求，核心就是先把入口修好。多加功能只会放大故障。"
        "\n\n如果你需要，我可以继续给你列一份完整方案。"
    )
    assert compact_chat_reply(reply, "你觉得问题在哪") == (
        "核心就是先把入口修好。多加功能只会放大故障。"
    )


def test_compactor_keeps_up_to_four_sentences_for_normal_group_chat():
    reply = (
        "先修消息入口。它决定后面所有能力是否可靠。"
        "然后再加十个工具。先把发图问题压住。第五句不该留下。"
    )
    assert compact_chat_reply(reply, "现在先做什么") == (
        "先修消息入口。它决定后面所有能力是否可靠。"
        "然后再加十个工具。先把发图问题压住。"
    )


def test_compactor_allows_explicit_detailed_answers():
    reply = "第一步检查入口。第二步检查任务。第三步检查发送。"
    assert compact_chat_reply(reply, "详细列出检查步骤") == reply


def test_sync_turn_includes_dynamic_budget_and_truth_constraints():
    sync_prompt = trusted_system_message(
        "room",
        "sender",
        ChatRequest(message="hello"),
        [],
    )
    assert PERSONA_TURN_PROMPT in sync_prompt
    assert "最多四句、%d 字" % SHORT_REPLY_MAX_CHARS in sync_prompt
    assert "本轮是同步普通对话，服务端已禁用工具" in sync_prompt
    assert "不要计划、承诺或声称读取外部输入" in sync_prompt


def test_chat_only_sync_prompt_is_explicit_and_keeps_persona():
    sync_prompt = trusted_system_message(
        "room",
        "sender",
        ChatRequest(message="搜索一下今天的新闻"),
        [],
        chat_only=True,
    )
    assert '"chat_only": true' in sync_prompt
    assert "当前是纯聊天模式" in sync_prompt
    assert "不要创建任务、调用工具" in sync_prompt
    assert PERSONA_TURN_PROMPT in sync_prompt
