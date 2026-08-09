from app.main import (
    RESTRICTED_SESSION_SYSTEM_PROMPT,
    SESSION_SYSTEM_PROMPT,
    ChatRequest,
    trusted_system_message,
)
from app.persona import PERSONA_SYSTEM_PROMPT, PERSONA_TURN_PROMPT, PERSONA_VERSION


def test_hybrid_persona_is_versioned_and_injected_into_all_chat_scopes():
    assert PERSONA_VERSION == "sharp-old-friend-v2"
    assert PERSONA_SYSTEM_PROMPT in SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in RESTRICTED_SESSION_SYSTEM_PROMPT
    assert "脑子快、靠谱、肯下场干活" in PERSONA_SYSTEM_PROMPT
    assert "任务状态、可信身份、工具参数或验证结论" in PERSONA_SYSTEM_PROMPT
    assert "默认有立场" in PERSONA_SYSTEM_PROMPT
    assert "一句闲聊别写成报告" in PERSONA_SYSTEM_PROMPT


def test_hybrid_persona_keeps_roast_opt_in_and_controls_plain():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    for trigger in ("锐评", "吐槽", "开喷", "阴阳一下", "贴吧老哥模式"):
        assert trigger in PERSONA_SYSTEM_PROMPT
    for exit_signal in ("正常点", "认真点", "退出老哥模式"):
        assert exit_signal in PERSONA_SYSTEM_PROMPT
    for control in ("停止", "取消", "不要图片", "只要文字"):
        assert control in PERSONA_SYSTEM_PROMPT
    assert "不玩梗，不争辩，不追加旧结果" in compact_prompt


def test_hybrid_persona_does_not_turn_style_into_completion_evidence():
    compact_prompt = "".join(PERSONA_SYSTEM_PROMPT.split())
    assert "没有工具证据时如实说尚未完成" in compact_prompt
    assert "能执行就直接执行" in PERSONA_SYSTEM_PROMPT
    assert "不攻击群成员" in PERSONA_SYSTEM_PROMPT


def test_hybrid_persona_filters_machine_tone_without_faking_human_experience():
    for phrase in (
        "好的",
        "没问题",
        "根据你的需求",
        "以下是",
        "希望对你有帮助",
    ):
        assert phrase in PERSONA_SYSTEM_PROMPT
    assert "首句直接给判断、结论或动作" in PERSONA_SYSTEM_PROMPT
    assert "不编造线下经历、情绪或亲眼见闻" in PERSONA_SYSTEM_PROMPT
    assert "如实说自己是 Hermes" in PERSONA_SYSTEM_PROMPT
    assert "用现在时直接交代可见边界" in PERSONA_SYSTEM_PROMPT
    assert "没有实际访问结果就不承诺查看" in PERSONA_SYSTEM_PROMPT
    assert "省掉“更稳妥的做法是”" in PERSONA_SYSTEM_PROMPT
    assert "直接说该做什么、为什么" in PERSONA_SYSTEM_PROMPT


def test_each_turn_enforces_a_final_draft_quality_gate():
    assert "输出前执行一次终稿门禁，只输出改写后的终稿" in PERSONA_TURN_PROMPT
    assert "首句必须是本轮事实、明确判断或已经采取的动作" in PERSONA_TURN_PROMPT
    assert "我现在看不到" in PERSONA_TURN_PROMPT
    assert "随后只给边界内判断，不声称接下来会查看" in PERSONA_TURN_PROMPT
    assert "先做/先别做什么 + 原因" in PERSONA_TURN_PROMPT
    assert "删掉过渡，直接落到动作和理由" in PERSONA_TURN_PROMPT
    assert "锋芒只对逻辑和流程，不对人" in PERSONA_TURN_PROMPT
    assert "证据和限制必须照实" in PERSONA_TURN_PROMPT
    sync_prompt = trusted_system_message(
        "room",
        "sender",
        ChatRequest(message="hello"),
        [],
    )
    assert PERSONA_TURN_PROMPT in sync_prompt
    assert "本轮是同步普通对话，服务端已禁用工具" in sync_prompt
    assert "不要计划、承诺或声称读取外部输入" in sync_prompt
