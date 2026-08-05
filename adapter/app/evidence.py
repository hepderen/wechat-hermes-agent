from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .intents import is_conceptual_question, is_explicit_research_request
from .security import redact_sensitive_text


RESEARCH_RE = re.compile(
    r"(调研|研究|检索|搜索|搜一搜|搜搜|搜一下|搜下|再搜一次|"
    r"查资料|查一查|查查|查一下|查下|找一找|找找|找一下|找下|"
    r"(?:帮我|替我|给我)(?:搜|查|找)|来源|引用|"
    r"research|search|look\s+up|find|sources?|citations?)",
    re.IGNORECASE,
)
BROWSER_RE = re.compile(
    r"(浏览器|网页|网站|页面|点击|登录|browser|playwright|webpage|website)",
    re.IGNORECASE,
)
COMMAND_RE = re.compile(
    r"(执行|运行|命令|终端|部署|安装|服务|shell|command|terminal|deploy|install)",
    re.IGNORECASE,
)
IMAGE_RE = re.compile(
    r"(图片|图像|配图|封面|海报|截图|image|photo|poster|cover)",
    re.IGNORECASE,
)
VIDEO_RE = re.compile(r"(视频|短片|video|mp4)", re.IGNORECASE)
FILE_OUTPUT_RE = re.compile(
    r"(?:生成|创建|制作|编写|导出|下载|保存|打包).{0,30}"
    r"(?:文件|文档|表格|报告|压缩包|pdf|docx|xlsx|csv|zip)"
    r"|(?:文件|文档|表格|报告|压缩包|pdf|docx|xlsx|csv|zip).{0,20}"
    r"(?:生成|创建|制作|导出|下载|保存|打包)"
    r"|(?:导出|下载|打包)(?:结果|内容|数据)?"
    r"|\b(?:create|generate|produce|export|download|save|package|write)\b"
    r".{0,80}\b(?:file|document|report|spreadsheet|workbook|presentation|"
    r"pdf|docx|xlsx|csv|zip)\b"
    r"|\b(?:export|download|package)\b",
    re.IGNORECASE,
)
PURE_TEXT_RE = re.compile(
    r"(改写|润色|翻译|起草|文案|纯文本|写一段|写文章|写脚本|"
    r"想法|标题|大纲|方案|建议|回答|总结|摘要|列表|名字|提示词|"
    r"rewrite|translate|draft|copywriting|text only|ideas?|outline|summary)",
    re.IGNORECASE,
)
EXTERNAL_ACTION_RE = re.compile(
    r"(发送|上传|发布|提交|删除|移动|重命名|配置|设置|定时|提醒|"
    r"备份|恢复|同步|调用|抓取|购买|下单|注册|登录|"
    r"send|upload|publish|submit|delete|rename|configure|schedule|"
    r"backup|restore|sync|purchase|sign\s*in)",
    re.IGNORECASE,
)
BLOCKED_RE = re.compile(
    r"^\s*(?:需要|请提供|请补充|还缺|无法继续|need\b|please provide\b|missing\b)",
    re.IGNORECASE,
)
COMMAND_TOOLS = frozenset({"terminal", "execute_code"})
RESEARCH_TOOLS = frozenset({"web_search", "web_extract", "x_search"})
BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_press",
        "browser_get_images",
        "browser_vision",
        "browser_console",
        "browser_cdp",
        "browser_dialog",
    }
)
RESEARCH_MAX_TOOL_CALLS = 24


