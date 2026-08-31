from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, patch


def test_persona_probe_uses_at_least_24_non_delivery_weirdotv_scenarios():
    script = Path(__file__).resolve().parents[1] / "scripts" / "probe_persona_cloud.py"
    values = runpy.run_path(str(script))
    cases = values["CASES"]

    assert len(cases) >= 24
    assert all(
        {"scenario", "sender_id", "sender_name", "message"}.issubset(case)
        for case in cases
    )
    assert len({case["scenario"] for case in cases}) == len(cases)
    assert "diagnostic_session_id" in script.read_text(encoding="utf-8")
    assert "diagnostic-no-delivery" in script.read_text(encoding="utf-8")
    source = script.read_text(encoding="utf-8")
    assert "weirdo-tv-sunxiaochuan" in source
    assert 'set(skills) != {"weirdo-tv-sunxiaochuan"}' in source
    assert "diagnostic response gave reply advice" in source
    assert "diagnostic response repeated persona catchphrases inside cooldown" in source
    advice = values["REPLY_ADVICE_RE"]
    assert advice.search("你可以这样回一句")
    assert advice.search("可以接：这局我站你")
    assert not advice.search("都在潜水呢，等你先冒个泡。")

    recent = values["recent_persona_catchphrases"]
    assert recent(
        [
            {"direction": "outgoing", "text": "啊对对对，这句有点东西。"},
            {"direction": "incoming", "text": "你又来了。"},
            {"direction": "outgoing", "text": "蚌埠住了。"},
        ]
    ) == {"啊对对对", "蚌埠住了"}


def test_persona_probe_retries_only_transient_model_failures():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "probe_persona_cloud.py"
    )
    values = runpy.run_path(str(script))
    retry = values["retry_diagnostic_case"]
    transient = values["TransientPersonaProbeError"]
    calls = 0

    async def operation(attempt):
        nonlocal calls
        calls += 1
        if attempt < 3:
            raise transient("provider is temporarily unavailable")
        return {"status": "succeeded"}

    with patch.object(values["asyncio"], "sleep", new_callable=AsyncMock) as sleep:
        payload, used_attempt = asyncio.run(
            retry(
                "persona",
                operation,
                attempts=3,
                initial_delay_seconds=2,
            )
        )

    assert payload == {"status": "succeeded"}
    assert calls == 3
    assert used_attempt == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]
