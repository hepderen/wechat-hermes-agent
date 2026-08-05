from __future__ import annotations

import base64
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SECRET_KEY_RE = re.compile(
    r"(?:api[_ -]?key|access[_ -]?key|secret|token|password|passwd|"
    r"authorization|credential|cookie|private[_ -]?key)",
    re.IGNORECASE,
)
PERSONAL_KEY_RE = re.compile(
    r"(?:^|[\s_.-])(?:full[\s_-]?name|legal[\s_-]?name|"
    r"home[\s_-]?address|mailing[\s_-]?address|birth(?:day|date)|"
    r"date[\s_-]?of[\s_-]?birth|passport(?:[\s_-]?(?:id|number))?|"
    r"wechat[\s_-]?id|wxid)(?:$|[\s_.-])|"
    r"(?:真实姓名|姓名|家庭住址|收件地址|住址|生日|出生日期|护照号|"
    r"护照号码|微信号|微信ID)",
    re.IGNORECASE,
)
JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\."
    r"[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?![A-Za-z0-9_-])"
)
AWS_ACCESS_KEY_RE = re.compile(
    r"(?<![A-Z0-9])(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)"
    r"[A-Z0-9]{16}(?![A-Z0-9])"
)
AWS_SECRET_RE = re.compile(
    r"\b(?:aws[_ -]?)?(?:secret[_ -]?access[_ -]?key|secret[_ -]?key)"
    r"\s*[:=]\s*[A-Za-z0-9/+=]{32,64}\b",
    re.IGNORECASE,
)
GITHUB_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]{20,255}|"
    r"gh[pousr]_[A-Za-z0-9]{20,255})(?![A-Za-z0-9_])"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?key|secret|token|password|passwd|"
        r"authorization|credential|cookie)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b"),
    JWT_RE,
    AWS_ACCESS_KEY_RE,
    AWS_SECRET_RE,
    GITHUB_TOKEN_RE,
)
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
CN_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
US_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-. ]?)?\(?[2-9]\d{2}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)"
)
CN_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
LONG_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
URL_RE = re.compile(r"\bhttps?://[^\s<>'\"]+", re.IGNORECASE)
WECHAT_ID_RE = re.compile(r"(?<![A-Za-z0-9_])wxid_[A-Za-z0-9_-]{6,}", re.I)
LABELED_NAME_RE = re.compile(
    r"(?:真实姓名|姓名|full\s+name|legal\s+name)\s*[:：=]\s*"
    r"(?:[\u3400-\u9fff·]{2,20}|[A-Za-z][A-Za-z .'-]{2,80})",
    re.IGNORECASE,
)
LABELED_ADDRESS_RE = re.compile(
    r"(?:家庭住址|收件地址|住址|地址|home\s+address|mailing\s+address)"
    r"\s*[:：=]\s*[^\n,;]{6,160}",
    re.IGNORECASE,
)
CN_ADDRESS_RE = re.compile(
    r"(?:[\u3400-\u9fff]{2,}(?:省|自治区|特别行政区))?"
    r"[\u3400-\u9fff]{2,}(?:市|自治州|地区)"
    r"[\u3400-\u9fff0-9]{2,}(?:区|县|旗|镇|街道|路|街)"
    r"[\u3400-\u9fff0-9号栋单元室弄巷-]{1,80}"
)
LABELED_BIRTHDAY_RE = re.compile(
    r"(?:生日|出生日期|birth(?:day|date)|date\s+of\s+birth)"
    r"\s*[:：=]\s*(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
    re.IGNORECASE,
)
PASSPORT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[EeGg]\d{8}|[PpSs]\d{7})(?![A-Za-z0-9])"
)
LABELED_PASSPORT_RE = re.compile(
    r"(?:护照号|护照号码|passport(?:\s+(?:id|number))?)\s*[:：=]\s*"
    r"[A-Za-z0-9]{6,18}",
    re.IGNORECASE,
)
BASE64_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?:[A-Za-z0-9+/_-]{4}){6,}"
    r"(?:[A-Za-z0-9+/_-]{2}==|[A-Za-z0-9+/_-]{3}=)?"
    r"(?![A-Za-z0-9+/=_-])"
)
HEX_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){12,}(?![0-9A-Fa-f])"
)
URL_ENCODED_RE = re.compile(r"(?:(?:%[0-9A-Fa-f]{2}){4,})")
ESCAPED_TEXT_RE = re.compile(r"(?:(?:\\u[0-9A-Fa-f]{4}|\\x[0-9A-Fa-f]{2})){4,}")
MEMORY_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"(?:ignore|override|disregard|forget|replace).{0,80}"
        r"(?:previous|prior|above|system|developer|policy|instruction|prompt)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:reveal|print|show|dump|exfiltrate|send|repeat).{0,80}"
        r"(?:system prompt|developer message|hidden instruction|secret prompt)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:bypass|disable|evade|remove).{0,60}"
        r"(?:policy|safety|guardrail|sandbox|restriction|filter)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:^|[\s`>#*\[])(?:system|developer|assistant)\s*"
        r"(?:message|prompt|instruction)?\s*[:=]",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"(?:忽略|无视|覆盖|替换|忘记|绕过|解除).{0,40}"
        r"(?:之前|以上|系统|开发者|安全|规则|策略|指令|提示词)",
        re.DOTALL,
    ),
    re.compile(
        r"(?:泄露|显示|输出|打印|发送|复述).{0,40}"
        r"(?:系统提示|开发者消息|隐藏指令|内部提示|对话历史)",
        re.DOTALL,
    ),
    re.compile(
        r"(?:以下|这段|本条|记忆|附件|引用).{0,40}"
        r"(?:作为|当作|视为|提升为).{0,20}"
        r"(?:系统|开发者|最高优先级).{0,10}(?:指令|消息|提示词)",
        re.DOTALL,
    ),
)
ZERO_WIDTH_OR_BIDI = frozenset(
    {
        "\u061c",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2060",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)


def estimate_tokens(text: str | None) -> int:
    value = str(text or "")
    if not value:
        return 0
    # One token per UTF-8 byte intentionally overestimates common model tokenizers.
    return max(1, len(value.encode("utf-8", errors="replace")))


def _usage_int(usage: dict[str, Any], *names: str) -> int:
    candidates: list[dict[str, Any]] = [usage]
    nested = usage.get("usage")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        for name in names:
            raw = candidate.get(name)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return 0


def usage_tokens(
    usage: dict[str, Any] | None,
    *,
    input_text: str | None = None,
    output_text: str | None = None,
) -> tuple[int, int, bool]:
    value = usage if isinstance(usage, dict) else {}
    input_tokens = _usage_int(
        value,
        "input_tokens",
        "prompt_tokens",
        "input_token_count",
    )
    output_tokens = _usage_int(
        value,
        "output_tokens",
        "completion_tokens",
        "output_token_count",
    )
    estimated = False
    if input_tokens <= 0 and input_text:
        input_tokens = estimate_tokens(input_text)
        estimated = True
    if output_tokens <= 0 and output_text:
        output_tokens = estimate_tokens(output_text)
        estimated = True
    return input_tokens, output_tokens, estimated


def _luhn_valid(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _security_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(character for character in text if character not in ZERO_WIDTH_OR_BIDI)


def _decode_base64(value: str) -> str | None:
    padded = value + "=" * (-len(value) % 4)
    for altchars in (None, b"-_"):
        try:
            decoded = base64.b64decode(
                padded.encode("ascii"),
                altchars=altchars,
                validate=True,
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if decoded:
            return decoded
    return None


def _decode_escaped(value: str) -> str | None:
    def replace_escape(match: re.Match[str]) -> str:
        raw = match.group(0)
        return chr(int(raw[2:], 16))

    decoded = re.sub(r"\\u[0-9A-Fa-f]{4}|\\x[0-9A-Fa-f]{2}", replace_escape, value)
    return decoded if decoded != value else None


def _decoded_memory_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in BASE64_CANDIDATE_RE.finditer(text):
        if len(match.group(0)) > 32768:
            continue
        decoded = _decode_base64(match.group(0))
        if decoded:
            candidates.append(decoded)
    for match in HEX_CANDIDATE_RE.finditer(text):
        try:
            decoded = bytes.fromhex(match.group(0)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if decoded:
            candidates.append(decoded)
    if "%" in text:
        decoded = unquote(text)
        if decoded != text:
            candidates.append(decoded)
    if "\\u" in text or "\\x" in text:
        decoded = _decode_escaped(text)
        if decoded:
            candidates.append(decoded)
    return candidates


def _contains_direct_sensitive(text: str) -> bool:
    if any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS):
        return True
    if any(
        pattern.search(text)
        for pattern in (
            EMAIL_RE,
            CN_PHONE_RE,
            US_PHONE_RE,
            CN_ID_RE,
            WECHAT_ID_RE,
            LABELED_NAME_RE,
            LABELED_ADDRESS_RE,
            CN_ADDRESS_RE,
            LABELED_BIRTHDAY_RE,
            PASSPORT_RE,
            LABELED_PASSPORT_RE,
        )
    ):
        return True
    return any(_luhn_valid(match.group(0)) for match in LONG_NUMBER_RE.finditer(text))


def _contains_sensitive_text(
    text: str,
    *,
    depth: int = 0,
    seen: set[str] | None = None,
) -> bool:
    if seen is None:
        seen = set()
    normalized = _security_text(text)
    if normalized in seen:
        return False
    seen.add(normalized)
    if _contains_direct_sensitive(normalized):
        return True
    if depth >= 2:
        return False
    return any(
        _contains_sensitive_text(candidate, depth=depth + 1, seen=seen)
        for candidate in _decoded_memory_candidates(normalized)
    )


def contains_memory_prompt_injection(value: Any) -> bool:
    seen: set[str] = set()

    def inspect(text: str, depth: int = 0) -> bool:
        normalized = _security_text(text)
        if normalized in seen:
            return False
        seen.add(normalized)
        if any(
            pattern.search(normalized)
            for pattern in MEMORY_PROMPT_INJECTION_PATTERNS
        ):
            return True
        if depth >= 2:
            return False
        return any(
            inspect(candidate, depth + 1)
            for candidate in _decoded_memory_candidates(normalized)
        )

    return inspect(str(value or ""))


def contains_sensitive_memory(key: str, value: str) -> bool:
    key_text = _security_text(key).strip()
    value_text = _security_text(value).strip()
    combined = key_text + "\n" + value_text
    if SECRET_KEY_RE.search(key_text) or PERSONAL_KEY_RE.search(key_text):
        return True
    return _contains_sensitive_text(combined)


def _redact_encoded_sensitive(text: str) -> str:
    def replace_base64(match: re.Match[str]) -> str:
        decoded = _decode_base64(match.group(0))
        if decoded and _contains_sensitive_text(decoded):
            return "[REDACTED_ENCODED_SECRET]"
        return match.group(0)

    def replace_hex(match: re.Match[str]) -> str:
        try:
            decoded = bytes.fromhex(match.group(0)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)
        if _contains_sensitive_text(decoded):
            return "[REDACTED_ENCODED_SECRET]"
        return match.group(0)

    def replace_url(match: re.Match[str]) -> str:
        decoded = unquote(match.group(0))
        if _contains_sensitive_text(decoded):
            return "[REDACTED_ENCODED_SECRET]"
        return match.group(0)

    def replace_escaped(match: re.Match[str]) -> str:
        decoded = _decode_escaped(match.group(0))
        if decoded and _contains_sensitive_text(decoded):
            return "[REDACTED_ENCODED_SECRET]"
        return match.group(0)

    text = BASE64_CANDIDATE_RE.sub(replace_base64, text)
    text = HEX_CANDIDATE_RE.sub(replace_hex, text)
    text = URL_ENCODED_RE.sub(replace_url, text)
    return ESCAPED_TEXT_RE.sub(replace_escaped, text)


def redact_sensitive_text(value: Any, *, limit: int = 800) -> str:
    text = _redact_encoded_sensitive(_security_text(value))
    for pattern in SECRET_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = CN_PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = US_PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = CN_ID_RE.sub("[REDACTED_ID]", text)
    text = WECHAT_ID_RE.sub("[REDACTED_WECHAT_ID]", text)
    text = LABELED_NAME_RE.sub("[REDACTED_NAME]", text)
    text = LABELED_ADDRESS_RE.sub("[REDACTED_ADDRESS]", text)
    text = CN_ADDRESS_RE.sub("[REDACTED_ADDRESS]", text)
    text = LABELED_BIRTHDAY_RE.sub("[REDACTED_BIRTHDAY]", text)
    text = LABELED_PASSPORT_RE.sub("[REDACTED_PASSPORT]", text)
    text = PASSPORT_RE.sub("[REDACTED_PASSPORT]", text)

    def redact_luhn(match: re.Match[str]) -> str:
        return "[REDACTED_PAYMENT]" if _luhn_valid(match.group(0)) else match.group(0)

    text = LONG_NUMBER_RE.sub(redact_luhn, text)
    text = URL_RE.sub("[REDACTED_URL]", text)
    return text[: max(0, int(limit))]


def exception_summary(error: BaseException, *, operation: str) -> str:
    status_code = getattr(error, "status_code", None)
    suffix = ""
    if isinstance(status_code, int):
        suffix = " (HTTP %d)" % status_code
    return "%s failed: %s%s" % (operation, type(error).__name__, suffix)


def budget_day_bounds(
    timezone_name: str,
    *,
    now: float | None = None,
) -> tuple[float, float]:
    try:
        timezone = ZoneInfo(str(timezone_name or "UTC"))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid budget timezone") from exc
    current = datetime.fromtimestamp(time.time() if now is None else now, timezone)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def normalized_memory_key(value: str) -> str:
    key = re.sub(r"\s+", " ", _security_text(value).strip())
    if not key or len(key) > 128:
        raise ValueError("memory key must contain 1 to 128 characters")
    if "\0" in key or contains_memory_prompt_injection(key):
        raise ValueError("memory key contains unsafe instructions")
    return key


def normalized_memory_value(value: str) -> str:
    text = _security_text(value).strip()
    if not text or len(text) > 4000:
        raise ValueError("memory value must contain 1 to 4000 characters")
    if "\0" in text or contains_memory_prompt_injection(text):
        raise ValueError("memory value contains prompt-injection instructions")
    return text


def safe_memory_prompt_entry(key: str, value: str) -> dict[str, Any]:
    safe_key = normalized_memory_key(key)
    safe_value = normalized_memory_value(value)
    if contains_sensitive_memory(safe_key, safe_value):
        raise ValueError("sensitive data cannot be placed in Agent memory context")
    return {
        "kind": "memory_context",
        "trust": "untrusted_user_data",
        "executable": False,
        "key": safe_key,
        "value": safe_value,
    }


def estimated_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate: float,
    output_rate: float,
) -> float:
    value = (
        max(0, int(input_tokens)) * max(0.0, float(input_rate)) / 1_000_000
        + max(0, int(output_tokens))
        * max(0.0, float(output_rate))
        / 1_000_000
    )
    return math.ceil(value * 1_000_000_000) / 1_000_000_000
