#!/usr/bin/env python3
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = {}
LEGACY_CONFIG_PATH = ROOT / "config.json"
if LEGACY_CONFIG_PATH.exists():
    CONFIG.update(json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8")))
DB_CONFIG_PATH = ROOT / "db-config.json"
if DB_CONFIG_PATH.exists():
    CONFIG.update(json.loads(DB_CONFIG_PATH.read_text(encoding="utf-8")))
STATE_PATH = ROOT / "db-state.json"
STATE_BACKUP_PATH = ROOT / "db-state.backup.json"
STATE_MARKER_PATH = ROOT / ".db-state-initialized"
PENDING_DIR = ROOT / ".db-pending"
CHAT_API_URL = CONFIG.get("chat_api_url", "http://127.0.0.1:8765").rstrip("/")
CHAT_API_TOKEN = str(
    os.environ.get("WECHAT_CHAT_API_TOKEN")
    or CONFIG.get("chat_api_token")
    or ""
).strip()
GROUP_ID = CONFIG.get("chat_group_id", "00000000000@chatroom")
POLL_SECONDS = max(0.05, float(CONFIG.get("chat_api_poll_seconds", 0.25)))
MAX_RETRIES = min(3, max(1, int(CONFIG.get("chat_api_max_retries", 3))))
TEXT_CHUNK_CHARS = max(100, int(CONFIG.get("chat_api_text_chunk_chars", 1500)))
CONTEXT_MESSAGES = min(
    16,
    max(1, int(CONFIG.get("chat_context_messages", 16))),
)
CONTEXT_MESSAGE_CHARS = min(
    1200,
    max(100, int(CONFIG.get("chat_context_message_chars", 1200))),
)
CONTEXT_TOTAL_CHARS = min(
    19200,
    max(100, int(CONFIG.get("chat_context_total_chars", 19200))),
)
AI_API_URL = str(
    os.environ.get("HERMES_WECHAT_ADAPTER_URL")
    or CONFIG.get("api_url")
    or "http://127.0.0.1:8000/api/chat"
).rstrip("/")
if not AI_API_URL.endswith("/api/chat"):
    AI_API_URL += "/api/chat"
AI_API_TOKEN = str(
    os.environ.get("BRIDGE_TOKEN")
    or CONFIG.get("bridge_token")
    or ""
).strip()
AI_API_TIMEOUT = int(
    os.environ.get("HERMES_WECHAT_ADAPTER_TIMEOUT_SECONDS")
    or CONFIG.get("api_timeout_seconds")
    or 210
)
CONTROL_API_TIMEOUT = max(
    1,
    int(CONFIG.get("chat_control_api_timeout_seconds", 8)),
)
CONTROL_SCAN_SECONDS = max(
    0.05,
    float(CONFIG.get("chat_control_scan_seconds", 1.0)),
)
STRUCTURED_METADATA_WAIT_SECONDS = max(
    0.0,
    min(
        10.0,
        float(CONFIG.get("chat_structured_metadata_wait_seconds", 2.0)),
    ),
)


def config_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


_GROUP_LISTENER_ENV = os.environ.get("HERMES_WECHAT_GROUP_LISTENER_ENABLED")
GROUP_LISTENER_ENABLED = (
    config_flag(_GROUP_LISTENER_ENV)
    if _GROUP_LISTENER_ENV is not None
    else config_flag(CONFIG.get("chat_group_listener_enabled", False))
)
PROCESSED_IDENTITY_LIMIT = max(
    256,
    min(8192, int(CONFIG.get("chat_processed_identity_limit", 2048))),
)
BOT_WXID = str(
    CONFIG.get("bot_wxid")
    or CONFIG.get("wechat_bot_wxid")
    or os.environ.get("WECHAT_BOT_WXID")
    or ""
).strip()

# Chat API deployments have used a few field names while the underlying
# WeChat schema evolved.  Normalize them once at the Bridge boundary so
# routing, native @ matching, idempotency, and logging all see the same IDs.
SENDER_ID_FIELDS = (
    "sender_id",
    "sender_wxid",
    "sender_numeric_id",
    "real_sender_id",
    "from_user_id",
    "from_user",
    "sender",
)
ROOM_ID_FIELDS = (
    "room_id",
    "group_id",
    "chatroom_id",
    "chat_room_id",
    "conversation_id",
    "talker_id",
)
SERVER_ID_FIELDS = (
    "msg_svr_id",
    "server_id",
    "msg_server_id",
    "msgsvrid",
    "svr_id",
)
LOCAL_ID_FIELDS = (
    "source_local_id",
    "local_id",
    "msg_local_id",
    "message_local_id",
)
_EMPTY_IDENTIFIER_VALUES = frozenset(
    {"", "0", "unknown", "none", "null", "nil", "n/a"}
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "self", "bot"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off", "none", "null"})

LOG = logging.getLogger("wechat-db-bridge")
EMPTY_REPLY_FALLBACK = (
    "\u521a\u624dAI\u6ca1\u6709\u751f\u6210\u6709\u6548\u5185\u5bb9\uff0c"
    "\u9ebb\u70e6\u6362\u4e2a\u8bf4\u6cd5\u518d\u8bd5\u4e00\u6b21\u3002"
)
# Older Chat API releases embedded request correlation data in zero-width
# characters.  A rolling deployment can leave that data in a persisted
# pending result, so the Bridge sanitizes at both recovery and send time.
INTERNAL_FORMAT_CHARS = (
    "\u00ad\u061c\u200b\u200c\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2065\u2066\u2067\u2068\u2069"
    "\u206a\u206b\u206c\u206d\u206e\u206f\ufeff"
)
INTERNAL_FORMAT_TRANSLATION = str.maketrans(
    "",
    "",
    INTERNAL_FORMAT_CHARS,
)
LOW_INFORMATION_REPLY_KEYS = frozenset(
    {
        "\u55ef\u6765\u4e86",
        "\u55ef\u6211\u6765\u4e86",
        "\u6211\u6765\u4e86",
        "\u55ef\u6765\u5566",
        "\u6211\u6765\u5566",
        "\u6765\u4e86",
        "\u55ef\u5728",
        "\u6211\u5728",
        "\u5728\u5462",
        "\u5728\u7684",
        "\u5230\u5566",
        "\u6211\u5230\u5566",
    }
)
LOW_INFORMATION_REPLY_RE = re.compile(
    r"^(?:\u55ef+)?(?:\u6211)?(?:\u6765(?:\u4e86|\u5566)?|\u5728(?:\u5462|\u7684)?|\u5230(?:\u4e86|\u5566)?)(?:\u5440|\u554a|\u5462|\u54e6|\u5582)?$",
    re.IGNORECASE,
)
CONTROL_COMMAND_RE = re.compile(
    r"^(?:"
    r"\u4efb\u52a1(?:\s+T-[A-Fa-f0-9]{8})?|"
    r"\u53d6\u6d88(?:\s+T-[A-Fa-f0-9]{8})?|"
    r"\u91cd\u8bd5(?:\s+T-[A-Fa-f0-9]{8})?|"
    r"(?:\u8865\u5145|\u4fee\u6539)\s+T-[A-Fa-f0-9]{8}\s+.+|"
    r"\u505c|\u505c\u6b62|\u505c\u4e0b\u6765|\u505c\u4e00\u4e0b|"
    r"\u522b\u53d1\u4e86|\u522b\u518d\u53d1\u4e86|"
    r"\u4e0d\u8981\u53d1\u4e86|\u4e0d\u8981\u518d\u53d1\u4e86|"
    r"\u505c\u6b62\u53d1\u9001|\u5168\u90e8\u53d6\u6d88|"
    r"\u4e0d\u8981\u56fe\u7247|\u4e0d\u8981\u53d1\u56fe\u7247|"
    r"\u4e0d\u8981\u518d\u53d1\u56fe\u7247|\u522b\u53d1\u56fe|"
    r"\u522b\u518d\u53d1\u56fe|\u522b\u53d1\u56fe\u7247|"
    r"\u522b\u518d\u53d1\u56fe\u7247|\u505c\u6b62\u53d1\u56fe|"
    r"\u505c\u6b62\u53d1\u9001\u56fe\u7247|\u53ea\u8981\u6587\u5b57"
    r")$",
    re.DOTALL,
)
CONTROL_SUFFIX_RE = re.compile(r"[\s\u3002\uff01\uff1f!?]+$")
STOP_ALL_COMMANDS = {
    "\u505c",
    "\u505c\u6b62",
    "\u505c\u4e0b\u6765",
    "\u505c\u4e00\u4e0b",
    "\u522b\u53d1\u4e86",
    "\u522b\u518d\u53d1\u4e86",
    "\u4e0d\u8981\u53d1\u4e86",
    "\u4e0d\u8981\u518d\u53d1\u4e86",
    "\u505c\u6b62\u53d1\u9001",
    "\u5168\u90e8\u53d6\u6d88",
}
MEDIA_ONLY_COMMANDS = {
    "\u4e0d\u8981\u56fe\u7247",
    "\u4e0d\u8981\u53d1\u56fe\u7247",
    "\u4e0d\u8981\u518d\u53d1\u56fe\u7247",
    "\u522b\u53d1\u56fe",
    "\u522b\u518d\u53d1\u56fe",
    "\u522b\u53d1\u56fe\u7247",
    "\u522b\u518d\u53d1\u56fe\u7247",
    "\u505c\u6b62\u53d1\u56fe",
    "\u505c\u6b62\u53d1\u9001\u56fe\u7247",
    "\u53ea\u8981\u6587\u5b57",
}
PREPROCESSED_CONTROL_IDS = set()
PREPROCESSED_CONTROL_LOCK = threading.Lock()


class RemoteAPIError(RuntimeError):
    def __init__(self, service, *, status=None, kind="request_failed"):
        self.service = str(service)
        self.status = int(status) if status is not None else None
        self.kind = str(kind)
        if self.status is None:
            message = "%s %s" % (self.service, self.kind)
        else:
            message = "%s returned HTTP %d" % (self.service, self.status)
        super().__init__(message)


def safe_error_summary(exc):
    if isinstance(exc, RemoteAPIError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTPError status=%d" % int(exc.code)
    if isinstance(exc, urllib.error.URLError):
        return "URLError reason_type=%s" % type(exc.reason).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        return "%s errno=%d" % (type(exc).__name__, int(exc.errno))
    return type(exc).__name__


def read_json_response(response, service):
    try:
        data = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RemoteAPIError(service, kind="returned invalid JSON") from None
    if not isinstance(data, dict):
        raise RemoteAPIError(service, kind="returned a non-object response")
    return data


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_save_state(state):
    atomic_write_json(STATE_PATH, state)
    atomic_write_json(STATE_BACKUP_PATH, state)
    atomic_write_text(STATE_MARKER_PATH, "initialized\n")


def atomic_write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def validate_state(value):
    if not isinstance(value, dict):
        raise ValueError("bridge state must be a JSON object")
    state = dict(value)
    state["last_local_id"] = int(state.get("last_local_id", 0))
    if state["last_local_id"] < 0:
        raise ValueError("bridge cursor cannot be negative")
    state["cursor_ready"] = bool(state.get("cursor_ready", False))
    if state.get("retry") is not None and not isinstance(state["retry"], dict):
        raise ValueError("bridge retry state must be an object or null")
    if state.get("pending") is not None and not isinstance(state["pending"], dict):
        raise ValueError("bridge pending state must be an object or null")
    state.setdefault("retry", None)
    state.setdefault("pending", None)
    metadata_wait = state.get("metadata_wait")
    if "metadata_wait" in state and metadata_wait is not None:
        if not isinstance(metadata_wait, dict):
            raise ValueError("bridge metadata wait state must be an object or null")
        metadata_wait = dict(metadata_wait)
        metadata_wait["local_id"] = int(metadata_wait.get("local_id") or 0)
        metadata_wait["started_at"] = float(metadata_wait.get("started_at") or 0)
        metadata_wait["expires_at"] = float(metadata_wait.get("expires_at") or 0)
        if metadata_wait["local_id"] <= 0:
            raise ValueError("bridge metadata wait local id must be positive")
        if metadata_wait["started_at"] < 0 or metadata_wait["expires_at"] < 0:
            raise ValueError("bridge metadata wait timestamps cannot be negative")
        if metadata_wait["expires_at"] < metadata_wait["started_at"]:
            raise ValueError("bridge metadata wait deadline is invalid")
        state["metadata_wait"] = metadata_wait
    state.setdefault("last_reply_at", 0)
    if "stop_before_local_id" in state:
        state["stop_before_local_id"] = int(state["stop_before_local_id"])
        if state["stop_before_local_id"] < 0:
            raise ValueError("bridge stop cursor cannot be negative")
    if "control_scan_cursor" in state:
        state["control_scan_cursor"] = int(state["control_scan_cursor"])
        if state["control_scan_cursor"] < 0:
            raise ValueError("bridge control scan cursor cannot be negative")
    if "processed_identities" in state:
        if not isinstance(state["processed_identities"], list):
            raise ValueError("processed identities must be a list")
        if not all(isinstance(item, str) for item in state["processed_identities"]):
            raise ValueError("processed identities must contain strings")
    return state


def persisted_state_is_current(state):
    if not STATE_MARKER_PATH.exists():
        return False
    for path in (STATE_PATH, STATE_BACKUP_PATH):
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if persisted != state:
            return False
    return True


def load_state():
    errors = []
    for path in (STATE_PATH, STATE_BACKUP_PATH):
        try:
            state = validate_state(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        except Exception as exc:
            errors.append("%s: %s" % (path.name, exc))
            continue
        if path == STATE_BACKUP_PATH:
            LOG.warning("restoring bridge state from %s", path)
        if path == STATE_BACKUP_PATH or not persisted_state_is_current(state):
            atomic_save_state(state)
        return state
    if errors or STATE_MARKER_PATH.exists():
        raise RuntimeError(
            "bridge state is missing or corrupt; refusing to skip pending messages: %s"
            % ("; ".join(errors) or "no state or backup file exists")
        )
    return validate_state(
        {
            "cursor_ready": False,
            "last_local_id": 0,
            "retry": None,
            "pending": None,
            "metadata_wait": None,
            "last_reply_at": 0,
            "stop_before_local_id": 0,
            "control_scan_cursor": 0,
            "processed_identities": [],
        }
    )


def api_request(method, path, payload=None, timeout=20):
    data = None
    headers = {"Accept": "application/json"}
    if CHAT_API_TOKEN:
        headers["Authorization"] = "Bearer " + CHAT_API_TOKEN
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        CHAT_API_URL + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return read_json_response(response, "chat API")
    except urllib.error.HTTPError as exc:
        raise RemoteAPIError("chat API", status=exc.code) from None


def group_messages_path():
    return "/groups/%s/messages" % urllib.parse.quote(GROUP_ID, safe="")


def get_health():
    return api_request("GET", "/health", timeout=10)


def get_messages(after):
    path = "%s?after=%d&limit=200" % (group_messages_path(), int(after))
    return api_request("GET", path, timeout=15).get("messages", [])


def normalize_identifier(value):
    """Return one canonical, comparison-safe form for trusted IDs."""
    if value is None or isinstance(value, bool):
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).replace("\x00", "")
    normalized = normalized.strip().casefold()
    if normalized in _EMPTY_IDENTIFIER_VALUES:
        return ""
    if re.fullmatch(r"[+-]?\d+", normalized):
        try:
            numeric = int(normalized, 10)
        except ValueError:
            return ""
        return str(numeric) if numeric > 0 else ""
    return normalized


def message_identifier(message, fields):
    for field in fields:
        value = normalize_identifier(message.get(field))
        if value:
            return value
    return ""


def message_integer(message, fields, default=0):
    for field in fields:
        raw = message.get(field)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return int(str(raw).strip(), 10)
        except (TypeError, ValueError):
            continue
    return int(default)


def message_direction(message):
    raw = message.get("direction")
    if raw is None:
        raw = message.get("message_direction")
    value = unicodedata.normalize("NFKC", str(raw or "")).strip().casefold()
    if value in {"incoming", "inbound", "received"}:
        return "incoming"
    if value in {"outgoing", "outbound", "sent", "self"}:
        return "outgoing"
    return "unknown"


def message_origin_source(message):
    return message_integer(message, ("origin_source", "origin", "source_type"))


def message_flag(message, *fields):
    for field in fields:
        raw = message.get(field)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        value = unicodedata.normalize("NFKC", str(raw)).strip().casefold()
        if value in _TRUE_VALUES:
            return True
        if value in _FALSE_VALUES:
            return False
    return False


def message_local_id(message):
    value = message_integer(message, LOCAL_ID_FIELDS)
    return value if value > 0 else 0


def message_is_real_inbound(message):
    """Accept only a structured non-self inbound WeChat message.

    The Bridge treats all fields as Chat API metadata, never as user text. A
    partial old serializer is accepted when it provides either a positive
    incoming direction or the canonical inbound origin value, while every
    explicit outbound/self signal wins and rejects the record.
    """
    direction = message_direction(message)
    origin_source = message_origin_source(message)
    sender_id = message_sender_id(message)
    bot_id = normalize_identifier(BOT_WXID)
    if direction == "outgoing" or origin_source == 1:
        return False
    if message_flag(message, "is_bot", "is_self", "from_self", "self_sent"):
        return False
    if bot_id and sender_id and hmac.compare_digest(sender_id, bot_id):
        return False
    if direction not in {"incoming", "unknown"}:
        return False
    if origin_source not in {0, 2}:
        return False
    return bool(direction == "incoming" or origin_source == 2)


def get_recent_context(local_id):
    path = "%s?before=%d&limit=%d" % (
        group_messages_path(),
        int(local_id),
        CONTEXT_MESSAGES,
    )
    messages = api_request("GET", path, timeout=15).get("messages", [])
    candidates = []
    for item in messages:
        text = strip_internal_format_chars(item.get("text") or "").strip()
        if int(item.get("local_type", 0)) not in {1, 49} or not text:
            continue
        # Context is an incoming human transcript. This blocks records that
        # merely claim to be group traffic while carrying the bot's identity.
        if not message_is_real_inbound(item):
            continue
        candidates.append((item, text))
    context = []
    remaining = CONTEXT_TOTAL_CHARS
    for item, text in reversed(candidates):
        if len(context) >= CONTEXT_MESSAGES or remaining <= 0:
            break
        bounded = text[: min(CONTEXT_MESSAGE_CHARS, remaining)]
        context.append({
            "local_id": message_local_id(item),
            "sender_id": message_sender_id(item),
            "sender_name": str(item.get("sender_name") or ""),
            "direction": "incoming",
            "timestamp": int(item.get("timestamp") or item.get("create_time") or 0),
            "text": bounded,
            "message_type": str(
                item.get("message_type") or item.get("type") or "other"
            ),
            "reply_reference": dict(item.get("reply_reference") or {}),
        })
        remaining -= len(bounded)
    context.reverse()
    return context


def message_sender_id(message):
    return message_identifier(message, SENDER_ID_FIELDS)


def message_room_id(message):
    return message_identifier(message, ROOM_ID_FIELDS)


def normalize_control_command(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    while True:
        stripped = CONTROL_SUFFIX_RE.sub("", normalized).strip()
        if stripped == normalized:
            return stripped
        normalized = stripped


def control_command_kind(message):
    prompt = str(message.get("prompt") or message.get("text") or "")
    normalized = normalize_control_command(prompt)
    if not normalized or not CONTROL_COMMAND_RE.fullmatch(normalized):
        return ""
    if normalized in STOP_ALL_COMMANDS or normalized == "\u53d6\u6d88":
        return "cancel_all"
    if normalized in MEDIA_ONLY_COMMANDS:
        return "media_only"
    if normalized.startswith("\u53d6\u6d88"):
        return "cancel"
    if normalized.startswith("\u4efb\u52a1"):
        return "status"
    if normalized.startswith("\u91cd\u8bd5"):
        return "retry"
    if normalized.startswith("\u8865\u5145"):
        return "supplement"
    if normalized.startswith("\u4fee\u6539"):
        return "modify"
    return ""


def _native_at_values(value):
    if value is None:
        return []
    if isinstance(value, dict):
        values = []
        for key in (
            "at_user_list",
            "at_users",
            "atuserlist",
            "target_wxids",
            "targets",
            "users",
        ):
            values.extend(_native_at_values(value.get(key)))
        return values
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_native_at_values(item))
        return values
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("<"):
        try:
            root = ElementTree.fromstring(text)
        except (ElementTree.ParseError, TypeError, ValueError):
            return []
        values = []
        for element in root.iter():
            tag = str(element.tag).rsplit("}", 1)[-1].lower()
            if tag in {"atuserlist", "at_users", "atuser", "at_user"}:
                values.extend(_native_at_values(element.text))
        return values
    return [
        item.strip()
        for item in re.split(r"[,;|\s]+", text)
        if item.strip()
    ]


def native_at_user_ids(message):
    values = []
    for field in (
        "native_at_user_list",
        "at_user_list",
        "at_users",
        "atuserlist",
        "message_source",
        "msg_source",
        "msgsource",
        "mention_evidence",
    ):
        values.extend(_native_at_values(message.get(field)))
    return {normalize_identifier(value) for value in values if normalize_identifier(value)}


def trusted_mentions_bot(message):
    if "structured_valid" not in message:
        return bool(message.get("mentions_bot"))
    evidence = message.get("mention_evidence")
    evidence_source = ""
    if isinstance(evidence, dict):
        evidence_source = str(evidence.get("source") or "").strip().lower()
    evidence_source = str(
        message.get("mention_source")
        or message.get("mentions_source")
        or evidence_source
        or ""
    ).strip().lower()
    bot_id = normalize_identifier(BOT_WXID)
    native_ids = native_at_user_ids(message)
    if (
        bool(message.get("native_mentions_bot"))
        and evidence_source
        in {
            "native_at_metadata",
            "native_at_user_list",
            "msg_source_at_user_list",
        }
        and bot_id
        and bot_id in native_ids
    ):
        return True
    return bool(bot_id and bot_id in native_ids)


def trusted_reply_to_bot(message):
    if (
        not bool(message.get("reply_to_bot"))
        or not bool(message.get("structured_valid", True))
        or message_type(message) != "quoted_reply"
    ):
        return False
    reference = dict(message.get("reply_reference") or {})
    sender = normalize_identifier(
        reference.get("sender_id")
        or reference.get("sender_wxid")
        or reference.get("sender_numeric_id")
    )
    if not sender:
        return False
    bot_id = normalize_identifier(BOT_WXID)
    return not bot_id or hmac.compare_digest(sender, bot_id)


def legacy_message_identity(message):
    """Retain the v2 content fingerprint as a migration alias only."""
    room_id = message_room_id(message)
    sender_id = message_sender_id(message)
    sort_seq = int(message.get("sort_seq") or 0)
    timestamp = int(message.get("timestamp") or message.get("create_time") or 0)
    if not room_id or not sender_id or (sort_seq <= 0 and timestamp <= 0):
        return "local:%d" % int(message.get("local_id") or 0), False
    identity = {
        "v": 2,
        "room_id": room_id,
        "sender_id": sender_id,
        "sort_seq": sort_seq,
        "timestamp": timestamp,
        "origin_source": message_origin_source(message),
        "local_type": int(message.get("local_type") or 0),
        "message_type": message_type(message),
        "text": unicodedata.normalize(
            "NFC",
            str(message.get("text") or message.get("prompt") or ""),
        ).replace("\r\n", "\n").replace("\r", "\n").strip(),
        "reply_reference": dict(message.get("reply_reference") or {}),
        "attachments": list(message.get("attachments") or []),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), True


def message_identity(message):
    """Return a stable per-message physical identity for new state entries."""
    room_id = message_room_id(message)
    server_id = message_server_id(message)
    local_id = message_local_id(message)
    if room_id and server_id:
        identity = {
            "v": 3,
            "room_id": room_id,
            "msg_svr_id": server_id,
        }
    elif room_id and local_id:
        identity = {
            "v": 3,
            "room_id": room_id,
            "local_id": local_id,
        }
    else:
        return legacy_message_identity(message)
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), True


def message_identity_aliases(message):
    """Return the physical primary key and compatible v2 content alias."""
    primary, stable = message_identity(message)
    if not stable:
        return [], False
    aliases = [primary]
    legacy, legacy_stable = legacy_message_identity(message)
    if legacy_stable and legacy not in aliases:
        aliases.append(legacy)
    return aliases, True


def message_was_processed(state, message):
    identities, stable = message_identity_aliases(message)
    processed = set(state.get("processed_identities") or [])
    return bool(stable and any(identity in processed for identity in identities))


def remember_message_identity(state, message):
    identities_to_add, stable = message_identity_aliases(message)
    if not stable:
        return False
    identities = list(state.get("processed_identities") or [])
    for identity in identities_to_add:
        if identity in identities:
            identities.remove(identity)
        identities.append(identity)
    state["processed_identities"] = identities[-PROCESSED_IDENTITY_LIMIT:]
    return True


def message_delivery_key(message):
    identity, stable = message_identity(message)
    if stable:
        return identity[:32]
    return str(int(message["local_id"]))


def message_session_id(message):
    room_id = message_room_id(message)
    identity = room_id.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:32]
    return "wechat-room:" + digest


def message_request_id(message):
    room_id = message_room_id(message)
    identity, stable = message_identity(message)
    if stable:
        return "wechat:%s:msg:%s" % (room_id, identity[:32])
    return "wechat:%s:%d" % (room_id, int(message["local_id"]))


def message_server_id(message):
    return message_identifier(message, SERVER_ID_FIELDS)


def strip_internal_format_chars(value):
    """Remove invisible correlation/control characters from user-visible text."""
    return str(value or "").replace("\x00", "").translate(
        INTERNAL_FORMAT_TRANSLATION
    )


def sanitize_outbound_text(value):
    return (
        unicodedata.normalize("NFC", strip_internal_format_chars(value))
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def is_low_information_reply(value):
    """Recognize stale presence pings that should never fill the group chat."""
    candidate = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "",
        sanitize_outbound_text(value).casefold(),
    )
    return bool(
        candidate in LOW_INFORMATION_REPLY_KEYS
        or LOW_INFORMATION_REPLY_RE.fullmatch(candidate)
    )


def message_type(message):
    value = str(message.get("message_type") or message.get("type") or "").strip()
    if value:
        return value
    return "text" if int(message.get("local_type", 0)) == 1 else "other"


def ask_ai(message, prompt, *, timeout=None):
    local_id = message_local_id(message)
    room_id = message_room_id(message)
    sender_id = message_sender_id(message)
    if not room_id or room_id != normalize_identifier(GROUP_ID):
        raise ValueError("message room identity is missing or invalid")
    if local_id <= 0:
        raise ValueError("message local identity is missing or invalid")
    if not sender_id:
        raise ValueError("message sender identity is missing")
    context = get_recent_context(local_id)
    source_identity, source_identity_stable = message_identity(message)
    payload = json.dumps(
        {
            "message": prompt,
            "request_id": message_request_id(message),
            "session_id": message_session_id(message),
            "source": "linux-wechat-db-bridge",
            "room_id": room_id,
            "group_id": room_id,
            "local_id": local_id,
            "source_local_id": local_id,
            "msg_svr_id": message_server_id(message),
            "sender_id": sender_id,
            "sender_wxid": sender_id,
            "sender_name": str(message.get("sender_name") or ""),
            "timestamp": int(message.get("timestamp") or message.get("create_time") or 0),
            "direction": message_direction(message),
            "mentions_bot": trusted_mentions_bot(message),
            "reply_to_bot": trusted_reply_to_bot(message),
            "message_type": message_type(message),
            "attachments": list(message.get("attachments") or []),
            "reply_reference": dict(message.get("reply_reference") or {}),
            "source_identity": (
                source_identity if source_identity_stable else ""
            ),
            "context": context,
            "group_context": context,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        AI_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": AI_API_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=AI_API_TIMEOUT if timeout is None else timeout,
        ) as response:
            data = read_json_response(response, "AI API")
    except urllib.error.HTTPError as exc:
        raise RemoteAPIError("AI API", status=exc.code) from None
    reply = (
        data.get("reply")
        or data.get("content")
        or data.get("message")
        or data.get("answer")
        or ""
    )
    if isinstance(reply, dict):
        reply = reply.get("content") or reply.get("text") or ""
    reply = sanitize_outbound_text(reply)
    status = str(data.get("status") or "").strip()
    task_id = str(data.get("task_id") or "").strip()
    generation = data.get("generation")
    if not reply and status == "ignored":
        return {
            "text": "",
            "status": "ignored",
            "task_id": task_id,
            "generation": generation,
        }
    if is_low_information_reply(reply):
        LOG.warning(
            "suppressed low-information AI reply local_id=%d",
            local_id,
        )
        return {
            "text": "",
            "status": "ignored",
            "task_id": task_id,
            "generation": generation,
        }
    if not reply:
        raise RuntimeError("empty AI reply")
    if any(
        str(data.get(field) or "").strip()
        for field in ("media_type", "media_data", "media_url")
    ):
        LOG.warning(
            "ignored deprecated Adapter media response local_id=%d",
            local_id,
        )
    if status or task_id or generation is not None:
        return {
            "text": reply,
            "status": status,
            "task_id": task_id,
            "generation": generation,
        }
    return reply


def send_text(
    text,
    request_id,
    *,
    room_id=None,
    source_local_id=0,
    task_id="",
    generation=0,
):
    text = sanitize_outbound_text(text)
    if not text:
        raise ValueError("message text is empty")
    return api_request(
        "POST",
        group_messages_path(),
        {
            "text": text,
            "request_id": request_id,
            "room_id": str(room_id or GROUP_ID),
            "source_local_id": int(source_local_id or 0),
            "task_id": str(task_id or ""),
            "generation": int(generation or 0),
        },
        timeout=20,
    )


def split_text_chunks(text, max_chars=None):
    max_chars = TEXT_CHUNK_CHARS if max_chars is None else max(100, int(max_chars))
    remaining = str(text or "").strip()
    if not remaining:
        return []
    chunks = []
    separators = ("\n\n", "\n", "\u3002", "\uff01", "\uff1f", ";", "\uff1b", "\uff0c", " ")
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        cut = -1
        include = 0
        for separator in separators:
            index = window.rfind(separator, max_chars // 2, max_chars + 1)
            if index > cut:
                cut = index
                include = len(separator)
        if cut < 0:
            cut = max_chars
            include = 0
        else:
            cut += include
        chunk = remaining[:cut].strip()
        if not chunk:
            chunk = remaining[:max_chars]
            cut = max_chars
        chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def should_handle(message):
    trigger = bool(
        trusted_mentions_bot(message)
        or trusted_reply_to_bot(message)
        or is_control_message(message)
    )
    has_current_text = bool(
        str(message.get("prompt") or message.get("text") or "").strip()
    )
    return bool(
        message_is_real_inbound(message)
        and int(message.get("local_type", 0)) in {1, 49}
        and message_room_id(message) == normalize_identifier(GROUP_ID)
        and bool(message_sender_id(message))
        and message.get("structured_valid", True)
        and (trigger or GROUP_LISTENER_ENABLED)
        and has_current_text
    )


def message_may_have_pending_structured_metadata(message):
    if (
        not message_is_real_inbound(message)
        or int(message.get("local_type", 0)) not in {1, 49}
        or not bool(message_sender_id(message))
        or is_control_message(message)
    ):
        return False
    if GROUP_LISTENER_ENABLED:
        # Full listening removes the need for @ as a trigger, but a visible
        # candidate still deserves the short metadata window so a real native
        # @ can bypass passive pacing once WeChat finishes writing its XML.
        return bool(message.get("visible_mention_candidate")) or not bool(
            message.get("structured_valid", True)
        )
    if trusted_mentions_bot(message) or trusted_reply_to_bot(message):
        return False
    return bool(message.get("visible_mention_candidate")) or not bool(
        message.get("structured_valid", True)
    )


def should_wait_for_structured_metadata(state, message, now=None):
    local_id = int(message.get("local_id") or 0)
    wait = state.get("metadata_wait") or {}
    same_message = int(wait.get("local_id") or 0) == local_id
    if not message_may_have_pending_structured_metadata(message):
        if same_message:
            state["metadata_wait"] = None
            LOG.info(
                "structured metadata settled local_id=%d trigger=%s",
                local_id,
                should_handle(message),
            )
        return False
    if STRUCTURED_METADATA_WAIT_SECONDS <= 0:
        if same_message:
            state["metadata_wait"] = None
        return False
    now = time.time() if now is None else float(now)
    if same_message:
        if now < float(wait.get("expires_at") or 0):
            return True
        state["metadata_wait"] = None
        LOG.warning("structured metadata wait expired local_id=%d", local_id)
        return False
    state["metadata_wait"] = {
        "local_id": local_id,
        "started_at": now,
        "expires_at": now + STRUCTURED_METADATA_WAIT_SECONDS,
    }
    atomic_save_state(state)
    LOG.info("waiting for structured metadata local_id=%d", local_id)
    return True


def is_control_message(message):
    return bool(control_command_kind(message))


def retry_delay(attempts):
    return min(60.0, max(1.0, 2.0 ** max(0, attempts - 1)))


def prepare_ai_result(local_id, result):
    if isinstance(result, dict):
        raw_reply = result.get("text", "")
        if not raw_reply and result.get("chunks"):
            raw_reply = "\n".join(
                str(chunk or "") for chunk in result.get("chunks") or []
            )
    else:
        raw_reply = result
    reply = sanitize_outbound_text(raw_reply)
    status = str(result.get("status") or "") if isinstance(result, dict) else ""
    task_id = str(result.get("task_id") or "") if isinstance(result, dict) else ""
    generation = result.get("generation") if isinstance(result, dict) else None
    if status == "ignored" and not reply:
        return {
            "kind": "text",
            "text": "",
            "chunks": [],
            "status": status,
            "task_id": task_id,
            "generation": generation,
        }
    if is_low_information_reply(reply):
        LOG.warning(
            "suppressed low-information prepared result local_id=%d",
            int(local_id),
        )
        return {
            "kind": "text",
            "text": "",
            "chunks": [],
            "status": "ignored",
            "task_id": task_id,
            "generation": generation,
        }
    return {
        "kind": "text",
        "text": reply or EMPTY_REPLY_FALLBACK,
        "chunks": split_text_chunks(reply or EMPTY_REPLY_FALLBACK),
        "status": status,
        "task_id": task_id,
        "generation": generation,
    }


def normalize_prepared_result(local_id, prepared):
    if not isinstance(prepared, dict):
        prepared = {}
    if prepared.get("kind") != "text":
        LOG.warning(
            "suppressed legacy pending media local_id=%d kind=%s",
            int(local_id),
            str(prepared.get("kind") or "unknown"),
        )
    raw_text = prepared.get("text") or "\n".join(
        str(chunk or "") for chunk in prepared.get("chunks") or []
    )
    if not sanitize_outbound_text(raw_text):
        # A legacy media-only pending entry has no text fallback.  Clearing it
        # must remain silent rather than inventing an acknowledgement.
        return {
            "kind": "text",
            "text": "",
            "chunks": [],
            "status": str(prepared.get("status") or "ignored"),
            "task_id": str(prepared.get("task_id") or ""),
            "generation": prepared.get("generation"),
        }
    return prepare_ai_result(
        local_id,
        {
            "text": raw_text,
            "status": prepared.get("status") or "",
            "task_id": prepared.get("task_id") or "",
            "generation": prepared.get("generation"),
        },
    )


def send_prepared_result(message, prepared):
    if isinstance(message, dict):
        local_id = int(message["local_id"])
        room_id = message_room_id(message)
        delivery_key = message_delivery_key(message)
    else:
        local_id = int(message)
        room_id = GROUP_ID
        delivery_key = str(local_id)
    prepared = normalize_prepared_result(local_id, prepared)
    reply = str(prepared.get("text") or "")
    chunks = prepared.get("chunks") or split_text_chunks(reply)
    if not chunks:
        return reply
    metadata = {
        "room_id": room_id,
        "source_local_id": local_id,
        "task_id": str(prepared.get("task_id") or ""),
        "generation": int(prepared.get("generation") or 0),
    }
    if len(chunks) == 1:
        send_text(chunks[0], "reply:%s" % delivery_key, **metadata)
    else:
        for index, chunk in enumerate(chunks, start=1):
            send_text(
                chunk,
                "reply:%s:part:%d" % (delivery_key, index),
                **metadata,
            )
    return reply


def clear_pending_result(state, local_id):
    pending = state.get("pending") or {}
    if int(pending.get("local_id", -1)) != int(local_id):
        return
    prepared = pending.get("result") or {}
    image_path = prepared.get("image_path")
    if image_path:
        try:
            Path(image_path).unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            LOG.error(
                "failed to remove pending image artifact error=%s",
                safe_error_summary(exc),
            )
    state["pending"] = None


def stop_before_local_id(state):
    return max(0, int(state.get("stop_before_local_id") or 0))


def message_invalidated_by_stop(state, message):
    cutoff = stop_before_local_id(state)
    return bool(
        cutoff
        and int(message.get("local_id") or 0) < cutoff
        and not is_control_message(message)
    )


def discard_invalidated_delivery_state(state):
    cutoff = stop_before_local_id(state)
    if not cutoff:
        return False
    changed = False
    pending = state.get("pending") or {}
    pending_local_id = int(pending.get("local_id") or 0)
    if pending_local_id and pending_local_id < cutoff:
        clear_pending_result(state, pending_local_id)
        changed = True
    retry = state.get("retry") or {}
    retry_local_id = int(retry.get("local_id") or 0)
    if retry_local_id and retry_local_id < cutoff:
        state["retry"] = None
        changed = True
    if changed:
        atomic_save_state(state)
    return changed


def apply_control_effect(state, message, result):
    if control_command_kind(message) != "cancel_all":
        return False
    status = str(result.get("status") or "") if isinstance(result, dict) else ""
    if status != "canceled":
        return False
    local_id = int(message["local_id"])
    if local_id <= stop_before_local_id(state):
        return False
    state["stop_before_local_id"] = local_id
    discard_invalidated_delivery_state(state)
    atomic_save_state(state)
    LOG.info("room stop watermark committed local_id=%d", local_id)
    return True


def require_stop_effect(state, message, result):
    if control_command_kind(message) != "cancel_all":
        return
    local_id = int(message["local_id"])
    apply_control_effect(state, message, result)
    if "structured_valid" not in message:
        return
    if stop_before_local_id(state) < local_id:
        raise RuntimeError("stop control was not committed")


TERMINAL_DELIVERY_OUTCOMES = {
    409: "uncertain",
    422: "idempotency_conflict",
    423: "suppressed",
}


def terminal_delivery_outcome(exc):
    if not isinstance(exc, RemoteAPIError):
        return ""
    return TERMINAL_DELIVERY_OUTCOMES.get(exc.status, "")


def is_suppressed_error(exc):
    return terminal_delivery_outcome(exc) == "suppressed"


def mark_control_preprocessed(local_id):
    with PREPROCESSED_CONTROL_LOCK:
        PREPROCESSED_CONTROL_IDS.add(int(local_id))


def pop_control_preprocessed(local_id):
    with PREPROCESSED_CONTROL_LOCK:
        local_id = int(local_id)
        if local_id not in PREPROCESSED_CONTROL_IDS:
            return False
        PREPROCESSED_CONTROL_IDS.remove(local_id)
        return True


def deliver_result(message, result):
    local_id = int(message["local_id"])
    prepared = prepare_ai_result(local_id, result)
    try:
        reply = send_prepared_result(message, prepared)
    except Exception as exc:
        delivery_status = terminal_delivery_outcome(exc)
        if not delivery_status:
            raise
        LOG.warning(
            "outbound result reached terminal delivery state "
            "local_id=%d delivery=%s",
            local_id,
            delivery_status,
        )
        return prepared, delivery_status
    return prepared, "ignored" if not reply and not prepared.get("chunks") else "sent"


def _control_priority(message):
    kind = control_command_kind(message)
    if kind in {"cancel_all", "cancel", "media_only"}:
        return 0
    return 1


def process_priority_controls(state, active_local_id):
    after = max(
        int(active_local_id),
        int(state.get("last_local_id") or 0),
        int(state.get("control_scan_cursor") or 0),
    )
    deadline = time.monotonic() + CONTROL_SCAN_SECONDS
    caught_up = False
    failed_local_ids = []
    made_progress = False
    while True:
        messages = get_messages(after)
        page = sorted(
            (
                message
                for message in messages
                if int(message.get("local_id") or 0) > after
            ),
            key=lambda item: int(item.get("local_id") or 0),
        )
        if not page:
            caught_up = True
            break
        for message in sorted(
            page,
            key=lambda item: (
                _control_priority(item),
                int(item.get("local_id") or 0),
            ),
        ):
            local_id = int(message.get("local_id") or 0)
            if not is_control_message(message):
                continue
            if _control_priority(message) != 0:
                continue
            with PREPROCESSED_CONTROL_LOCK:
                if local_id in PREPROCESSED_CONTROL_IDS:
                    continue
            if message_was_processed(state, message):
                mark_control_preprocessed(local_id)
                continue
            if not should_handle(message):
                continue
            prompt = normalize_control_command(
                message.get("prompt") or message.get("text") or ""
            )
            try:
                result = ask_ai(
                    message,
                    prompt,
                    timeout=CONTROL_API_TIMEOUT,
                )
                require_stop_effect(state, message, result)
                _, delivery_status = deliver_result(message, result)
            except Exception as exc:
                failed_local_ids.append(local_id)
                LOG.error(
                    "priority control failed local_id=%d error=%s",
                    local_id,
                    safe_error_summary(exc),
                )
                continue
            remember_message_identity(state, message)
            mark_control_preprocessed(local_id)
            LOG.info(
                "priority control processed local_id=%d delivery=%s",
                local_id,
                delivery_status,
            )
        next_after = max(int(message.get("local_id") or 0) for message in page)
        if next_after <= after:
            caught_up = True
            break
        after = next_after
        made_progress = True
        if len(page) < 200:
            caught_up = True
            break
        if time.monotonic() >= deadline:
            break
    scan_cursor = after
    if failed_local_ids:
        scan_cursor = min(scan_cursor, min(failed_local_ids) - 1)
    previous_cursor = int(state.get("control_scan_cursor") or 0)
    state["control_scan_cursor"] = max(
        int(active_local_id),
        scan_cursor,
    )
    if made_progress or state["control_scan_cursor"] != previous_cursor:
        atomic_save_state(state)
    return caught_up and not failed_local_ids


def ask_ai_with_control_preemption(state, message, prompt):
    results = queue.Queue(maxsize=1)

    def invoke():
        try:
            results.put((True, ask_ai(message, prompt)))
        except BaseException as exc:
            results.put((False, exc))

    worker = threading.Thread(
        target=invoke,
        name="wechat-bridge-ai-%d" % int(message["local_id"]),
        daemon=True,
    )
    worker.start()
    completed = None
    while True:
        if completed is None:
            try:
                completed = results.get(timeout=POLL_SECONDS)
            except queue.Empty:
                process_priority_controls(state, int(message["local_id"]))
                continue
        ok, value = completed
        if process_priority_controls(state, int(message["local_id"])):
            if ok:
                return value
            raise value
        time.sleep(POLL_SECONDS)


def handle_message(state, message):
    local_id = int(message["local_id"])
    if pop_control_preprocessed(local_id):
        remember_message_identity(state, message)
        state["last_local_id"] = local_id
        state["retry"] = None
        atomic_save_state(state)
        return True
    if message_was_processed(state, message):
        clear_pending_result(state, local_id)
        state["last_local_id"] = local_id
        state["retry"] = None
        atomic_save_state(state)
        LOG.info("duplicate structured message skipped local_id=%d", local_id)
        return True
    if message_invalidated_by_stop(state, message):
        clear_pending_result(state, local_id)
        remember_message_identity(state, message)
        state["last_local_id"] = local_id
        state["retry"] = None
        atomic_save_state(state)
        LOG.info("old message invalidated by stop local_id=%d", local_id)
        return True
    if not should_handle(message):
        if message_direction(message) == "incoming":
            LOG.info(
                "structured inbound ignored local_id=%d type=%s "
                "mention=%s reply=%s visible_candidate=%s valid=%s "
                "sender_present=%s",
                local_id,
                message_type(message),
                trusted_mentions_bot(message),
                trusted_reply_to_bot(message),
                bool(message.get("visible_mention_candidate")),
                bool(message.get("structured_valid", True)),
                bool(message_sender_id(message)),
            )
        remember_message_identity(state, message)
        state["last_local_id"] = local_id
        state["retry"] = None
        atomic_save_state(state)
        return True

    prompt = str(message.get("prompt") or "").strip() or "\u4f60\u597d"
    LOG.info(
        "structured trigger local_id=%d sender=%s type=%s mention=%s reply=%s",
        local_id,
        message_sender_id(message),
        message_type(message),
        trusted_mentions_bot(message),
        trusted_reply_to_bot(message),
    )
    started = time.monotonic()
    pending = state.get("pending") or {}
    if int(pending.get("local_id", -1)) != local_id:
        try:
            if is_control_message(message):
                if not process_priority_controls(state, local_id):
                    raise RuntimeError("priority control scan incomplete")
                result = ask_ai(
                    message,
                    normalize_control_command(prompt),
                    timeout=CONTROL_API_TIMEOUT,
                )
                require_stop_effect(state, message, result)
            else:
                result = ask_ai_with_control_preemption(state, message, prompt)
        except RuntimeError as exc:
            if "empty ai reply" not in str(exc).lower():
                raise
            LOG.warning("empty AI reply for local_id=%d; using fallback", local_id)
            result = EMPTY_REPLY_FALLBACK
        pending = {
            "local_id": local_id,
            "created_at": time.time(),
            "result": prepare_ai_result(local_id, result),
        }
        state["pending"] = pending
        atomic_save_state(state)
    try:
        reply = send_prepared_result(message, pending["result"])
    except Exception as exc:
        delivery_status = terminal_delivery_outcome(exc)
        if not delivery_status:
            raise
        LOG.warning(
            "pending reply reached terminal delivery state "
            "local_id=%d delivery=%s",
            local_id,
            delivery_status,
        )
        reply = ""
    LOG.info(
        "reply finalized for local_id=%d in %.2fs",
        local_id,
        time.monotonic() - started,
    )
    clear_pending_result(state, local_id)
    remember_message_identity(state, message)
    state["last_local_id"] = local_id
    state["retry"] = None
    state["last_reply_at"] = time.time()
    atomic_save_state(state)
    return True


def record_failure(state, message, exc):
    local_id = int(message["local_id"])
    previous = state.get("retry") or {}
    if int(previous.get("local_id", -1)) != local_id:
        previous = {}
    attempts = int(previous.get("attempts", 0)) + 1
    state["retry"] = {
        "local_id": local_id,
        "attempts": attempts,
        "next_retry_at": time.time() + retry_delay(attempts),
        "error": safe_error_summary(exc),
        "phase": "failure_notice" if attempts >= MAX_RETRIES else "processing",
    }
    atomic_save_state(state)
    LOG.error(
        "message local_id=%d failed, retry %d/%d error=%s",
        local_id,
        attempts,
        MAX_RETRIES,
        safe_error_summary(exc),
    )
    return attempts


def finish_failed_message(state, message):
    local_id = int(message["local_id"])
    if message_invalidated_by_stop(state, message):
        clear_pending_result(state, local_id)
        remember_message_identity(state, message)
        state["last_local_id"] = local_id
        state["retry"] = None
        atomic_save_state(state)
        LOG.info("failure notice invalidated by stop local_id=%d", local_id)
        return
    try:
        send_text(
            "\u521a\u521a\u8fde\u7eed\u91cd\u8bd5\u4e86\u51e0\u6b21\uff0cAI\u4ecd\u7136\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002",
            "failure:%s" % message_delivery_key(message),
            room_id=message_room_id(message),
            source_local_id=local_id,
        )
    except Exception as exc:
        delivery_status = terminal_delivery_outcome(exc)
        if not delivery_status:
            raise
        LOG.warning(
            "failure notice reached terminal delivery state "
            "local_id=%d delivery=%s",
            local_id,
            delivery_status,
        )
    clear_pending_result(state, local_id)
    remember_message_identity(state, message)
    state["last_local_id"] = local_id
    state["retry"] = None
    atomic_save_state(state)
    LOG.error("message local_id=%d exhausted retries", local_id)


def record_failure_notice_failure(state, message, exc):
    local_id = int(message["local_id"])
    retry = state.get("retry") or {}
    attempts = max(MAX_RETRIES, int(retry.get("attempts", MAX_RETRIES)))
    state["retry"] = {
        "local_id": local_id,
        "attempts": attempts,
        "next_retry_at": time.time() + retry_delay(attempts),
        "error": safe_error_summary(exc),
        "phase": "failure_notice",
    }
    atomic_save_state(state)
    LOG.error(
        "failed to send final failure notice for local_id=%d error=%s",
        local_id,
        safe_error_summary(exc),
    )


def initialize_cursor(state):
    if state.get("cursor_ready"):
        return
    health = get_health()
    state["last_local_id"] = int(health.get("latest_local_id", 0))
    state["control_scan_cursor"] = state["last_local_id"]
    state["cursor_ready"] = True
    state["retry"] = None
    atomic_save_state(state)
    LOG.info("database cursor baseline=%d", state["last_local_id"])


def run_once(state):
    discard_invalidated_delivery_state(state)
    retry = state.get("retry") or {}
    pending = state.get("pending") or {}
    controls_scanned = False
    if retry or pending:
        active_local_id = int(
            retry.get("local_id")
            or pending.get("local_id")
            or state.get("last_local_id")
            or 0
        )
        if not process_priority_controls(state, active_local_id):
            return
        controls_scanned = True
        discard_invalidated_delivery_state(state)
        retry = state.get("retry") or {}
    if retry and time.time() < float(retry.get("next_retry_at", 0)):
        return
    messages = get_messages(state.get("last_local_id", 0))
    if (
        not controls_scanned
        and any("structured_valid" in message for message in messages)
    ):
        if not process_priority_controls(
            state,
            int(state.get("last_local_id") or 0),
        ):
            return
        discard_invalidated_delivery_state(state)
    for message in messages:
        local_id = int(message["local_id"])
        if local_id <= int(state.get("last_local_id", 0)):
            continue
        if (
            not message_invalidated_by_stop(state, message)
            and should_wait_for_structured_metadata(state, message)
        ):
            return
        retry = state.get("retry") or {}
        if (
            int(retry.get("local_id", -1)) == local_id
            and (
                retry.get("phase") == "failure_notice"
                or int(retry.get("attempts", 0)) >= MAX_RETRIES
            )
        ):
            try:
                finish_failed_message(state, message)
            except Exception as exc:
                record_failure_notice_failure(state, message, exc)
            return
        try:
            handle_message(state, message)
        except Exception as exc:
            attempts = record_failure(state, message, exc)
            if attempts >= MAX_RETRIES:
                try:
                    finish_failed_message(state, message)
                except Exception as notice_exc:
                    record_failure_notice_failure(state, message, notice_exc)
            return


def main():
    LOG.info(
        "structured WeChat bridge started group=%s api=%s",
        GROUP_ID,
        CHAT_API_URL,
    )
    state = load_state()
    api_failure_streak = 0
    while True:
        try:
            initialize_cursor(state)
            run_once(state)
            api_failure_streak = 0
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            api_failure_streak += 1
            LOG.error(
                "structured bridge loop failed streak=%d error=%s",
                api_failure_streak,
                safe_error_summary(exc),
            )
            time.sleep(min(15.0, 1.5 * api_failure_streak))


if __name__ == "__main__":
    main()
