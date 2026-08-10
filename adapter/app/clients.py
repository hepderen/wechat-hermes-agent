from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import urllib.parse
from typing import Any, Awaitable, Callable

import httpx


RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class RemoteAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        error_type: str = "",
        delivery_uncertain: bool = False,
        pre_submission: bool = False,
        retryable: bool = False,
        retry_after_seconds: float = 0,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = str(error_type or "")
        self.delivery_uncertain = bool(delivery_uncertain)
        self.pre_submission = bool(pre_submission)
        self.retryable = bool(retryable)
        self.retry_after_seconds = max(0.0, float(retry_after_seconds or 0))


def response_error(
    response: httpx.Response,
    *,
    data: dict[str, Any] | None = None,
) -> RemoteAPIError:
    payload = data or {}
    retryable = bool(payload.get("retryable")) or (
        response.status_code in RETRYABLE_HTTP_STATUSES
    )
    retry_after = 0.0
    headers = getattr(response, "headers", {}) or {}
    try:
        retry_after = min(
            60.0,
            max(0.0, float(headers.get("Retry-After") or 0)),
        )
    except (TypeError, ValueError):
        retry_after = 0.0
    return RemoteAPIError(
        "remote API returned HTTP %s" % response.status_code,
        response.status_code,
        error_type=str(payload.get("error_type") or ""),
        retryable=retryable,
        retry_after_seconds=retry_after,
    )


def retry_delay_seconds(error: RemoteAPIError, attempts: int) -> float:
    if not error.retryable:
        return 0.0
    attempt = max(1, int(attempts))
    exponential = min(30.0, 5.0 * (2 ** (attempt - 1)))
    return min(60.0, max(exponential, error.retry_after_seconds))


class HermesClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.api_key,
            "Accept": "application/json",
        }

    async def ensure_session(
        self,
        session_id: str,
        title: str,
        system_prompt: str,
    ) -> None:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    self.base_url
                    + "/api/sessions/"
                    + urllib.parse.quote(session_id, safe=""),
                    headers=self.headers(),
                )
                if response.status_code == 200:
                    return
                if response.status_code != 404:
                    raise response_error(response)
                response = await client.post(
                    self.base_url + "/api/sessions",
                    headers=self.headers(),
                    json={
                        "id": session_id,
                        "title": title,
                        "system_prompt": system_prompt,
                    },
                )
        except httpx.HTTPError as exc:
            raise RemoteAPIError("Hermes session request failed") from exc
        if response.status_code not in {201, 409}:
            raise response_error(response)

    async def chat(
        self,
        session_id: str,
        message: str,
        system_message: str,
        *,
        timeout_seconds: float = 240,
        disable_tools: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        timeout = httpx.Timeout(connect=15, read=timeout_seconds, write=30, pool=15)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.base_url
                    + "/api/sessions/"
                    + urllib.parse.quote(session_id, safe="")
                    + "/chat",
                    headers=self.headers(),
                    json={
                        "message": message,
                        "system_message": system_message,
                        "disable_tools": disable_tools,
                    },
                )
        except httpx.HTTPError as exc:
            raise RemoteAPIError("Hermes chat request failed") from exc
        if response.status_code != 200:
            raise response_error(response)
        data = response.json()
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise RemoteAPIError("Hermes returned an empty session response")
        return content, data.get("usage") or {}

    async def session_history(
        self,
        session_id: str,
        limit: int = 8,
        max_chars: int = 12_000,
        max_message_chars: int = 4_000,
    ) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                self.base_url
                + "/api/sessions/"
                + urllib.parse.quote(session_id, safe="")
                + "/messages",
                headers=self.headers(),
            )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise response_error(response)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            return []
        selected: list[dict[str, str]] = []
        remaining = max(1, int(max_chars))
        item_limit = max(1, int(max_message_chars))
        for item in reversed(data):
            if len(selected) >= max(1, int(limit)) or remaining <= 0:
                break
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                bounded = content[: min(item_limit, remaining)]
                selected.append({"role": role, "content": bounded})
                remaining -= len(bounded)
        selected.reverse()
        return selected

    async def start_run(
        self,
        session_id: str,
        message: str,
        instructions: str,
        history: list[dict[str, str]],
        *,
        idempotency_key: str,
    ) -> str:
        trusted_key = str(idempotency_key or "").strip()
        if not trusted_key:
            raise ValueError("Hermes run idempotency key is required")
        response = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        self.base_url + "/v1/runs",
                        headers={
                            **self.headers(),
                            "Idempotency-Key": trusted_key,
                        },
                        json={
                            "input": message,
                            "instructions": instructions,
                            "session_id": session_id,
                            "conversation_history": history,
                            "idempotency_key": trusted_key,
                        },
                    )
                break
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(0.1)
                    continue
                raise RemoteAPIError(
                    "Hermes run creation outcome is unknown",
                    error_type="run_creation_uncertain",
                    delivery_uncertain=True,
                ) from exc
        if response is None:
            raise RemoteAPIError("Hermes run creation returned no response")
        if response.status_code != 202:
            raise response_error(response)
        try:
            data = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RemoteAPIError(
                "Hermes run creation returned invalid JSON",
                response.status_code,
                error_type="run_creation_uncertain",
                delivery_uncertain=True,
            ) from exc
        if not isinstance(data, dict):
            raise RemoteAPIError(
                "Hermes run creation returned a non-object response",
                response.status_code,
                error_type="run_creation_uncertain",
                delivery_uncertain=True,
            )
        run_id = str(data.get("run_id") or "")
        if not run_id:
            raise RemoteAPIError(
                "Hermes run creation did not return a run ID",
                response.status_code,
                error_type="run_creation_uncertain",
                delivery_uncertain=True,
            )
        return run_id

    async def get_run(self, run_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                self.base_url + "/v1/runs/" + urllib.parse.quote(run_id, safe=""),
                headers=self.headers(),
            )
        if response.status_code != 200:
            raise response_error(response)
        return response.json()

    async def stop_run(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                self.base_url
                + "/v1/runs/"
                + urllib.parse.quote(run_id, safe="")
                + "/stop",
                headers=self.headers(),
                json={},
            )
        if response.status_code not in {200, 404}:
            raise response_error(response)

    async def wait_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
        cancel_requested: Callable[[], bool],
        event_callback: Callable[
            [dict[str, Any]], Awaitable[None] | None
        ] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        terminal_event = False
        seen_events: set[str] = set()
        last_event_id = ""

        async def publish(event: dict[str, Any]) -> None:
            nonlocal last_event_id
            raw_id = str(
                event.get("id")
                or event.get("event_id")
                or event.get("sequence")
                or ""
            ).strip()
            if raw_id:
                event_key = "id:" + raw_id
            else:
                encoded = json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                event_key = "sha256:" + hashlib.sha256(
                    encoded.encode("utf-8")
                ).hexdigest()
            if event_key in seen_events:
                return
            seen_events.add(event_key)
            if raw_id:
                last_event_id = raw_id
            event.setdefault("_adapter_event_key", event_key)
            if event_callback is None:
                return
            result = event_callback(event)
            if inspect.isawaitable(result):
                await result

        timeout = httpx.Timeout(connect=15, read=35, write=20, pool=15)
        watcher_done = asyncio.Event()

        async def watch_cancellation() -> None:
            while not watcher_done.is_set():
                if cancel_requested():
                    try:
                        await self.stop_run(run_id)
                    except RemoteAPIError:
                        await asyncio.sleep(0.1)
                        continue
                    return
                try:
                    await asyncio.wait_for(watcher_done.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass

        cancellation_watcher = asyncio.create_task(
            watch_cancellation(),
            name="hermes-cancel-watch-%s" % run_id,
        )
        try:
            for stream_attempt in range(2):
                if terminal_event:
                    break
                stream_headers = {
                    **self.headers(),
                    "Accept": "text/event-stream",
                }
                if last_event_id:
                    stream_headers["Last-Event-ID"] = last_event_id
                sse_event_id = ""
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream(
                            "GET",
                            self.base_url
                            + "/v1/runs/"
                            + urllib.parse.quote(run_id, safe="")
                            + "/events",
                            headers=stream_headers,
                        ) as response:
                            if response.status_code != 200:
                                raise response_error(response)
                            async for line in response.aiter_lines():
                                if cancel_requested():
                                    await self.stop_run(run_id)
                                if time.monotonic() >= deadline:
                                    await self.stop_run(run_id)
                                    raise TimeoutError(
                                        "Hermes run exceeded the task time limit"
                                    )
                                if line.startswith("id:"):
                                    sse_event_id = line[3:].strip()
                                    continue
                                if not line.startswith("data:"):
                                    continue
                                try:
                                    event = json.loads(line[5:].strip())
                                except json.JSONDecodeError:
                                    continue
                                if sse_event_id and not any(
                                    event.get(key)
                                    for key in ("id", "event_id", "sequence")
                                ):
                                    event["id"] = sse_event_id
                                sse_event_id = ""
                                event_name = event.get("event") or event.get("type")
                                await publish(event)
                                if event_name in {
                                    "run.completed",
                                    "run.failed",
                                    "run.cancelled",
                                    "run.canceled",
                                }:
                                    terminal_event = True
                                    break
                except (httpx.HTTPError, RemoteAPIError):
                    if stream_attempt == 0 and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                        continue
                    break
        finally:
            watcher_done.set()
            await cancellation_watcher

        while True:
            if cancel_requested():
                await self.stop_run(run_id)
            if time.monotonic() >= deadline:
                await self.stop_run(run_id)
                raise TimeoutError("Hermes run exceeded the task time limit")
            status = await self.get_run(run_id)
            historical_events = (
                status.get("events")
                or status.get("tool_events")
                or status.get("event_history")
                or []
            )
            if isinstance(historical_events, list):
                for event in historical_events:
                    if isinstance(event, dict):
                        await publish(event)
            raw = str(status.get("status") or "").lower()
            if raw in {"completed", "failed", "cancelled", "canceled"}:
                if not terminal_event:
                    suffix = "cancelled" if raw == "canceled" else raw
                    await publish(
                        {
                            "event": "run." + suffix,
                            "run_id": run_id,
                            "source": "poll",
                        }
                    )
                return status
            if raw == "waiting_for_approval":
                await self.stop_run(run_id)
                raise RemoteAPIError(
                    "Hermes requested approval even though approval mode must be disabled"
                )
            if terminal_event:
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(2)


class ChatApiClient:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        chunk_chars: int = 1500,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_token = str(auth_token or "").strip()
        self.chunk_chars = max(100, int(chunk_chars))

    def headers(self) -> dict[str, str]:
        if not self.auth_token:
            raise ValueError("Chat API authentication token is required")
        return {
            "Authorization": "Bearer " + self.auth_token,
            "Accept": "application/json",
        }

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RemoteAPIError(
                "Chat API returned a non-JSON response",
                response.status_code,
                error_type="invalid_response",
                delivery_uncertain=response.status_code in {200, 201, 202},
            ) from exc
        if not isinstance(data, dict):
            raise RemoteAPIError(
                "Chat API returned a non-object response",
                response.status_code,
                error_type="invalid_response",
                delivery_uncertain=response.status_code in {200, 201, 202},
            )
        return data

    @staticmethod
    def _validate_envelope(
        *,
        request_id: str,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        trusted_request_id = str(request_id or "").strip()
        trusted_task_id = str(task_id or "").strip()
        trusted_source_local_id = int(source_local_id)
        trusted_generation = int(generation or 0)
        if not trusted_request_id:
            raise ValueError("request_id is required")
        if trusted_source_local_id <= 0:
            raise ValueError("source_local_id must be positive")
        if trusted_task_id and trusted_generation <= 0:
            raise ValueError("task deliveries require a positive generation")
        if not trusted_task_id:
            trusted_generation = 0
        return {
            "request_id": trusted_request_id,
            "source_local_id": trusted_source_local_id,
            "task_id": trusted_task_id,
            "generation": trusted_generation,
        }

    @staticmethod
    def split_text(text: str, max_chars: int) -> list[str]:
        remaining = str(text or "").strip()
        chunks: list[str] = []
        separators = ("\n\n", "\n", "。", "！", "？", "；", "，", " ")
        while len(remaining) > max_chars:
            window = remaining[: max_chars + 1]
            cut = max(
                (window.rfind(separator, max_chars // 2) for separator in separators),
                default=-1,
            )
            if cut < 0:
                cut = max_chars
            else:
                cut += 1
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            chunks.append(remaining)
        return [chunk for chunk in chunks if chunk]

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float,
        *,
        delivery: bool = False,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.base_url + path,
                    headers=self.headers(),
                    json=payload,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise RemoteAPIError(
                "Chat API connection failed before submission",
                error_type="connection_failed",
                pre_submission=True,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise RemoteAPIError(
                "Chat API request failed after submission may have begun",
                error_type=type(exc).__name__,
                delivery_uncertain=delivery,
            ) from exc
        data = self._response_object(response)
        if response.status_code == 423:
            return {
                "ok": False,
                "status": "suppressed",
                "barrier": data.get("barrier") or {},
                "error_type": str(data.get("error_type") or "barrier"),
            }
        if response.status_code == 409:
            error_type = str(data.get("error_type") or "")
            status = str(data.get("status") or "").lower()
            if error_type == "idempotency_conflict" or status == "idempotency_conflict":
                raise RemoteAPIError(
                    "Chat API rejected an altered idempotent request",
                    response.status_code,
                    error_type="idempotency_conflict",
                )
            if error_type != "send_uncertain" and status != "uncertain":
                raise RemoteAPIError(
                    "Chat API returned an unknown conflict response",
                    response.status_code,
                    error_type=error_type or "unknown_conflict",
                    delivery_uncertain=delivery,
                )
            return {
                "ok": False,
                "status": "uncertain",
                "error_type": error_type or "send_uncertain",
            }
        if response.status_code not in {200, 201, 202}:
            raise response_error(response, data=data)
        return data

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.base_url + path,
                    headers=self.headers(),
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise RemoteAPIError("Chat API request failed") from exc
        data = self._response_object(response)
        if response.status_code != 200:
            raise response_error(response, data=data)
        return data

    @staticmethod
    def trusted_envelope(
        *,
        request_id: str,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        return ChatApiClient._validate_envelope(
            request_id=request_id,
            source_local_id=source_local_id,
            task_id=task_id,
            generation=generation,
        )

    async def commit_barrier(
        self,
        room_id: str,
        source_local_id: int,
        mode: str,
        *,
        task_id: str = "",
        generation: int = 0,
        reason: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        trusted_request_id = str(request_id or "").strip() or (
            "barrier:%s:%d:%s:%s:%d"
            % (
                room_id,
                int(source_local_id),
                mode,
                str(task_id or ""),
                int(generation or 0),
            )
        )
        envelope = self.trusted_envelope(
            request_id=trusted_request_id,
            source_local_id=source_local_id,
            task_id=task_id,
            generation=generation,
        )
        return await self._post(
            "/control/barriers",
            {
                "room_id": room_id,
                "mode": mode,
                "reason": str(reason or "")[:300],
                **envelope,
            },
            5,
        )

    async def check_barrier(
        self,
        room_id: str,
        source_local_id: int,
        item_kind: str,
        *,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        envelope = self._validate_envelope(
            request_id="barrier-check:%s:%d:%s:%s:%d"
            % (
                room_id,
                int(source_local_id),
                item_kind,
                str(task_id or ""),
                int(generation or 0),
            ),
            source_local_id=source_local_id,
            task_id=task_id,
            generation=generation,
        )
        return await self._get(
            "/control/check",
            {
                "room_id": room_id,
                "item_kind": item_kind,
                "source_local_id": envelope["source_local_id"],
                "task_id": envelope["task_id"],
                "generation": envelope["generation"],
            },
            5,
        )

    async def delivery_status(
        self,
        room_id: str,
        request_id: str,
        item_kind: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        envelope = self._validate_envelope(
            request_id=request_id,
            source_local_id=source_local_id,
            task_id=task_id,
            generation=generation,
        )
        return await self._get(
            "/delivery/status",
            {
                "room_id": room_id,
                "item_kind": str(item_kind or "").strip().lower(),
                **envelope,
            },
            10,
        )

    async def send_text_item(
        self,
        room_id: str,
        text: str,
        request_id: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        room = urllib.parse.quote(room_id, safe="")
        return await self._post(
            "/groups/%s/messages" % room,
            {
                "text": text,
                **self.trusted_envelope(
                    request_id=request_id,
                    source_local_id=source_local_id,
                    task_id=task_id,
                    generation=generation,
                ),
            },
            30,
            delivery=True,
        )

    async def send_text(
        self,
        room_id: str,
        text: str,
        request_id: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> list[dict[str, Any]]:
        chunks = self.split_text(text, self.chunk_chars)
        results: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, 1):
            chunk_id = request_id if len(chunks) == 1 else "%s:part:%d" % (request_id, index)
            results.append(
                await self.send_text_item(
                    room_id,
                    chunk,
                    chunk_id,
                    source_local_id=source_local_id,
                    task_id=task_id,
                    generation=generation,
                )
            )
        return results

    async def send_image(
        self,
        room_id: str,
        encoded: str,
        request_id: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        room = urllib.parse.quote(room_id, safe="")
        return await self._post(
            "/groups/%s/media" % room,
            {
                "type": "image",
                "data": encoded,
                **self.trusted_envelope(
                    request_id=request_id,
                    source_local_id=source_local_id,
                    task_id=task_id,
                    generation=generation,
                ),
            },
            180,
            delivery=True,
        )

    async def send_video(
        self,
        room_id: str,
        url: str,
        request_id: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        room = urllib.parse.quote(room_id, safe="")
        return await self._post(
            "/groups/%s/media" % room,
            {
                "type": "video",
                "url": url,
                **self.trusted_envelope(
                    request_id=request_id,
                    source_local_id=source_local_id,
                    task_id=task_id,
                    generation=generation,
                ),
            },
            240,
            delivery=True,
        )

    async def send_file(
        self,
        room_id: str,
        url: str,
        request_id: str,
        *,
        source_local_id: int,
        task_id: str = "",
        generation: int = 0,
    ) -> dict[str, Any]:
        room = urllib.parse.quote(room_id, safe="")
        return await self._post(
            "/groups/%s/media" % room,
            {
                "type": "file",
                "url": url,
                **self.trusted_envelope(
                    request_id=request_id,
                    source_local_id=source_local_id,
                    task_id=task_id,
                    generation=generation,
                ),
            },
            240,
            delivery=True,
        )
