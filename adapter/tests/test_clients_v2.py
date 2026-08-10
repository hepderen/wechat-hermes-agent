from __future__ import annotations

import asyncio

import httpx
import pytest

from app import clients
from app.clients import ChatApiClient, HermesClient, RemoteAPIError


class Response:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload


def test_transient_http_errors_are_retryable_with_bounded_backoff():
    error = clients.response_error(
        Response(429, {}, {"Retry-After": "12"})
    )
    assert error.retryable is True
    assert error.retry_after_seconds == 12
    assert clients.retry_delay_seconds(error, 1) == 12
    assert clients.retry_delay_seconds(error, 4) == 30


def test_permanent_http_errors_do_not_trigger_backoff():
    error = clients.response_error(Response(400, {}))
    assert error.retryable is False
    assert clients.retry_delay_seconds(error, 1) == 0


def test_wait_run_publishes_sse_tool_events():
    events = [
        {"event": "tool.started", "tool": "terminal"},
        {
            "event": "tool.completed",
            "tool": "terminal",
            "result": {"exit_code": 0},
        },
        {"event": "run.completed"},
    ]

    class Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            import json

            for event in events:
                yield "data: " + json.dumps(event)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Stream()

    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    hermes.get_run = lambda _run_id: asyncio.sleep(
        0, result={"status": "completed", "output": "done"}
    )
    captured = []
    original = clients.httpx.AsyncClient
    clients.httpx.AsyncClient = lambda **_kwargs: Client()
    try:
        result = asyncio.run(
            hermes.wait_run(
                "run-1",
                timeout_seconds=5,
                cancel_requested=lambda: False,
                event_callback=captured.append,
            )
        )
    finally:
        clients.httpx.AsyncClient = original
    assert result["status"] == "completed"
    assert [event["event"] for event in captured] == [
        "tool.started",
        "tool.completed",
        "run.completed",
    ]


def test_wait_run_emits_poll_terminal_after_sse_disconnect(monkeypatch):
    class BrokenStream:
        async def __aenter__(self):
            raise httpx.ReadError("disconnect")

        async def __aexit__(self, *_args):
            return False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return BrokenStream()

    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hermes,
        "get_run",
        lambda _run_id: asyncio.sleep(
            0, result={"status": "completed", "output": "done"}
        ),
    )
    captured = []
    asyncio.run(
        hermes.wait_run(
            "run-1",
            timeout_seconds=5,
            cancel_requested=lambda: False,
            event_callback=captured.append,
        )
    )
    assert len(captured) == 1
    assert captured[0]["event"] == "run.completed"
    assert captured[0]["run_id"] == "run-1"
    assert captured[0]["source"] == "poll"
    assert captured[0]["_adapter_event_key"].startswith("sha256:")


def test_wait_run_stops_idle_sse_when_cancel_is_requested(monkeypatch):
    stopped = asyncio.Event()

    class IdleStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            await asyncio.wait_for(stopped.wait(), timeout=1)
            yield 'data: {"event":"run.canceled"}'

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return IdleStream()

    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    cancel_requested = False

    async def stop_run(_run_id):
        stopped.set()

    async def get_run(_run_id):
        return {"status": "canceled", "output": ""}

    async def scenario():
        nonlocal cancel_requested
        task = asyncio.create_task(
            hermes.wait_run(
                "run-idle",
                timeout_seconds=5,
                cancel_requested=lambda: cancel_requested,
            )
        )
        await asyncio.sleep(0.05)
        started = asyncio.get_running_loop().time()
        cancel_requested = True
        result = await asyncio.wait_for(task, timeout=1)
        return result, asyncio.get_running_loop().time() - started

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hermes, "stop_run", stop_run)
    monkeypatch.setattr(hermes, "get_run", get_run)
    result, elapsed = asyncio.run(scenario())
    assert result["status"] == "canceled"
    assert elapsed < 1


