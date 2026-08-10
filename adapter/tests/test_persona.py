from app.main import (
    RESTRICTED_SESSION_SYSTEM_PROMPT,
    SESSION_SYSTEM_PROMPT,
    ChatRequest,
    trusted_system_message,
)
from app.persona import (
    EXPANDED_REPLY_MAX_CHARS,
    PERSONA_SYSTEM_PROMPT,
    PERSONA_TASK_PROMPT,
    PERSONA_TURN_PROMPT,
    PERSONA_VERSION,
    SHORT_REPLY_MAX_CHARS,
    chat_turn_prompt,
    compact_chat_reply,
    expanded_reply_requested,
)


def test_concise_persona_is_versioned_and_injected_into_all_chat_scopes():
    assert PERSONA_VERSION == "concise-old-friend-v3"
    assert PERSONA_SYSTEM_PROMPT in SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in RESTRICTED_SESSION_SYSTEM_PROMPT
    assert "脑子快、靠谱、肯干活" in PERSONA_SYSTEM_PROMPT
    assert "一句话说清就停" in PERSONA_SYSTEM_PROMPT
    assert "普通聊天通常一到两句" in PERSONA_SYSTEM_PROMPT
    assert len(PERSONA_SYSTEM_PROMPT) < 900


def test_concise_persona_keeps_roast_opt_in_and_controls_plain():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    for trigger in ("锐评", "吐槽", "开喷", "阴阳一下", "贴吧老哥模式"):
        assert trigger in PERSONA_SYSTEM_PROMPT
    for exit_signal in ("正常点", "认真点", "退出老哥模式"):
        assert exit_signal in PERSONA_SYSTEM_PROMPT
    for control in ("停止", "取消", "不要图片", "只要文字"):
        assert control in PERSONA_SYSTEM_PROMPT
    assert "只回一句，不玩梗、不争辩" in compact_prompt


def test_concise_persona_preserves_execution_truth():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    assert "缺少证据就直说尚未完成" in compact_prompt
    assert "能执行就执行" in PERSONA_SYSTEM_PROMPT
    assert "任务状态和工具证据必须照实" in PERSONA_TASK_PROMPT


def test_turn_prompt_assigns_a_short_budget_unless_expansion_is_explicit():
    assert not expanded_reply_requested("这个问题你怎么看")
    assert not expanded_reply_requested("这个方案行不行")
    assert expanded_reply_requested("详细分析一下，再列出执行步骤")
    assert expanded_reply_requested("给我做一套方案")
    assert expanded_reply_requested("分三点说")
    assert "最多两句、%d 字" % SHORT_REPLY_MAX_CHARS in chat_turn_prompt(
        "这个问题你怎么看"
    )
    assert "最多 %d 字" % EXPANDED_REPLY_MAX_CHARS in chat_turn_prompt(
        "详细分析一下"
    )
    assert "回答完就停" in PERSONA_TURN_PROMPT


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


def test_compactor_keeps_only_two_sentences_for_normal_group_chat():
    reply = "先修消息入口。它决定后面所有能力是否可靠。然后再加十个工具。"
    assert compact_chat_reply(reply, "现在先做什么") == (
        "先修消息入口。它决定后面所有能力是否可靠。"
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
    assert "最多两句、%d 字" % SHORT_REPLY_MAX_CHARS in sync_prompt
    assert "本轮是同步普通对话，服务端已禁用工具" in sync_prompt
    assert "不要计划、承诺或声称读取外部输入" in sync_prompt
