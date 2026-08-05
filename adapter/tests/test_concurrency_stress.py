from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.store import AdapterStore
from tests.test_store_v2 import ROOM_ID, queued_task


def run_race(executor, first, second):
    barrier = threading.Barrier(2)

    def synchronized(function):
        barrier.wait(timeout=5)
        return function()

    left = executor.submit(synchronized, first)
    right = executor.submit(synchronized, second)
    return left.result(timeout=10), right.result(timeout=10)


def test_same_inbound_message_has_exactly_one_concurrent_owner(tmp_path):
    store = AdapterStore(tmp_path / "adapter.db")

    def begin():
        return store.begin_inbound(
            request_id="concurrent-request",
            request_hash="same-content",
            room_id=ROOM_ID,
            sender_id="wxid_sender",
            source_local_id=900,
            msg_svr_id="9900",
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _index: begin(), range(100)))

    assert sum(result["created"] is True for result in results) == 1
    assert {result["request_id"] for result in results} == {
        "concurrent-request"
    }


def test_cancel_and_late_run_id_attachment_preserve_task_invariants_100_times(
    tmp_path,
):
    store = AdapterStore(tmp_path / "adapter.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        for index in range(100):
            task = queued_task(store, "cancel-run-race-%03d" % index, index + 1)
            claimed = store.claim_next()
            assert claimed["id"] == task["id"]
            run_id = "run-race-%03d" % index
            attached, canceled = run_race(
                executor,
                lambda: store.set_run_id(
                    task["id"],
                    run_id,
                    generation=task["generation"],
                ),
                lambda: store.cancel_task(task["id"], ROOM_ID),
            )

            current = store.get_task(task["id"])
            assert canceled["id"] == task["id"]
            assert current["cancel_requested"] is True
            assert (current["hermes_run_id"] == run_id) is bool(attached)
            assert store.complete(
                task["id"],
                "canceled",
                generation=task["generation"],
            ) is True


def test_outbox_send_claim_and_stop_suppression_never_return_to_prepared(
    tmp_path,
):
    store = AdapterStore(tmp_path / "adapter.db")
    with ThreadPoolExecutor(max_workers=2) as executor:
        for index in range(100):
            task = queued_task(store, "outbox-race-%03d" % index, index + 1)
            claimed = store.claim_next()
            assert store.complete(claimed["id"], "succeeded", output="done")
            item = store.prepare_outbox(
                task["id"],
                task["generation"],
                [
                    {
                        "kind": "text",
                        "content": "summary",
                        "source_local_id": index + 1,
                        "is_summary": True,
                    }
                ],
            )[0]

            run_race(
                executor,
                lambda: store.mark_outbox_sending(item["id"]),
                lambda: store.suppress_task_generation(
                    task["id"],
                    task["generation"],
                    "concurrent stop",
                ),
            )
            current = store.list_outbox(task["id"], task["generation"])[0]
            assert current["state"] in {"sending", "suppressed"}
            assert current["state"] != "prepared"
            if current["state"] == "sending":
                assert store.recover_outbox() >= 1
                recovered = store.list_outbox(
                    task["id"],
                    task["generation"],
                )[0]
                assert recovered["state"] == "uncertain"