def test_start_run_retries_response_loss_with_same_idempotency_key(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            calls.append((url, dict(headers), dict(json)))
            if len(calls) == 1:
                raise httpx.ReadError("response lost")
            return Response(202, {"run_id": "run-recovered"})

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    run_id = asyncio.run(
        hermes.start_run(
            "session-1",
            "execute",
            "trusted instructions",
            [],
            idempotency_key="task:T-12345678:generation:2",
        )
    )

    assert run_id == "run-recovered"
    assert len(calls) == 2
    assert calls[0][1]["Idempotency-Key"] == calls[1][1]["Idempotency-Key"]
    assert calls[0][2] == calls[1][2]


@pytest.mark.parametrize("payload", [None, [], {}])
def test_start_run_treats_unusable_202_response_as_uncertain(
    monkeypatch,
    payload,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            if payload is None:
                class InvalidJSONResponse:
                    status_code = 202

                    def json(self):
                        raise ValueError("response body was lost")

                return InvalidJSONResponse()
            return Response(202, payload)

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    hermes = HermesClient("http://127.0.0.1:8642", "secret")

    with pytest.raises(RemoteAPIError) as caught:
        asyncio.run(
            hermes.start_run(
                "session-1",
                "execute",
                "trusted instructions",
                [],
                idempotency_key="task:T-12345678:generation:1:attempt:1",
            )
        )

    assert caught.value.delivery_uncertain is True
    assert caught.value.error_type == "run_creation_uncertain"


def test_wait_run_deduplicates_sse_replay_against_poll_history(monkeypatch):
    replayed = {
        "id": "event-1",
        "event": "tool.started",
        "tool": "terminal",
    }

    class ReplayThenDisconnect:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            import json

            yield "data: " + json.dumps(replayed)
            raise httpx.ReadError("disconnect after replayable event")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return ReplayThenDisconnect()

    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hermes,
        "get_run",
        lambda _run_id: asyncio.sleep(
            0,
            result={
                "status": "completed",
                "output": "done",
                "events": [dict(replayed)],
            },
        ),
    )
    captured = []
    result = asyncio.run(
        hermes.wait_run(
            "run-1",
            timeout_seconds=5,
            cancel_requested=lambda: False,
            event_callback=captured.append,
        )
    )

    assert result["status"] == "completed"
    assert [event["event"] for event in captured].count("tool.started") == 1
    assert [event["event"] for event in captured].count("run.completed") == 1


def test_wait_run_reconnects_sse_with_last_event_id_and_collects_evidence(
    monkeypatch,
):
    calls = []

    class Stream:
        status_code = 200

        def __init__(self, attempt):
            self.attempt = attempt

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            if self.attempt == 1:
                yield "id: event-1"
                yield 'data: {"event":"tool.started","tool":"terminal"}'
                raise httpx.ReadError("disconnect")
            yield "id: event-2"
            yield (
                'data: {"event":"tool.completed","tool":"terminal",'
                '"exit_code":0}'
            )
            yield 'data: {"event":"run.completed"}'

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **kwargs):
            calls.append(dict(kwargs["headers"]))
            return Stream(len(calls))

    hermes = HermesClient("http://127.0.0.1:8642", "secret")
    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hermes,
        "get_run",
        lambda _run_id: asyncio.sleep(
            0,
            result={"status": "completed", "output": "done"},
        ),
    )
    captured = []

    result = asyncio.run(
        hermes.wait_run(
            "run-reconnect",
            timeout_seconds=5,
            cancel_requested=lambda: False,
            event_callback=captured.append,
        )
    )

    assert result["status"] == "completed"
    assert len(calls) == 2
    assert "Last-Event-ID" not in calls[0]
    assert calls[1]["Last-Event-ID"] == "event-1"
    assert [event["event"] for event in captured] == [
        "tool.started",
        "tool.completed",
        "run.completed",
    ]


def test_chat_api_client_sends_trusted_envelope_and_maps_delivery_states(
    monkeypatch,
):
    calls = []
    responses = [
        Response(201, {"ok": True, "barrier": {"id": 1}}),
        Response(423, {"status": "suppressed", "barrier": {"id": 1}}),
        Response(
            409,
            {
                "status": "uncertain",
                "error_type": "send_uncertain",
            },
        ),
        Response(200, {"status": "sent", "confirmed_local_id": 44}),
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            calls.append((url, headers, json))
            return responses.pop(0)

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    chat = ChatApiClient("http://127.0.0.1:8765", "internal-secret")

    barrier = asyncio.run(
        chat.commit_barrier(
            "room@chatroom",
            20,
            "all",
            task_id="T-12345678",
            generation=2,
            reason="stop",
        )
    )
    suppressed = asyncio.run(
        chat.send_text_item(
            "room@chatroom",
            "old",
            "outbox-1",
            source_local_id=19,
            task_id="T-12345678",
            generation=2,
        )
    )
    uncertain = asyncio.run(
        chat.send_image(
            "room@chatroom",
            "encoded",
            "outbox-2",
            source_local_id=21,
            task_id="T-12345678",
            generation=2,
        )
    )
    sent = asyncio.run(
        chat.send_video(
            "room@chatroom",
            "http://127.0.0.1/video",
            "outbox-3",
            source_local_id=21,
            task_id="T-12345678",
            generation=2,
        )
    )

    assert barrier["ok"] is True
    assert suppressed["status"] == "suppressed"
    assert uncertain["status"] == "uncertain"
    assert sent["confirmed_local_id"] == 44
    assert calls[0][1]["Authorization"] == "Bearer internal-secret"
    assert calls[1][2]["source_local_id"] == 19
    assert calls[1][2]["task_id"] == "T-12345678"
    assert calls[1][2]["generation"] == 2


def test_chat_api_client_queries_delivery_status_with_trusted_envelope(
    monkeypatch,
):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, *, headers, params):
            calls.append((url, headers, params))
            return Response(
                200,
                {
                    "ok": True,
                    "status": "confirmed",
                    "confirmed_local_id": 44,
                },
            )

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    chat = ChatApiClient("http://127.0.0.1:8765", "internal-secret")

    result = asyncio.run(
        chat.delivery_status(
            "room@chatroom",
            "task:T-12345678:g:2:item:1",
            "text",
            source_local_id=21,
            task_id="T-12345678",
            generation=2,
        )
    )

    assert result["status"] == "confirmed"
    assert calls == [
            (
                "http://127.0.0.1:8765/delivery/status",
                {
                    "Authorization": "Bearer internal-secret",
                    "Accept": "application/json",
                },
            {
                "room_id": "room@chatroom",
                "item_kind": "text",
                "request_id": "task:T-12345678:g:2:item:1",
                "source_local_id": 21,
                "task_id": "T-12345678",
                "generation": 2,
            },
        )
    ]


