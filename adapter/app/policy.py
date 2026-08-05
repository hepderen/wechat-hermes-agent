from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .intents import is_conceptual_question, is_explicit_research_request


EXECUTION_RE = re.compile(
    r"(?:"
    r"搜一搜|搜搜|搜一下|搜下|再搜一次|查一查|查查|查一下|查下|"
    r"找一找|找找|找一下|找下|"
    r"(?:帮我|替我|给我)(?:搜|查|找)|"
    r"制作|生成|创建|导出|视频|脚本|分镜|字幕|素材|封面|图片|"
    r"调研|研究|检索|搜索|浏览器|网页|下载|文件|表格|文档|幻灯片|"
    r"执行|运行|终端|命令|部署|安装|skill|mcp|定时|提醒|发送|"
    r"记住|忘记|记忆|偏好|"
    r"\b(?:create|generate|export|video|script|storyboard|subtitle|image|"
    r"research|search|look\s+up|find|browse|browser|download|file|document|spreadsheet|"
    r"slides?|execute|run|terminal|command|deploy|install|schedule|remind|"
    r"send|remember|forget|memory|preference|skill|mcp)\b"
    r")",
    re.IGNORECASE,
)
TASK_COMMAND_RE = re.compile(
    r"^\s*(任务|取消|重试)(?:\s+(T-[A-Fa-f0-9]{8}))?\s*[。.!！]?\s*$"
)
REVISION_COMMAND_RE = re.compile(
    r"^\s*(补充|修改)\s+(T-[A-Fa-f0-9]{8})\s+(.+?)\s*$",
    re.DOTALL,
)
MEDIA_ONLY_STOP_RE = re.compile(
    r"^\s*(?:"
    r"不要图片|不要发图片|不要再发图片|"
    r"别发图|别再发图|别发图片|别再发图片|"
    r"停止发图|停止发送图片|只要文字"
    r")\s*[。.!！]?\s*$"
)
STOP_ALL_RE = re.compile(
    r"^\s*(?:"
    r"停|停止|停下来|停一下|"
    r"别发了|别再发了|不要发了|不要再发了|"
    r"停止发送|全部取消"
    r")\s*[。.!！]?\s*$"
)


@dataclass(frozen=True)
class TaskCommand:
    action: str
    task_id: str | None
    content: str = ""


def parse_task_command(message: str) -> TaskCommand | None:
    value = str(message or "")
    if MEDIA_ONLY_STOP_RE.fullmatch(value):
        return TaskCommand("media_only", None)
    if STOP_ALL_RE.fullmatch(value):
        return TaskCommand("cancel_all", None)
    revision = REVISION_COMMAND_RE.fullmatch(value)
    if revision:
        action = "supplement" if revision.group(1) == "补充" else "modify"
        return TaskCommand(
            action,
            revision.group(2).upper(),
            revision.group(3).strip(),
        )
    match = TASK_COMMAND_RE.fullmatch(value)
    if not match:
        return None
    action = {"任务": "status", "取消": "cancel", "重试": "retry"}[match.group(1)]
    task_id = match.group(2).upper() if match.group(2) else None
    if action == "cancel" and task_id is None:
        action = "cancel_all"
    return TaskCommand(action, task_id)


def should_run_async(
    message: str,
    message_type: str,
    attachments: list[dict[str, Any]],
) -> bool:
    if attachments or message_type not in {"", "text"}:
        return True
    text = str(message or "")
    if is_explicit_research_request(text):
        return True
    if is_conceptual_question(text):
        return False
    return bool(EXECUTION_RE.search(text))


def stable_session_id(
    room_id: str | None,
    sender_id: str,
    generation: str = "1",
) -> str:
    scope = "room:" + room_id if room_id else "private:" + sender_id
    version = str(generation or "").strip() or "1"
    value = "generation:%s\n%s" % (version, scope)
    return "wechat:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def stable_diagnostic_session_id(room_id: str, diagnostic_id: str) -> str:
    value = "room:%s\ndiagnostic:%s" % (room_id, diagnostic_id.strip())
    return (
        "wechat-diagnostic:"
        + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    )


def request_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def format_task(task: dict[str, Any]) -> str:
    if task.get("internal_state") == "blocked_on_input":
        label = "等待补充信息"
    else:
        labels = {
            "queued": "排队中",
            "running": "执行中",
            "succeeded": "已成功",
            "failed": "失败",
            "canceled": "已取消",
        }
        label = labels.get(task["status"], task["status"])
    text = "任务 %s：%s" % (task["id"], label)
    if task.get("error") and task["status"] == "failed":
        text += "\n原因：" + str(task["error"])[:300]
    return text


def format_task_list(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "这个群目前没有任务记录。"
    lines = ["最近任务："]
    for task in tasks:
        lines.append(format_task(task).splitlines()[0])
    return "\n".join(lines)