def _attachment_types(attachments: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for item in attachments:
        item_type = str(item.get("type") or "").strip().lower()
        mime_type = str(item.get("mime_type") or "").strip().lower()
        if item_type:
            values.add(item_type)
        if mime_type:
            values.add(mime_type.split("/", 1)[0])
    return values


def build_execution_plan(
    message: str,
    message_type: str = "text",
    attachments: list[dict[str, Any]] | None = None,
    *,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    text = str(message or "").strip()
    attachments = list(attachments or [])
    attachment_types = _attachment_types(attachments)
    expected_artifacts: list[str] = []
    conceptual = is_conceptual_question(text)

    if not conceptual and VIDEO_RE.search(text):
        expected_artifacts.append("video")
    if not conceptual and IMAGE_RE.search(text):
        expected_artifacts.append("image")
    if not conceptual and FILE_OUTPUT_RE.search(text):
        expected_artifacts.append("file")
    expected_artifacts = list(dict.fromkeys(expected_artifacts))

    capabilities: list[str] = []
    if is_explicit_research_request(text) or (
        not conceptual and RESEARCH_RE.search(text)
    ):
        capabilities.append("research")
    if not conceptual and BROWSER_RE.search(text):
        capabilities.append("browser")
    if not conceptual and COMMAND_RE.search(text):
        capabilities.append("command")
    if expected_artifacts or attachments or message_type not in {"", "text"}:
        capabilities.append("file")
    if not conceptual and EXTERNAL_ACTION_RE.search(text) and not capabilities:
        capabilities.append("external")

    if len(capabilities) > 1:
        task_type = "compound"
    elif capabilities:
        task_type = capabilities[0]
    elif PURE_TEXT_RE.search(text):
        task_type = "text_creation"
    else:
        task_type = "general"

    tool_for_capability = {
        "research": "research",
        "browser": "browser",
        "command": "terminal",
        "file": "files",
        "external": "external",
    }
    required_tools = [tool_for_capability[item] for item in capabilities]
    requires_tool_evidence = bool(capabilities)
    delivery_policy = (
        "requested_artifacts" if expected_artifacts else "text_only"
    )
    success_conditions = ["non_empty_result"]
    if "research" in capabilities:
        success_conditions.append("source_recorded")
    if "browser" in capabilities:
        success_conditions.append("browser_action_recorded")
    if "command" in capabilities:
        success_conditions.append("zero_exit_code")
    if "file" in capabilities:
        success_conditions.append("verified_artifact")
    if "external" in capabilities:
        success_conditions.append("external_tool_recorded")

    return {
        "goal": text[:2000],
        "task_type": task_type,
        "capabilities": capabilities,
        "required_tools": required_tools,
        "expected_artifacts": expected_artifacts,
        "input_attachment_types": sorted(attachment_types),
        "success_conditions": success_conditions,
        "requires_tool_evidence": requires_tool_evidence,
        "max_tool_calls": (
            RESEARCH_MAX_TOOL_CALLS if "research" in capabilities else None
        ),
        "timeout_seconds": max(1, int(timeout_seconds)),
        "delivery_policy": delivery_policy,
    }


def effective_tool_call_limit(
    plan: dict[str, Any],
    global_limit: int,
) -> int:
    hard_limit = max(1, int(global_limit))
    try:
        plan_limit = int(plan.get("max_tool_calls") or 0)
    except (TypeError, ValueError):
        return hard_limit
    if plan_limit <= 0:
        return hard_limit
    return min(hard_limit, plan_limit)


def _nested(event: dict[str, Any], *keys: str) -> Any:
    current: Any = event
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _safe_source(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    safe_parts: list[str] = []
    for part in raw.split(",")[:10]:
        candidate = part.strip()
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = ":" + str(parsed.port) if parsed.port else ""
            safe_parts.append(
                urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        parsed.hostname + port,
                        parsed.path,
                        "",
                        "",
                    )
                )
            )
        else:
            safe_parts.append(redact_sensitive_text(candidate, limit=200))
    return ",".join(safe_parts)[:500]


def normalize_run_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("event") or event.get("type") or "").strip()
    if event_type not in {
        "tool.started",
        "tool.completed",
        "tool.failed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.canceled",
    }:
        return None

    tool_name = str(
        event.get("tool")
        or event.get("tool_name")
        or _nested(event, "data", "tool")
        or _nested(event, "data", "name")
        or ""
    )[:128]
    exit_code = (
        event.get("exit_code")
        if isinstance(event.get("exit_code"), int)
        else _nested(event, "result", "exit_code")
    )
    if not isinstance(exit_code, int):
        exit_code = _nested(event, "data", "exit_code")
    if not isinstance(exit_code, int):
        exit_code = None

    result_value = (
        event.get("result_summary")
        or event.get("summary")
        or event.get("error")
        or event.get("result")
        or _nested(event, "data", "summary")
        or _nested(event, "data", "error")
        or ""
    )
    if isinstance(result_value, (dict, list)):
        result_value = "structured result"
    result_summary = redact_sensitive_text(str(result_value), limit=500)

    source_value = (
        event.get("source")
        or event.get("url")
        or _nested(event, "result", "source")
        or _nested(event, "result", "url")
        or _nested(event, "data", "source")
        or ""
    )
    if not source_value:
        sources = event.get("sources") or _nested(event, "result", "sources")
        if isinstance(sources, list):
            source_value = ",".join(
                str(item.get("url") if isinstance(item, dict) else item)
                for item in sources[:10]
            )
    source = _safe_source(source_value)
    artifact_id = str(
        event.get("artifact_id")
        or _nested(event, "result", "artifact_id")
        or _nested(event, "data", "artifact_id")
        or ""
    )[:64]
    return {
        "event_type": event_type,
        "tool_name": tool_name,
        "exit_code": exit_code,
        "result_summary": result_summary,
        "source": source,
        "artifact_id": artifact_id,
    }


