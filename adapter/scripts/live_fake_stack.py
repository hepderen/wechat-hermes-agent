from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


ROOM_ID = "fake-production@chatroom"
BRIDGE_TOKEN = "fake-bridge-token"
INTERNAL_TOKEN = "fake-internal-token"
HERMES_TOKEN = "fake-hermes-token"
CHAT_API_TOKEN = "fake-chat-api-token"
TASK_ID_RE = re.compile(r"T-[A-F0-9]{8}")
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01"
    b"\x89\x99=\x1d"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: set[str] = set()
        self.runs: dict[str, dict[str, Any]] = {}
        self.next_run_id = 1
        self.adapter_url = ""
        self.artifact_root = Path()
        self.events: list[dict[str, Any]] = []
        self.barriers: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.media_calls: list[dict[str, Any]] = []
        self.delivery_states: dict[str, dict[str, Any]] = {}
        self.delivery_checks: list[str] = []
        self.drop_next_text_response = False
        self.fail_next_image = True

    def record(self, event: str, **fields: Any) -> None:
        with self.lock:
            self.events.append(
                {"event": event, "time_ns": time.monotonic_ns(), **fields}
            )


STATE = FakeState()


def foreground_hermes_chat_count() -> int:
    """Keep companion-maintenance calls out of foreground routing assertions."""
    summary_prefixes = (
        "wechat-companion-summary:",
        "wechat-relationship-summary:",
    )
    with STATE.lock:
        return sum(
            item["event"] == "hermes.chat"
            and not str(item.get("session_id") or "").startswith(summary_prefixes)
            for item in STATE.events
        )


class JsonHandler(BaseHTTPRequestHandler):
    server_version = "WechatHermesFake/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, status: int, value: Any) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def register_fake_image(task_id: str) -> None:
    task_root = STATE.artifact_root / task_id
    task_root.mkdir(parents=True, exist_ok=True)
    path = task_root / "fake-result.png"
    path.write_bytes(PNG_BYTES)
    request = urllib.request.Request(
        STATE.adapter_url + "/internal/artifacts/register",
        data=json_bytes(
            {
                "task_id": task_id,
                "path": str(path),
                "role": "primary",
            }
        ),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": INTERNAL_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(
                    "artifact registration returned HTTP %d" % response.status
                )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "artifact registration returned HTTP %d: %s"
            % (exc.code, detail)
        ) from exc


