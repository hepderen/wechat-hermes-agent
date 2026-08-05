#!/usr/bin/env python3
"""Exercise Adapter -> production Hermes search with an isolated fake Chat API."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


PROBE_MESSAGES = {
    "python": (
        "Research the official Python documentation. Use web_search and "
        "web_extract, cite at least two public source URLs, and return a "
        "concise Chinese result."
    ),
    "china": (
        "请研究腾讯云官方大模型文档和中国政府网最新人工智能政策。必须调用 "
        "web_search 和 web_extract，引用至少两个公开来源 URL，并用中文简要回答。"
    ),
    "twitter": (
        "上推特帮我搜一搜今天的 AI 热点新闻。必须调用 web_search 和 "
        "web_extract，引用至少两个公开来源 URL，并用中文简要回答。"
    ),
}


def load_fake_stack(adapter_root: Path):
    path = adapter_root / "scripts" / "live_fake_stack.py"
    spec = importlib.util.spec_from_file_location("adapter_search_fake_stack", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Adapter fake-stack helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gateway_proxy_handler(target: str, api_key: str):
    target = target.rstrip("/")

    class GatewayProxyHandler(BaseHTTPRequestHandler):
        server_version = "WechatHermesSearchProbe/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _json(self, status: int, value: Any) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _forward(self, method: str) -> None:
            body = self._body()
            if method == "POST" and urlsplit(self.path).path == "/v1/skills/reload":
                payload = json.loads(body.decode() or "{}")
                self._json(
                    200,
                    {
                        "object": "hermes.skills.reload",
                        "skills_root": str(payload.get("expected_skills_root") or ""),
                        "reloaded": False,
                        "count": 0,
                    },
                )
                return

            headers = {
                "Authorization": "Bearer " + api_key,
                "Accept": self.headers.get("Accept") or "application/json",
                "Content-Type": self.headers.get("Content-Type")
                or "application/json",
                "Accept-Encoding": "identity",
            }
            url = target + self.path
            timeout = httpx.Timeout(connect=10, read=300, write=30, pool=10)
            try:
                if urlsplit(self.path).path.endswith("/events"):
                    with httpx.Client(timeout=timeout, trust_env=False) as client:
                        with client.stream(
                            method,
                            url,
                            headers=headers,
                            content=body or None,
                        ) as response:
                            self.send_response(response.status_code)
                            self.send_header(
                                "Content-Type",
                                response.headers.get("content-type")
                                or "text/event-stream",
                            )
                            self.send_header("Connection", "close")
                            self.end_headers()
                            for chunk in response.iter_bytes():
                                self.wfile.write(chunk)
                                self.wfile.flush()
                    self.close_connection = True
                    return

                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    response = client.request(
                        method,
                        url,
                        headers=headers,
                        content=body or None,
                    )
                self.send_response(response.status_code)
                self.send_header(
                    "Content-Type",
                    response.headers.get("content-type") or "application/json",
                )
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            except (httpx.HTTPError, OSError) as exc:
                self._json(502, {"error": type(exc).__name__})

        def do_GET(self) -> None:
            self._forward("GET")

        def do_POST(self) -> None:
            self._forward("POST")

    return GatewayProxyHandler


def reset_fake_chat(fake: Any) -> None:
    with fake.STATE.lock:
        fake.STATE.sessions.clear()
        fake.STATE.runs.clear()
        fake.STATE.events.clear()
        fake.STATE.barriers.clear()
        fake.STATE.chat_calls.clear()
        fake.STATE.media_calls.clear()
        fake.STATE.next_run_id = 1
        fake.STATE.fail_next_image = False


def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.getenv(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError("production Hermes API key is unavailable")

    fake = load_fake_stack(args.adapter_root)
    reset_fake_chat(fake)
    proxy = fake.ServerThread(gateway_proxy_handler(args.hermes_url, api_key))
    chat = fake.ServerThread(fake.ChatHandler)
    proxy.start()
    chat.start()
    adapter_process = None
    adapter_log = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="wechat-hermes-search-probe-",
            ignore_cleanup_errors=True,
        ) as raw:
            temp = Path(raw)
            database = temp / "adapter.db"
            artifacts = temp / "artifacts"
            home = temp / "home"
            adapter_port = fake.free_port()
            adapter_url = "http://127.0.0.1:%d" % adapter_port
            fake.STATE.adapter_url = adapter_url
            fake.STATE.artifact_root = artifacts
            environment = fake.adapter_environment(
                database,
                artifacts,
                proxy.url,
                chat.url,
                adapter_port,
                home,
            )
            environment.update(
                {
                    "HERMES_WECHAT_MAX_TASK_SECONDS": str(args.timeout),
                    "HERMES_WECHAT_MAX_TASK_ATTEMPTS": str(
                        max(1, int(args.max_attempts))
                    ),
                    "HERMES_WECHAT_SYNC_TIMEOUT_SECONDS": "8",
                    "HERMES_WECHAT_SESSION_GENERATION": "search-probe-%d"
                    % int(time.time()),
                }
            )
            log_path = temp / "adapter.log"
            adapter_process, adapter_log = fake.start_adapter(
                args.adapter_root,
                environment,
                log_path,
            )
            with httpx.Client(timeout=15, trust_env=False) as client:
                fake.wait_ready(client, adapter_url, adapter_process)
                queued = fake.post_chat(
                    client,
                    adapter_url,
                    message=args.message,
                    local_id=910001,
                    request_id=args.request_id,
                )
                task_id = str(queued.get("task_id") or "")
                if queued.get("status") != "queued" or not task_id:
                    raise RuntimeError("Adapter did not queue the research task")
                task = fake.wait_task(
                    client,
                    adapter_url,
                    task_id,
                    {"succeeded", "failed", "canceled"},
                    timeout=float(args.timeout),
                )
                states = fake.wait_outbox_terminal(
                    database,
                    task_id,
                    timeout=60,
                )

            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                events = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT event_type, tool_name, source, exit_code
                        FROM tool_events
                        WHERE task_id=?
                        ORDER BY id
                        """,
                        (task_id,),
                    )
                ]
            completed = [
                item for item in events if item["event_type"] == "tool.completed"
            ]
            tools = [str(item.get("tool_name") or "") for item in completed]
            sources = []
            for item in completed:
                for value in str(item.get("source") or "").split(","):
                    parsed = urlsplit(value.strip())
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        sources.append(value.strip())

            with fake.STATE.lock:
                chat_calls = [
                    item
                    for item in fake.STATE.chat_calls
                    if item.get("task_id") == task_id
                ]
                media_count = len(fake.STATE.media_calls)

            if task.get("status") != "succeeded":
                raise RuntimeError(
                    "research task failed verifier: %s" % str(task.get("error") or "")
                )
            if "web_search" not in tools or "web_extract" not in tools:
                raise RuntimeError("research task lacked search and extraction evidence")
            if len(set(sources)) < 2:
                raise RuntimeError("research task recorded fewer than two source URLs")
            if ("text", "confirmed") not in states:
                raise RuntimeError("fake Chat API did not confirm the final text item")
            if len(chat_calls) != 1 or media_count != 0:
                raise RuntimeError("isolated delivery count was not exactly one text item")

            return {
                "ok": True,
                "task_id": task_id,
                "run_id": str(task.get("hermes_run_id") or ""),
                "status": task.get("status"),
                "completed_tools": tools,
                "source_hosts": sorted(
                    {urlsplit(value).hostname for value in sources}
                ),
                "outbox": states,
                "fake_text_deliveries": len(chat_calls),
                "fake_media_deliveries": media_count,
            }
    except Exception:
        if "log_path" in locals() and log_path.exists():
            sys.stderr.write(log_path.read_text(encoding="utf-8", errors="replace"))
        raise
    finally:
        if adapter_process is not None and adapter_log is not None:
            fake.stop_adapter(adapter_process, adapter_log)
        chat.close()
        proxy.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter-root", type=Path, required=True)
    parser.add_argument("--hermes-url", default="http://127.0.0.1:8642")
    parser.add_argument("--api-key-env", default="API_SERVER_KEY")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--profile", choices=sorted(PROBE_MESSAGES), default="python")
    parser.add_argument(
        "--message",
        default=None,
    )
    parser.add_argument(
        "--request-id",
        default="isolated-production-search-probe",
    )
    args = parser.parse_args()
    args.message = args.message or PROBE_MESSAGES[args.profile]
    args.adapter_root = args.adapter_root.resolve()
    print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