def test_chat_api_client_rejects_invalid_envelopes_before_network(monkeypatch):
    class Client:
        async def __aenter__(self):
            raise AssertionError("network must not be touched")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    chat = ChatApiClient("http://127.0.0.1:8765", "internal-secret")

    calls = (
        chat.send_text_item(
            "room@chatroom",
            "text",
            "",
            source_local_id=1,
            task_id="T-12345678",
            generation=1,
        ),
        chat.send_image(
            "room@chatroom",
            "encoded",
            "image-1",
            source_local_id=0,
            task_id="T-12345678",
            generation=1,
        ),
        chat.send_file(
            "room@chatroom",
            "http://127.0.0.1/file",
            "file-1",
            source_local_id=1,
            task_id="T-12345678",
            generation=0,
        ),
    )
    for call in calls:
        try:
            asyncio.run(call)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid envelope was accepted")


def test_chat_api_client_distinguishes_idempotency_conflict(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            assert headers["Authorization"] == "Bearer internal-secret"
            assert json["request_id"]
            return Response(
                409,
                {
                    "status": "idempotency_conflict",
                    "error_type": "idempotency_conflict",
                },
            )

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    chat = ChatApiClient("http://127.0.0.1:8765", "internal-secret")

    try:
        asyncio.run(
            chat.send_text_item(
                "room@chatroom",
                "changed",
                "text-1",
                source_local_id=1,
                task_id="T-12345678",
                generation=1,
            )
        )
    except clients.RemoteAPIError as exc:
        assert exc.error_type == "idempotency_conflict"
        assert exc.delivery_uncertain is False
    else:
        raise AssertionError("idempotency conflict was not rejected")


def test_chat_api_client_preserves_422_idempotency_conflict(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            return Response(
                422,
                {
                    "status": "idempotency_conflict",
                    "error_type": "idempotency_conflict",
                },
            )

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    chat = ChatApiClient("http://127.0.0.1:8765", "internal-secret")

    try:
        asyncio.run(
            chat.send_text_item(
                "room@chatroom",
                "changed",
                "text-422",
                source_local_id=1,
                task_id="T-12345678",
                generation=1,
            )
        )
    except clients.RemoteAPIError as exc:
        assert exc.status_code == 422
        assert exc.error_type == "idempotency_conflict"
        assert exc.delivery_uncertain is False
    else:
        raise AssertionError("HTTP 422 idempotency conflict was not rejected")
def test_session_history_keeps_newest_messages_with_strict_character_budget(
    monkeypatch,
):
    data = [
        {
            "role": "user" if index % 2 else "assistant",
            "content": "marker-%02d:" % index + ("x" * 5_000),
        }
        for index in range(1, 13)
    ]
    data.insert(9, {"role": "tool", "content": "must-not-leak"})

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, *, headers):
            assert headers["Authorization"] == "Bearer secret"
            return Response(200, {"data": data})

    monkeypatch.setattr(clients.httpx, "AsyncClient", lambda **_kwargs: Client())
    hermes = HermesClient("http://127.0.0.1:8642", "secret")

    history = asyncio.run(hermes.session_history("session-1"))

    assert [item["content"][:10] for item in history] == [
        "marker-10:",
        "marker-11:",
        "marker-12:",
    ]
    assert all(len(item["content"]) <= 4_000 for item in history)
    assert sum(len(item["content"]) for item in history) == 12_000
    assert all(item["role"] in {"user", "assistant"} for item in history)
