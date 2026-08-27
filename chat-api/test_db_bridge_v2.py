import importlib.util
import json
import threading
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent


def load_chat_api():
    module_name = "chat_api_v2_test_%d" % threading.get_ident()
    spec = importlib.util.spec_from_file_location(module_name, HERE / "chat_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bridge():
    module_name = "db_bridge_v2_test_%d" % threading.get_ident()
    spec = importlib.util.spec_from_file_location(module_name, HERE / "db_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.GROUP_ID = "group"
    module.BOT_WXID = "wxid_bot"
    module.AI_API_URL = "http://127.0.0.1:8000/api/chat"
    module.AI_API_TOKEN = "test-token"
    module.AI_API_TIMEOUT = 10
    module.CONTROL_SCAN_SECONDS = 10
    module.GROUP_LISTENER_ENABLED = False
    module.PREPROCESSED_CONTROL_IDS.clear()
    return module


class BridgeV2IngressTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()
        self.chat_api = load_chat_api()

    def serialized_message(self, local_id, prompt="do work"):
        source = (
            "<msgsource><atuserlist>wxid_bot</atuserlist></msgsource>"
        ).encode("utf-8")
        row = {
            "local_id": local_id,
            "server_id": "server-%d" % local_id,
            "local_type": 1,
            "sort_seq": local_id * 10,
            "real_sender_id": 7,
            "create_time": 100000 + local_id,
            "status": 3,
            "origin_source": 2,
            "message_content": (
                "wxid_member:\n@Hermes\u2005" + prompt
            ),
            "WCDB_CT_message_content": 0,
            "source": self.chat_api.zstandard.ZstdCompressor().compress(source),
            "WCDB_CT_source": 4,
        }

        class FakeReader:
            group_id = "group"
            mention = "@Hermes"
            bot_wxid = "wxid_bot"
            _decompressor = self.chat_api.zstandard.ZstdDecompressor()

        reader = FakeReader()
        reader._serialize_row = (
            self.chat_api.SnapshotReader._serialize_row.__get__(
                reader,
                FakeReader,
            )
        )
        return reader._serialize_row(row)

    def message(
        self,
        local_id,
        prompt="do work",
        *,
        native_mention=True,
        sender="wxid_member",
    ):
        message = {
            "group_id": "group",
            "local_id": local_id,
            "server_id": "server-%d" % local_id,
            "msg_svr_id": "server-%d" % local_id,
            "sort_seq": local_id * 10,
            "timestamp": 100000 + local_id,
            "direction": "incoming",
            "origin_source": 2,
            "local_type": 1,
            "message_type": "text",
            "structured_valid": True,
            "mentions_bot": True,
            "reply_to_bot": False,
            "text": prompt,
            "prompt": prompt,
            "sender_wxid": sender,
        }
        if native_mention:
            message.update(
                {
                    "native_mentions_bot": True,
                    "mention_source": "native_at_user_list",
                    "at_user_list": ["wxid_bot"],
                }
            )
        return message

    def pages(self, messages):
        def get_page(after):
            return [
                item
                for item in messages
                if int(item["local_id"]) > int(after)
            ][:200]

        return get_page

    def test_stop_invalidates_pending_retry_and_unsubmitted_old_messages(self):
        old = self.message(11, "old work")
        unsubmitted = self.message(12, "another old task")
        stop = self.message(13, "\u505c\u6b62\uff1f", native_mention=False)
        future = self.message(14, "new work")
        messages = [old, unsubmitted, stop, future]
        state = {
            "last_local_id": 10,
            "retry": {
                "local_id": 11,
                "attempts": 1,
                "next_retry_at": 9999999999,
                "phase": "processing",
            },
            "pending": {
                "local_id": 11,
                "result": {
                    "kind": "text",
                    "text": "obsolete answer",
                    "chunks": ["obsolete answer"],
                },
            },
        }
        ai_calls = []
        sent = []

        def ask(message, prompt, *, timeout=None):
            ai_calls.append((message["local_id"], prompt, timeout))
            if message["local_id"] == 13:
                return {"text": "stopped", "status": "canceled"}
            return "fresh answer"

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=ask,
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(
                (text, request_id, metadata)
            ),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual([call[0] for call in ai_calls], [13, 14])
        self.assertEqual([item[0] for item in sent], ["stopped", "fresh answer"])
        self.assertNotIn("obsolete answer", [item[0] for item in sent])
        self.assertEqual(state["stop_before_local_id"], 13)
        self.assertEqual(state["last_local_id"], 14)
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["retry"])

    def test_fresh_batch_scans_stop_before_submitting_older_ordinary_message(self):
        old = self.message(11, "old work")
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        messages = [old, stop]
        state = {"last_local_id": 10, "retry": None, "pending": None}
        calls = []
        sent = []

        def ask(message, prompt, *, timeout=None):
            calls.append(message["local_id"])
            return {"text": "stopped", "status": "canceled"}

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=ask,
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(text),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual(calls, [12])
        self.assertEqual(sent, ["stopped"])
        self.assertEqual(state["stop_before_local_id"], 12)
        self.assertEqual(state["last_local_id"], 12)

    def test_stop_invalidates_persisted_failure_notice_before_send(self):
        failed = self.message(11, "old failed work")
        stop = self.message(12, "\u522b\u53d1\u4e86\uff01", native_mention=False)
        messages = [failed, stop]
        state = {
            "last_local_id": 10,
            "retry": {
                "local_id": 11,
                "attempts": self.bridge.MAX_RETRIES,
                "next_retry_at": 0,
                "phase": "failure_notice",
            },
            "pending": None,
        }
        sent = []

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "stopped", "status": "canceled"},
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(
                (text, request_id)
            ),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual(sent, [("stopped", mock.ANY)])
        self.assertFalse(any(request_id.startswith("failure:") for _, request_id in sent))
        self.assertEqual(state["last_local_id"], 12)
        self.assertIsNone(state["retry"])

    def test_control_scan_pages_beyond_one_thousand_without_truncation(self):
        messages = [
            self.message(index, "ordinary", native_mention=False)
            for index in range(1, 1305)
        ]
        stop = self.message(1305, "\u5168\u90e8\u53d6\u6d88", native_mention=False)
        messages.append(stop)
        state = {"last_local_id": 0, "retry": None, "pending": None}
        page_calls = []

        def get_page(after):
            page_calls.append(int(after))
            return self.pages(messages)(after)

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=get_page,
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "stopped", "status": "canceled"},
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            caught_up = self.bridge.process_priority_controls(state, 0)

        self.assertTrue(caught_up)
        self.assertGreaterEqual(len(page_calls), 7)
        ask.assert_called_once()
        self.assertEqual(state["stop_before_local_id"], 1305)
        self.assertEqual(state["control_scan_cursor"], 1305)

    def test_time_bounded_control_scan_resumes_from_persisted_cursor(self):
        messages = [
            self.message(index, "ordinary", native_mention=False)
            for index in range(1, 451)
        ]
        stop = self.message(451, "\u505c\u6b62", native_mention=False)
        messages.append(stop)
        state = {"last_local_id": 0, "retry": None, "pending": None}
        self.bridge.CONTROL_SCAN_SECONDS = 0.0001

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge.time,
            "monotonic",
            side_effect=[0.0, 1.0, 2.0, 3.0, 4.0],
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "stopped", "status": "canceled"},
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            first = self.bridge.process_priority_controls(state, 0)
            first_cursor = state["control_scan_cursor"]
            second = self.bridge.process_priority_controls(state, 0)
            second_cursor = state["control_scan_cursor"]
            third = self.bridge.process_priority_controls(state, 0)

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertEqual(first_cursor, 200)
        self.assertEqual(second_cursor, 400)
        self.assertEqual(state["control_scan_cursor"], 451)
        ask.assert_called_once()

    def test_failed_stop_control_blocks_persisted_pending_delivery(self):
        old = self.message(11, "old work")
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        messages = [old, stop]
        state = {
            "last_local_id": 10,
            "retry": {
                "local_id": 11,
                "attempts": 1,
                "next_retry_at": 0,
                "phase": "processing",
            },
            "pending": {
                "local_id": 11,
                "result": {
                    "kind": "text",
                    "text": "must wait",
                    "chunks": ["must wait"],
                },
            },
        }

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=RuntimeError("adapter unavailable"),
        ), mock.patch.object(
            self.bridge,
            "send_text",
        ) as send, mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        send.assert_not_called()
        self.assertEqual(state["last_local_id"], 10)
        self.assertIsNotNone(state["pending"])
        self.assertEqual(state["control_scan_cursor"], 11)

    def test_missing_sender_fails_closed_and_never_becomes_unknown(self):
        message = self.message(11)
        message["sender_wxid"] = ""
        message["sender_numeric_id"] = 0
        self.assertEqual(self.bridge.message_sender_id(message), "")
        self.assertFalse(self.bridge.should_handle(message))
        with self.assertRaisesRegex(ValueError, "sender identity is missing"):
            self.bridge.ask_ai(message, "do work")

    def test_production_serializer_emits_trusted_native_mention_contract(self):
        message = self.serialized_message(11, "real task")

        self.assertEqual(message["msg_svr_id"], "server-11")
        self.assertTrue(message["native_mentions_bot"])
        self.assertEqual(message["native_at_user_list"], ["wxid_bot"])
        self.assertEqual(message["mention_source"], "msg_source_at_user_list")
        self.assertTrue(self.bridge.trusted_mentions_bot(message))
        self.assertTrue(self.bridge.should_handle(message))

    def test_three_serialized_native_mentions_are_each_processed_once(self):
        messages = [
            self.serialized_message(local_id, "same")
            for local_id in (11, 12, 13)
        ]
        state = {"last_local_id": 10, "retry": None, "pending": None}
        calls = []
        sent = []

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=lambda message, prompt, **kwargs: calls.append(
                (message["local_id"], prompt)
            )
            or "answer",
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(
                request_id
            ),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual(calls, [(11, "same"), (12, "same"), (13, "same")])
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(set(sent)), 3)
        self.assertTrue(all(value.startswith("reply:") for value in sent))
        self.assertEqual(state["last_local_id"], 13)

    def test_command_normalization_matches_adapter_punctuation_semantics(self):
        cases = {
            "\u505c\u6b62\uff1f": "cancel_all",
            "\u522b\u53d1\u4e86!  ": "cancel_all",
            "\u53d6\u6d88 T-12AB34CD\uff01": "cancel",
            "\u4efb\u52a1 T-12AB34CD\u3002": "status",
            "\u53ea\u8981\u6587\u5b57\uff1f": "media_only",
            "\u4fee\u6539 T-12AB34CD \u65b0\u8981\u6c42\uff01": "modify",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                message = self.message(11, text, native_mention=False)
                self.assertEqual(
                    self.bridge.control_command_kind(message),
                    expected,
                )
                self.assertTrue(self.bridge.is_control_message(message))
        self.assertFalse(
            self.bridge.is_control_message(
                self.message(11, "\u505c\u6b62\u5427", native_mention=False)
            )
        )

    def test_alias_changes_keep_request_identity_and_delivery_idempotency(self):
        first = self.message(11, "same real message")
        first.update({"sort_seq": 700, "timestamp": 900})
        alias = dict(first)
        alias.update(
            {
                "local_id": 99,
                "server_id": "new-server-alias",
                "msg_svr_id": "new-server-alias",
            }
        )
        self.assertEqual(
            self.bridge.message_request_id(first),
            self.bridge.message_request_id(alias),
        )
        self.assertEqual(
            self.bridge.message_delivery_key(first),
            self.bridge.message_delivery_key(alias),
        )
        state = {"last_local_id": 10, "retry": None, "pending": None}
        sent = []
        with mock.patch.object(
            self.bridge,
            "get_messages",
            return_value=[],
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value="one answer",
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(request_id),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.handle_message(state, first)
            self.bridge.handle_message(state, alias)

        ask.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("reply:"))
        self.assertEqual(state["last_local_id"], 99)

    def test_display_text_mention_is_not_trusted_without_native_evidence(self):
        display_only = self.message(11, "@Hermes do work", native_mention=False)
        display_only["mentions_bot"] = True
        self.assertFalse(self.bridge.trusted_mentions_bot(display_only))
        self.assertFalse(self.bridge.should_handle(display_only))

        body_injection = dict(display_only)
        body_injection["text"] = (
            "<msgsource><atuserlist>wxid_bot</atuserlist></msgsource>"
        )
        body_injection["prompt"] = body_injection["text"]
        self.assertFalse(self.bridge.trusted_mentions_bot(body_injection))
        self.assertFalse(self.bridge.should_handle(body_injection))

    def test_group_listener_forwards_unmentioned_structured_text(self):
        message = self.message(
            11,
            "这个话题到底怎么搞？",
            native_mention=False,
        )
        message["mentions_bot"] = False
        self.bridge.GROUP_LISTENER_ENABLED = True

        self.assertTrue(self.bridge.should_handle(message))

        state = {"last_local_id": 10, "retry": None, "pending": None}
        sent = []
        with mock.patch.object(
            self.bridge,
            "get_messages",
            return_value=[message],
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "", "status": "ignored"},
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda *args, **kwargs: sent.append((args, kwargs)),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        ask.assert_called_once_with(message, "这个话题到底怎么搞？")
        self.assertEqual(sent, [])
        self.assertEqual(state["last_local_id"], 11)

    def test_group_listener_waits_for_invalid_structured_metadata(self):
        message = self.message(11, "普通聊天", native_mention=False)
        message.update({"mentions_bot": False, "structured_valid": False})
        self.bridge.GROUP_LISTENER_ENABLED = True
        state = {"last_local_id": 10, "retry": None, "pending": None}

        self.assertTrue(
            self.bridge.should_wait_for_structured_metadata(state, message, now=100)
        )
        self.assertEqual(state["metadata_wait"]["local_id"], 11)

    def test_group_listener_keeps_native_mention_settlement_window(self):
        message = self.message(11, "@Hermes 你看看", native_mention=False)
        message.update(
            {
                "mentions_bot": False,
                "visible_mention_candidate": True,
                "structured_valid": True,
            }
        )
        self.bridge.GROUP_LISTENER_ENABLED = True
        state = {"last_local_id": 10, "retry": None, "pending": None}

        self.assertTrue(
            self.bridge.should_wait_for_structured_metadata(state, message, now=100)
        )
        self.assertEqual(state["metadata_wait"]["local_id"], 11)

    def test_delayed_native_mention_metadata_is_processed_once(self):
        incomplete = self.message(11, "do work", native_mention=False)
        incomplete.update(
            {
                "mentions_bot": False,
                "visible_mention_candidate": True,
                "mention_source": "",
            }
        )
        complete = self.message(11, "do work")
        complete["visible_mention_candidate"] = True
        state = {"last_local_id": 10, "retry": None, "pending": None}
        snapshots = iter(([incomplete], [complete], []))
        sent = []

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=lambda after: next(snapshots),
        ), mock.patch.object(
            self.bridge,
            "process_priority_controls",
            return_value=True,
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value="answer",
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(request_id),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)
            self.assertEqual(state["last_local_id"], 10)
            ask.assert_not_called()

            self.bridge.run_once(state)
            self.bridge.run_once(state)

        ask.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(state["last_local_id"], 11)

    def test_visible_mention_without_native_metadata_expires_without_trigger(self):
        display_only = self.message(11, "do work", native_mention=False)
        display_only.update(
            {
                "mentions_bot": False,
                "visible_mention_candidate": True,
                "mention_source": "",
            }
        )
        state = {"last_local_id": 10, "retry": None, "pending": None}

        with mock.patch.object(
            self.bridge,
            "get_messages",
            return_value=[display_only],
        ), mock.patch.object(
            self.bridge,
            "process_priority_controls",
            return_value=True,
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
        ) as send, mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)
            self.assertEqual(state["last_local_id"], 10)
            state["metadata_wait"]["expires_at"] = 0
            self.bridge.run_once(state)

        ask.assert_not_called()
        send.assert_not_called()
        self.assertEqual(state["last_local_id"], 11)
        self.assertIsNone(state["metadata_wait"])

    def test_stop_bypasses_metadata_wait_and_invalidates_older_candidate(self):
        display_only = self.message(11, "do work", native_mention=False)
        display_only.update(
            {
                "mentions_bot": False,
                "visible_mention_candidate": True,
                "mention_source": "",
            }
        )
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        state = {"last_local_id": 10, "retry": None, "pending": None}
        sent = []

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages([display_only, stop]),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "stopped", "status": "canceled"},
        ) as ask, mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(text),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        ask.assert_called_once()
        self.assertEqual(sent, ["stopped"])
        self.assertEqual(state["last_local_id"], 12)
        self.assertIsNone(state.get("metadata_wait"))

    def test_delayed_native_metadata_race_repeats_without_loss(self):
        state = {"last_local_id": 10, "retry": None, "pending": None}
        sent = []

        with mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value="answer",
        ) as ask, mock.patch.object(
            self.bridge,
            "process_priority_controls",
            return_value=True,
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(request_id),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            for local_id in range(11, 111):
                incomplete = self.message(local_id, "same", native_mention=False)
                incomplete.update(
                    {
                        "mentions_bot": False,
                        "visible_mention_candidate": True,
                        "mention_source": "",
                    }
                )
                complete = self.message(local_id, "same")
                complete["visible_mention_candidate"] = True

                self.assertTrue(
                    self.bridge.should_wait_for_structured_metadata(
                        state,
                        incomplete,
                        now=float(local_id),
                    )
                )
                self.assertFalse(
                    self.bridge.should_wait_for_structured_metadata(
                        state,
                        complete,
                        now=float(local_id),
                    )
                )
                self.bridge.handle_message(state, complete)
                self.bridge.handle_message(state, complete)

        self.assertEqual(ask.call_count, 100)
        self.assertEqual(len(sent), 100)
        self.assertEqual(len(set(sent)), 100)
        self.assertEqual(state["last_local_id"], 110)

    def test_native_at_metadata_and_real_reply_are_trusted(self):
        native = self.message(11, "do work", native_mention=False)
        native.update(
            {
                "mentions_bot": False,
                "msg_source": (
                    "<msgsource><atuserlist>wxid_other,wxid_bot"
                    "</atuserlist></msgsource>"
                ),
            }
        )
        self.assertTrue(self.bridge.trusted_mentions_bot(native))
        self.assertTrue(self.bridge.should_handle(native))

        reply = self.message(12, "continue", native_mention=False)
        reply.update(
            {
                "local_type": 49,
                "message_type": "quoted_reply",
                "mentions_bot": False,
                "reply_to_bot": True,
                "reply_reference": {
                    "sender_wxid": "wxid_bot",
                    "content": "previous answer",
                },
            }
        )
        self.assertTrue(self.bridge.trusted_reply_to_bot(reply))
        self.assertTrue(self.bridge.should_handle(reply))

    def test_priority_scan_defers_status_until_after_stop_scan(self):
        status = self.message(11, "\u4efb\u52a1", native_mention=False)
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        state = {"last_local_id": 10, "retry": None, "pending": None}
        calls = []

        def ask(message, prompt, *, timeout=None):
            calls.append(message["local_id"])
            if message["local_id"] == 12:
                return {"text": "stopped", "status": "canceled"}
            return {"text": "task list", "status": "succeeded"}

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages([status, stop]),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=ask,
        ), mock.patch.object(
            self.bridge,
            "send_text",
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.assertTrue(self.bridge.process_priority_controls(state, 10))

        self.assertEqual(calls, [12])

    def test_run_once_scans_future_stop_before_direct_status_reply(self):
        status = self.message(11, "\u4efb\u52a1", native_mention=False)
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        messages = [status, stop]
        state = {"last_local_id": 10, "retry": None, "pending": None}
        calls = []
        sent = []

        def ask(message, prompt, *, timeout=None):
            calls.append(message["local_id"])
            if message["local_id"] == 12:
                return {"text": "stopped", "status": "canceled"}
            return {"text": "task list", "status": "succeeded"}

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=ask,
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(text),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual(calls, [12, 11])
        self.assertEqual(sent, ["stopped", "task list"])
        self.assertEqual(state["last_local_id"], 12)

    def test_stop_beyond_first_page_precedes_earlier_status_delivery(self):
        status = self.message(11, "\u4efb\u52a1", native_mention=False)
        ordinary = [
            self.message(index, "ordinary", native_mention=False)
            for index in range(12, 251)
        ]
        stop = self.message(251, "\u505c\u6b62", native_mention=False)
        messages = [status, *ordinary, stop]
        state = {"last_local_id": 10, "retry": None, "pending": None}
        calls = []
        sent = []

        def ask(message, prompt, *, timeout=None):
            calls.append(message["local_id"])
            if message["local_id"] == 251:
                return {"text": "stopped", "status": "canceled"}
            return {"text": "task list", "status": "succeeded"}

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages(messages),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            side_effect=ask,
        ), mock.patch.object(
            self.bridge,
            "send_text",
            side_effect=lambda text, request_id, **metadata: sent.append(text),
        ), mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        self.assertEqual(calls, [251, 11])
        self.assertEqual(sent, ["stopped", "task list"])
        self.assertEqual(state["stop_before_local_id"], 251)

    def test_uncommitted_stop_status_blocks_persisted_pending_delivery(self):
        old = self.message(11, "old work")
        stop = self.message(12, "\u505c\u6b62", native_mention=False)
        state = {
            "last_local_id": 10,
            "retry": {
                "local_id": 11,
                "attempts": 1,
                "next_retry_at": 0,
                "phase": "processing",
            },
            "pending": {
                "local_id": 11,
                "result": {
                    "kind": "text",
                    "text": "must wait",
                    "chunks": ["must wait"],
                },
            },
        }

        with mock.patch.object(
            self.bridge,
            "get_messages",
            side_effect=self.pages([old, stop]),
        ), mock.patch.object(
            self.bridge,
            "ask_ai",
            return_value={"text": "not stopped", "status": "failed"},
        ), mock.patch.object(
            self.bridge,
            "send_text",
        ) as send, mock.patch.object(self.bridge, "atomic_save_state"):
            self.bridge.run_once(state)

        send.assert_not_called()
        self.assertNotIn("stop_before_local_id", state)
        self.assertIsNotNone(state["pending"])
        self.assertEqual(state["control_scan_cursor"], 11)

    def test_recent_context_is_newest_first_bounded_and_ordered(self):
        messages = [
            {
                "local_id": index,
                "local_type": 1,
                "sender_wxid": "wxid_%d" % index,
                "direction": "incoming",
                "message_type": "text",
                "text": "marker-%02d:" % index + ("x" * 2_000),
            }
            for index in range(1, 13)
        ]
        requested = []

        def fake_api_request(_method, path, **_kwargs):
            requested.append(path)
            return {"messages": messages}

        with mock.patch.object(
            self.bridge,
            "api_request",
            side_effect=fake_api_request,
        ):
            context = self.bridge.get_recent_context(99)

        self.assertIn("before=99&limit=8", requested[0])
        self.assertEqual(len(context), 8)
        self.assertEqual(context[0]["local_id"], 5)
        self.assertEqual(context[-1]["local_id"], 12)
        self.assertTrue(all(len(item["text"]) <= 1200 for item in context))
        self.assertLessEqual(sum(len(item["text"]) for item in context), 9600)


if __name__ == "__main__":
    unittest.main()