class HermesHandler(JsonHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self.send_json(200, {"status": "ok"})
            return
        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            self.send_json(200, {"data": []})
            return
        if path.startswith("/api/sessions/"):
            session_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            with STATE.lock:
                exists = session_id in STATE.sessions
            self.send_json(200 if exists else 404, {"id": session_id})
            return
        if path.startswith("/v1/runs/") and path.endswith("/events"):
            run_id = urllib.parse.unquote(path.split("/")[-2])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                with STATE.lock:
                    run = dict(STATE.runs.get(run_id) or {})
                status = str(run.get("status") or "failed")
                if status == "running":
                    time.sleep(0.02)
                    continue
                for event in run.get("events") or []:
                    self.wfile.write(b"data: " + json_bytes(event) + b"\n\n")
                terminal = {
                    "completed": "run.completed",
                    "failed": "run.failed",
                    "canceled": "run.canceled",
                }.get(status, "run.failed")
                self.wfile.write(
                    b"data: "
                    + json_bytes({"event": terminal, "run_id": run_id})
                    + b"\n\n"
                )
                self.wfile.flush()
                return
        if path.startswith("/v1/runs/"):
            run_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            with STATE.lock:
                run = dict(STATE.runs.get(run_id) or {})
            if not run:
                self.send_json(404, {"detail": "not found"})
                return
            self.send_json(
                200,
                {
                    "run_id": run_id,
                    "status": run["status"],
                    "output": run.get("output") or "",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
            return
        self.send_json(404, {"detail": "not found"})

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/sessions/"):
            session_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            with STATE.lock:
                STATE.sessions.discard(session_id)
            self.send_response(204)
            self.end_headers()
            return
        self.send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        payload = self.read_json()
        if path == "/api/sessions":
            session_id = str(payload.get("id") or "")
            with STATE.lock:
                STATE.sessions.add(session_id)
            self.send_json(201, {"id": session_id})
            return
        if path.startswith("/api/sessions/") and path.endswith("/chat"):
            session_id = urllib.parse.unquote(
                path[len("/api/sessions/") : -len("/chat")].strip("/")
            )
            STATE.record(
                "hermes.chat",
                session_id=session_id,
                message=str(payload.get("message") or ""),
            )
            if session_id.startswith("wechat-companion-summary:"):
                content = json.dumps(
                    {
                        "mood": "playful",
                        "shared_jokes": ["假栈群梗"],
                        "open_loops": ["继续验证群聊上下文"],
                        "summary": "假栈已生成可用的群聊短摘要。",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                content = "FAKE_SYNC_OK:" + str(payload.get("message") or "")[:80]
            self.send_json(
                200,
                {
                    "message": {
                        "content": content
                    },
                    "usage": {"input_tokens": 4, "output_tokens": 3},
                },
            )
            return
        if path == "/v1/runs":
            prompt = str(payload.get("input") or "")
            instructions = str(payload.get("instructions") or "")
            with STATE.lock:
                run_id = "fake-run-%d" % STATE.next_run_id
                STATE.next_run_id += 1
            events: list[dict[str, Any]] = []
            status = "completed"
            output = "fake run completed"
            if "evidence-success" in prompt:
                events.append(
                    {
                        "event": "tool.completed",
                        "tool": "terminal",
                        "duration": 0.01,
                        "error": False,
                        "exit_code": 0,
                    }
                )
            elif "evidence-drop-response" in prompt:
                events.append(
                    {
                        "event": "tool.completed",
                        "tool": "terminal",
                        "duration": 0.01,
                        "error": False,
                        "exit_code": 0,
                    }
                )
                with STATE.lock:
                    STATE.drop_next_text_response = True
            elif "hold-stop" in prompt:
                status = "running"
                output = ""
            elif "media-uncertain" in prompt:
                match = TASK_ID_RE.search(instructions)
                if not match:
                    self.send_json(500, {"detail": "task id missing"})
                    return
                register_fake_image(match.group(0))
                output = "verified image artifact registered"
            elif "no-evidence" in prompt:
                output = "claimed completion without evidence"
            with STATE.lock:
                STATE.runs[run_id] = {
                    "status": status,
                    "output": output,
                    "events": events,
                    "prompt": prompt,
                }
            STATE.record("hermes.run.started", run_id=run_id, prompt=prompt)
            self.send_json(202, {"run_id": run_id})
            return
        if path.startswith("/v1/runs/") and path.endswith("/stop"):
            run_id = urllib.parse.unquote(path.split("/")[-2])
            with STATE.lock:
                if run_id in STATE.runs:
                    STATE.runs[run_id]["status"] = "canceled"
            STATE.record("hermes.run.stop", run_id=run_id)
            self.send_json(200, {"run_id": run_id, "status": "canceled"})
            return
        self.send_json(404, {"detail": "not found"})


def barrier_blocks(
    barrier: dict[str, Any],
    room_id: str,
    source_local_id: int,
    kind: str,
    task_id: str,
    generation: int,
) -> bool:
    if barrier["room_id"] != room_id:
        return False
    if barrier["mode"] == "media_only" and kind == "text":
        return False
    if barrier.get("task_id"):
        return (
            barrier["task_id"] == task_id
            and int(barrier.get("generation") or 0) == generation
        )
    return source_local_id < int(barrier["source_local_id"])


class ChatHandler(JsonHandler):
    def authorized(self) -> bool:
        return self.headers.get("Authorization") == (
            "Bearer " + CHAT_API_TOKEN
        )

    def do_GET(self) -> None:
        if not self.authorized():
            self.send_json(401, {"detail": "unauthorized"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self.send_json(200, {"status": "ready", "ready": True})
            return
        if parsed.path == "/delivery/status":
            values = urllib.parse.parse_qs(parsed.query)
            request_id = values.get("request_id", [""])[0]
            with STATE.lock:
                STATE.delivery_checks.append(request_id)
                delivery = dict(
                    STATE.delivery_states.get(request_id)
                    or {"status": "not_submitted"}
                )
            self.send_json(200, {"ok": True, **delivery})
            return
        if parsed.path == "/control/check":
            values = urllib.parse.parse_qs(parsed.query)
            room_id = values.get("room_id", [""])[0]
            source_local_id = int(values.get("source_local_id", ["0"])[0])
            kind = values.get("item_kind", ["text"])[0]
            task_id = values.get("task_id", [""])[0]
            generation = int(values.get("generation", ["0"])[0])
            with STATE.lock:
                blocking = next(
                    (
                        item
                        for item in reversed(STATE.barriers)
                        if barrier_blocks(
                            item,
                            room_id,
                            source_local_id,
                            kind,
                            task_id,
                            generation,
                        )
                    ),
                    None,
                )
            self.send_json(
                200,
                {"allowed": blocking is None, "barrier": blocking},
            )
            return
        self.send_json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if not self.authorized():
            self.send_json(401, {"detail": "unauthorized"})
            return
        path = urllib.parse.urlsplit(self.path).path
        payload = self.read_json()
        if path == "/control/barriers":
            barrier = {
                "id": len(STATE.barriers) + 1,
                "room_id": str(payload.get("room_id") or ""),
                "source_local_id": int(payload.get("source_local_id") or 0),
                "mode": str(payload.get("mode") or ""),
                "task_id": str(payload.get("task_id") or ""),
                "generation": int(payload.get("generation") or 0),
                "reason": str(payload.get("reason") or ""),
            }
            with STATE.lock:
                STATE.barriers.append(barrier)
            STATE.record("chat.barrier", **barrier)
            self.send_json(201, {"ok": True, "barrier": barrier})
            return
        if path.startswith("/groups/") and path.endswith("/messages"):
            with STATE.lock:
                STATE.chat_calls.append(dict(payload))
                confirmed_local_id = 9000 + len(STATE.chat_calls)
                request_id = str(payload.get("request_id") or "")
                STATE.delivery_states[request_id] = {
                    "status": "confirmed",
                    "confirmed_local_id": confirmed_local_id,
                }
                drop_response = STATE.drop_next_text_response
                STATE.drop_next_text_response = False
            STATE.record(
                "chat.text",
                request_id=request_id,
                task_id=str(payload.get("task_id") or ""),
            )
            if drop_response:
                STATE.record(
                    "chat.text.response_dropped",
                    request_id=request_id,
                    task_id=str(payload.get("task_id") or ""),
                )
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.send_json(
                200,
                {
                    "ok": True,
                    "status": "sent",
                    "confirmed_local_id": confirmed_local_id,
                },
            )
            return
        if path.startswith("/groups/") and path.endswith("/media"):
            with STATE.lock:
                STATE.media_calls.append(dict(payload))
                fail = payload.get("type") == "image" and STATE.fail_next_image
                if fail:
                    STATE.fail_next_image = False
                confirmed_local_id = 10000 + len(STATE.media_calls)
                request_id = str(payload.get("request_id") or "")
                STATE.delivery_states[request_id] = (
                    {"status": "uncertain", "error_type": "send_uncertain"}
                    if fail
                    else {
                        "status": "confirmed",
                        "confirmed_local_id": confirmed_local_id,
                    }
                )
            STATE.record(
                "chat.media",
                request_id=request_id,
                task_id=str(payload.get("task_id") or ""),
                uncertain=fail,
            )
            if fail:
                self.send_json(409, {"status": "uncertain"})
            else:
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "status": "sent",
                        "confirmed_local_id": confirmed_local_id,
                    },
                )
            return
        self.send_json(404, {"detail": "not found"})


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Avoid noisy tracebacks when a test client closes an SSE connection."""

    def handle_error(self, request, client_address) -> None:
        _exc_type, exc, _traceback = sys.exc_info()
        if isinstance(
            exc,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


class ServerThread:
    def __init__(self, handler: type[BaseHTTPRequestHandler]):
        self.server = QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name=handler.__name__,
            daemon=True,
        )

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def adapter_environment(
    database: Path,
    artifacts: Path,
    hermes_url: str,
    chat_url: str,
    adapter_port: int,
    home: Path,
) -> dict[str, str]:
    environment = {
        **os.environ,
        "BRIDGE_TOKEN": BRIDGE_TOKEN,
        "HERMES_WECHAT_INTERNAL_TOKEN": INTERNAL_TOKEN,
        "HERMES_BASE_URL": hermes_url,
        "HERMES_API_KEY": HERMES_TOKEN,
        "WECHAT_CHAT_API_URL": chat_url,
        "WECHAT_CHAT_API_TOKEN": CHAT_API_TOKEN,
        "ALLOWED_WECHAT_ROOM_IDS": ROOM_ID,
        "WECHAT_BOT_WXID": "wxid_fake_bot",
        "HERMES_WECHAT_DB_PATH": str(database),
        "HERMES_WECHAT_ARTIFACT_ROOT": str(artifacts),
        "HERMES_WECHAT_CLEANUP_STATUS_PATH": str(
            database.parent / "cleanup-status.json"
        ),
        "HERMES_WECHAT_ARTIFACT_BASE_URL": (
            "http://127.0.0.1:%d" % adapter_port
        ),
        "HERMES_WECHAT_PORT": str(adapter_port),
        "HERMES_WECHAT_MAX_TASK_SECONDS": "30",
        "HERMES_WECHAT_MAX_TASK_ATTEMPTS": "1",
        "HERMES_WECHAT_DAILY_COST_LIMIT_USD": "20",
        "HERMES_WECHAT_DAILY_TOKEN_LIMIT": "10000000",
        "HERMES_WECHAT_BUDGET_TIMEZONE": "Asia/Shanghai",
        "HERMES_INPUT_TOKEN_COST_PER_MILLION": "3",
        "HERMES_OUTPUT_TOKEN_COST_PER_MILLION": "15",
        "HERMES_WECHAT_SESSION_GENERATION": "fake-stack",
        "ALLOW_PRIVATE_WECHAT_CHAT": "false",
        "HERMES_WECHAT_WORKER_POLL_SECONDS": "0.05",
        "HERMES_WECHAT_SYNC_TIMEOUT_SECONDS": "2",
        "HERMES_WECHAT_GROUP_LISTENER_ENABLED": "true",
        "HERMES_WECHAT_GROUP_LISTENER_MIN_REPLY_GAP_SECONDS": "0",
        "HERMES_WECHAT_GROUP_LISTENER_MIN_TURNS_BETWEEN_REPLIES": "2",
        "HERMES_WECHAT_GROUP_LISTENER_NAMES": "小格,Hermes",
        # Relationship summaries have their own contract tests. Keeping them
        # off here makes this end-to-end harness count foreground chats only.
        "HERMES_WECHAT_RELATIONSHIP_MEMORY_ENABLED": "false",
        "HERMES_WECHAT_DELIVERY_RECONCILE_ATTEMPTS": "3",
        "HERMES_WECHAT_DELIVERY_RECONCILE_DELAY_SECONDS": "0.02",
    }
    return environment


def start_adapter(
    root: Path,
    environment: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    log_file = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            environment["HERMES_WECHAT_PORT"],
            "--log-level",
            "warning",
        ],
        cwd=root,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return process, log_file


def stop_adapter(process: subprocess.Popen[bytes], log_file: Any) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    log_file.close()


def wait_ready(client: httpx.Client, adapter_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("candidate Adapter exited with code %s" % process.returncode)
        try:
            response = client.get(adapter_url + "/health")
            if response.status_code == 200 and response.json().get("ready"):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError("candidate Adapter did not become ready")


def post_chat(
    client: httpx.Client,
    adapter_url: str,
    *,
    message: str,
    local_id: int,
    request_id: str,
    mentions_bot: bool = True,
) -> dict[str, Any]:
    response = client.post(
        adapter_url + "/api/chat",
        headers={"X-Bridge-Token": BRIDGE_TOKEN},
        json={
            "message": message,
            "request_id": request_id,
            "room_id": ROOM_ID,
            "sender_id": "wxid_fake_member",
            "source_local_id": local_id,
            "msg_svr_id": "svr-%d" % local_id,
            "mentions_bot": mentions_bot,
            "reply_to_bot": False,
            "message_type": "text",
        },
    )
    response.raise_for_status()
    return response.json()


def get_task(
    client: httpx.Client,
    adapter_url: str,
    task_id: str,
) -> dict[str, Any]:
    response = client.get(
        adapter_url + "/internal/tasks/" + task_id,
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        params={"room_id": ROOM_ID},
    )
    response.raise_for_status()
    return response.json()["task"]


def wait_task(
    client: httpx.Client,
    adapter_url: str,
    task_id: str,
    statuses: set[str],
    timeout: float = 15,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = get_task(client, adapter_url, task_id)
        if str(last.get("status") or "") in statuses:
            return last
        time.sleep(0.05)
    raise TimeoutError(
        "task %s did not reach %s; last=%s"
        % (task_id, sorted(statuses), last.get("status"))
    )


def outbox_states(database: Path, task_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT kind, state
            FROM outbox_items
            WHERE task_id=?
            ORDER BY id
            """,
            (task_id,),
        ).fetchall()
    return [(str(kind), str(state)) for kind, state in rows]


def wait_outbox_terminal(
    database: Path,
    task_id: str,
    timeout: float = 15,
) -> list[tuple[str, str]]:
    terminal = {"confirmed", "uncertain", "suppressed", "failed"}
    deadline = time.monotonic() + timeout
    states: list[tuple[str, str]] = []
    while time.monotonic() < deadline:
        states = outbox_states(database, task_id)
        if states and all(state in terminal for _, state in states):
            return states
        time.sleep(0.05)
    raise TimeoutError("outbox did not settle for %s: %s" % (task_id, states))


def assert_barrier_precedes_stop() -> None:
    with STATE.lock:
        barrier = next(
            item for item in STATE.events if item["event"] == "chat.barrier"
        )
        stopped = next(
            item for item in STATE.events if item["event"] == "hermes.run.stop"
        )
    if int(barrier["time_ns"]) >= int(stopped["time_ns"]):
        raise AssertionError("Hermes stop happened before the outbound barrier")


def run_live_stack(root: Path) -> dict[str, Any]:
    hermes = ServerThread(HermesHandler)
    chat = ServerThread(ChatHandler)
    hermes.start()
    chat.start()
    adapter_process: subprocess.Popen[bytes] | None = None
    adapter_log: Any = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="wechat-hermes-live-fake-",
            ignore_cleanup_errors=True,
        ) as raw:
            temp = Path(raw)
            database = temp / "adapter.db"
            artifacts = temp / "artifacts"
            home = temp / "home"
            adapter_port = free_port()
            adapter_url = "http://127.0.0.1:%d" % adapter_port
            STATE.adapter_url = adapter_url
            STATE.artifact_root = artifacts
            environment = adapter_environment(
                database,
                artifacts,
                hermes.url,
                chat.url,
                adapter_port,
                home,
            )
            log_path = temp / "adapter.log"
            adapter_process, adapter_log = start_adapter(
                root,
                environment,
                log_path,
            )
            with httpx.Client(timeout=10) as client:
                wait_ready(client, adapter_url, adapter_process)

                mentions = [
                    post_chat(
                        client,
                        adapter_url,
                        message="hello mention %d" % local_id,
                        local_id=local_id,
                        request_id="fake-mention-%d" % local_id,
                    )
                    for local_id in (101, 102, 103)
                ]
                if [item.get("status") for item in mentions] != [
                    "succeeded",
                    "succeeded",
                    "succeeded",
                ]:
                    raise AssertionError("three structured mentions were not independent")
                chat_count = foreground_hermes_chat_count()
                if chat_count != 3:
                    raise AssertionError(
                        "expected 3 foreground Hermes chats, got %d" % chat_count
                    )

                passive_reply = post_chat(
                    client,
                    adapter_url,
                    message="小格，帮我搜索一下今天有什么新闻",
                    local_id=104,
                    request_id="fake-passive-name",
                    mentions_bot=False,
                )
                if passive_reply.get("status") != "succeeded":
                    raise AssertionError(
                        "plain-name passive group chat was not answered"
                    )
                if passive_reply.get("task_id"):
                    raise AssertionError(
                        "passive group message unexpectedly created a task"
                    )
                ignored_passive = post_chat(
                    client,
                    adapter_url,
                    message="哈哈哈",
                    local_id=105,
                    request_id="fake-passive-low-signal",
                    mentions_bot=False,
                )
                if ignored_passive.get("status") != "ignored":
                    raise AssertionError(
                        "low-signal passive group chat was not suppressed"
                    )
                passive_chat_count = foreground_hermes_chat_count()
                if passive_chat_count != 4:
                    raise AssertionError(
                        "passive group routing produced an unexpected foreground chat count"
                    )

                success_reply = post_chat(
                    client,
                    adapter_url,
                    message="mcp command evidence-success",
                    local_id=201,
                    request_id="fake-command-success",
                )
                success_id = str(success_reply.get("task_id") or "")
                if not success_id:
                    raise AssertionError(
                        "evidence-success request was not queued as a task"
                    )
                success = wait_task(
                    client,
                    adapter_url,
                    success_id,
                    {"succeeded"},
                )
                success_outbox = wait_outbox_terminal(database, success_id)
                if ("text", "confirmed") not in success_outbox:
                    raise AssertionError(
                        "successful evidence task summary was not confirmed"
                    )

                dropped_reply = post_chat(
                    client,
                    adapter_url,
                    message="mcp command evidence-drop-response",
                    local_id=203,
                    request_id="fake-command-drop-response",
                )
                dropped_id = str(dropped_reply.get("task_id") or "")
                if not dropped_id:
                    raise AssertionError(
                        "dropped-response request was not queued as a task"
                    )
                wait_task(
                    client,
                    adapter_url,
                    dropped_id,
                    {"succeeded"},
                )
                dropped_outbox = wait_outbox_terminal(database, dropped_id)
                if ("text", "confirmed") not in dropped_outbox:
                    raise AssertionError(
                        "dropped HTTP response was not reconciled as confirmed"
                    )
                with STATE.lock:
                    dropped_sends = sum(
                        item.get("task_id") == dropped_id
                        for item in STATE.chat_calls
                    )
                    dropped_checks = sum(
                        value.startswith("task:%s:" % dropped_id)
                        for value in STATE.delivery_checks
                    )
                if dropped_sends != 1:
                    raise AssertionError(
                        "dropped HTTP response caused a duplicate text send"
                    )
                if dropped_checks < 1:
                    raise AssertionError(
                        "dropped HTTP response did not trigger status reconciliation"
                    )
                reconciliation_metrics = client.get(adapter_url + "/metrics")
                reconciliation_metrics.raise_for_status()
                if (
                    "wechat_hermes_outbox_reconciled_confirmed_total 1"
                    not in reconciliation_metrics.text
                ):
                    raise AssertionError(
                        "Adapter metrics omitted confirmed reconciliation"
                    )

                failed_reply = post_chat(
                    client,
                    adapter_url,
                    message="mcp command no-evidence",
                    local_id=202,
                    request_id="fake-command-no-evidence",
                )
                failed_id = str(failed_reply.get("task_id") or "")
                if not failed_id:
                    raise AssertionError(
                        "no-evidence request was not queued as a task"
                    )
                failed = wait_task(
                    client,
                    adapter_url,
                    failed_id,
                    {"failed"},
                )
                if "exit code 0" not in str(failed.get("error") or ""):
                    raise AssertionError("no-evidence execution did not fail closed")
                wait_outbox_terminal(database, failed_id)

                held_reply = post_chat(
                    client,
                    adapter_url,
                    message="mcp command hold-stop",
                    local_id=301,
                    request_id="fake-hold-stop",
                )
                held_id = str(held_reply.get("task_id") or "")
                if not held_id:
                    raise AssertionError(
                        "hold-stop request was not queued as a task"
                    )
                wait_task(client, adapter_url, held_id, {"running"})
                stop_reply = post_chat(
                    client,
                    adapter_url,
                    message="\u505c\u6b62",
                    local_id=302,
                    request_id="fake-stop-command",
                )
                if stop_reply.get("status") != "canceled":
                    raise AssertionError("stop command did not cancel active work")
                wait_task(client, adapter_url, held_id, {"canceled"})
                stopped_outbox = wait_outbox_terminal(database, held_id)
                if any(state != "suppressed" for _, state in stopped_outbox):
                    raise AssertionError(
                        "old stopped task output escaped the room barrier"
                    )
                assert_barrier_precedes_stop()

                media_reply = post_chat(
                    client,
                    adapter_url,
                    message="mcp image media-uncertain",
                    local_id=401,
                    request_id="fake-media-uncertain",
                )
                media_id = str(media_reply.get("task_id") or "")
                if not media_id:
                    raise AssertionError(
                        "media request was not queued as a task"
                    )
                wait_task(client, adapter_url, media_id, {"succeeded"})
                media_outbox = wait_outbox_terminal(database, media_id)
                if ("image", "uncertain") not in media_outbox:
                    raise AssertionError(
                        "HTTP 409 media delivery was not marked uncertain"
                    )
                if ("text", "confirmed") not in media_outbox:
                    raise AssertionError(
                        "media terminal summary was not sent exactly once"
                    )
                with STATE.lock:
                    media_calls_before = len(STATE.media_calls)
                    summary_calls_before = sum(
                        item.get("task_id") == media_id
                        for item in STATE.chat_calls
                    )
                if media_calls_before != 1 or summary_calls_before != 1:
                    raise AssertionError(
                        "unexpected pre-restart media or summary delivery count"
                    )

                stop_adapter(adapter_process, adapter_log)
                adapter_process = None
                adapter_log = None
                adapter_process, adapter_log = start_adapter(
                    root,
                    environment,
                    log_path,
                )
                wait_ready(client, adapter_url, adapter_process)
                time.sleep(1)
                with STATE.lock:
                    media_calls_after = len(STATE.media_calls)
                    summary_calls_after = sum(
                        item.get("task_id") == media_id
                        for item in STATE.chat_calls
                    )
                if media_calls_after != media_calls_before:
                    raise AssertionError(
                        "uncertain media was retried after Adapter restart"
                    )
                if summary_calls_after != summary_calls_before:
                    raise AssertionError(
                        "terminal summary was duplicated after Adapter restart"
                    )

                metrics = client.get(adapter_url + "/metrics")
                metrics.raise_for_status()
                if "wechat_hermes_outbox" not in metrics.text:
                    raise AssertionError("Adapter metrics omitted Outbox state")
                if "wechat_hermes_outbox_reconciled_confirmed_total" not in metrics.text:
                    raise AssertionError("Adapter metrics omitted reconciliation counter")

                return {
                    "status": "ok",
                    "checks": {
                        "three_structured_mentions": 3,
                        "passive_plain_name_chat": passive_reply["status"],
                        "passive_low_signal": ignored_passive["status"],
                        "execution_with_evidence": success["status"],
                        "dropped_response_outbox": dropped_outbox,
                        "dropped_response_sends": dropped_sends,
                        "dropped_response_checks": dropped_checks,
                        "execution_without_evidence": failed["status"],
                        "stop_barrier_before_run_stop": True,
                        "stopped_outbox": stopped_outbox,
                        "media_outbox": media_outbox,
                        "media_calls_after_restart": media_calls_after,
                        "summary_calls_after_restart": summary_calls_after,
                        "metrics": True,
                    },
                }
    except Exception:
        if "log_path" in locals() and log_path.exists():
            sys.stderr.write(log_path.read_text(encoding="utf-8", errors="replace"))
        raise
    finally:
        if adapter_process is not None and adapter_log is not None:
            stop_adapter(adapter_process, adapter_log)
        chat.close()
        hermes.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    result = run_live_stack(args.root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
