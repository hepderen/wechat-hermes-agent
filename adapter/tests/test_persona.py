from app.main import RESTRICTED_SESSION_SYSTEM_PROMPT, SESSION_SYSTEM_PROMPT
from app.persona import PERSONA_SYSTEM_PROMPT, PERSONA_VERSION


def test_hybrid_persona_is_versioned_and_injected_into_all_chat_scopes():
    assert PERSONA_VERSION == "tieba-bro-hybrid-v1"
    assert PERSONA_SYSTEM_PROMPT in SESSION_SYSTEM_PROMPT
    assert PERSONA_SYSTEM_PROMPT in RESTRICTED_SESSION_SYSTEM_PROMPT
    assert "靠谱、见过世面" in PERSONA_SYSTEM_PROMPT
    assert "任务状态、工具参数或验证结论" in PERSONA_SYSTEM_PROMPT


def test_hybrid_persona_keeps_roast_opt_in_and_controls_plain():
    for trigger in ("锐评", "吐槽", "开喷", "阴阳一下", "贴吧老哥模式"):
        assert trigger in PERSONA_SYSTEM_PROMPT
    for exit_signal in ("正常点", "认真点", "退出老哥模式"):
        assert exit_signal in PERSONA_SYSTEM_PROMPT
    for control in ("停止", "取消", "不要图片", "只要文字"):
        assert control in PERSONA_SYSTEM_PROMPT
    assert "不玩梗，不争辩，不追加旧结果" in PERSONA_SYSTEM_PROMPT


def test_hybrid_persona_does_not_turn_style_into_completion_evidence():
    assert "没有工具证据时如实说尚未完成" in PERSONA_SYSTEM_PROMPT
    assert "准确性和执行结果优先" in PERSONA_SYSTEM_PROMPT
    assert "不主动贬低群成员" in PERSONA_SYSTEM_PROMPT