def _artifact_kind(artifact: dict[str, Any]) -> str:
    mime_type = str(artifact.get("mime_type") or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "file"


def _tool_matches(event: dict[str, Any], allowed: frozenset[str]) -> bool:
    return str(event.get("tool_name") or "").strip().lower() in allowed


def looks_blocked_on_input(output: str) -> bool:
    text = str(output or "").strip()
    return bool(text and len(text) <= 800 and BLOCKED_RE.search(text))


def verify_completion(
    plan: dict[str, Any],
    tool_events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    output: str,
    run_status: str = "completed",
) -> dict[str, Any]:
    raw_status = str(run_status or "").lower()
    if raw_status in {"cancelled", "canceled"}:
        return {"status": "canceled", "reason": "Hermes run was canceled"}
    if raw_status == "failed":
        return {"status": "failed", "reason": "Hermes run failed"}
    if bool(plan.get("requires_tool_evidence")) and looks_blocked_on_input(output):
        return {
            "status": "blocked_on_input",
            "reason": str(output).strip()[:800],
        }

    verified_artifacts = [
        item
        for item in artifacts
        if bool(item.get("verified"))
        and int(item.get("size_bytes") or 0) > 0
        and bool(item.get("sha256"))
    ]
    completed = [
        event
        for event in tool_events
        if event.get("event_type") == "tool.completed"
    ]
    failed = [
        event
        for event in tool_events
        if event.get("event_type") == "tool.failed"
    ]
    success_conditions = set(plan.get("success_conditions") or [])

    if not bool(plan.get("requires_tool_evidence")):
        if str(output or "").strip():
            return {
                "status": "succeeded",
                "reason": "text result completed",
                "evidence_count": len(completed),
                "artifact_count": len(verified_artifacts),
            }
        return {"status": "failed", "reason": "Hermes returned an empty result"}

    if not str(output or "").strip():
        return {
            "status": "failed",
            "reason": "execution completed without a final result",
        }

    if "zero_exit_code" in success_conditions:
        success = any(
            _tool_matches(event, COMMAND_TOOLS)
            and event.get("exit_code") == 0
            for event in completed
        )
        if not success:
            return {
                "status": "failed",
                "reason": "no successful command evidence with exit code 0",
            }
    if "source_recorded" in success_conditions:
        success = any(
            _tool_matches(event, RESEARCH_TOOLS)
            and str(event.get("source") or "").strip().startswith(
                ("http://", "https://")
            )
            for event in completed
        )
        if not success:
            return {
                "status": "failed",
                "reason": "research completed without recorded sources",
            }
    if "browser_action_recorded" in success_conditions:
        success = any(
            _tool_matches(event, BROWSER_TOOLS)
            and bool(
                str(event.get("result_summary") or "").strip()
                or str(event.get("source") or "").strip()
            )
            for event in completed
        )
        if not success:
            return {
                "status": "failed",
                "reason": "no successful browser action evidence",
            }
    if "verified_artifact" in success_conditions:
        expected = set(plan.get("expected_artifacts") or [])
        kinds = {_artifact_kind(item) for item in verified_artifacts}
        if not verified_artifacts:
            return {
                "status": "failed",
                "reason": "file task completed without a verified artifact",
            }
        missing = expected - kinds
        if missing:
            return {
                "status": "failed",
                "reason": (
                    "verified artifacts are missing requested types: "
                    + ",".join(sorted(missing))
                ),
            }
    if "external_tool_recorded" in success_conditions and not completed:
        return {
            "status": "failed",
            "reason": "external task completed without successful tool evidence",
        }

    return {
        "status": "succeeded",
        "reason": "required execution evidence verified",
        "evidence_count": len(completed),
        "failed_evidence_count": len(failed),
        "artifact_count": len(verified_artifacts),
    }
