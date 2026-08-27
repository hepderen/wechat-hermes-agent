#!/usr/bin/env python3
import argparse
import base64
import faulthandler
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ElementTree
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import zstandard
from Crypto.Cipher import AES


PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = 80
SQLITE_HEADER = b"SQLite format 3\x00"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_FRAME_SIZE = WAL_FRAME_HEADER_SIZE + PAGE_SIZE

LOG = logging.getLogger("wechat-chat-api")


class SnapshotRace(RuntimeError):
    pass


class SearchPopupNotFoundError(RuntimeError):
    """The Weixin search popup did not reach a usable state in time."""


class SearchPopupAmbiguousError(RuntimeError):
    """More than one usable search popup matched the window filter."""


def fsync_parent_directory(path):
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(str(Path(path).parent), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        fsync_parent_directory(path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def derive_mac_key(enc_key, salt):
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)


def page_hmac_valid(page, page_number, mac_key):
    if len(page) != PAGE_SIZE:
        return False
    start = SALT_SIZE if page_number == 1 else 0
    payload_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    expected = page[PAGE_SIZE - HMAC_SIZE :]
    digest = hmac.new(mac_key, page[start:payload_end], hashlib.sha512)
    digest.update(struct.pack("<I", page_number))
    return hmac.compare_digest(digest.digest(), expected)


def decrypt_page(enc_key, page, page_number):
    iv_start = PAGE_SIZE - RESERVE_SIZE
    iv = page[iv_start : iv_start + IV_SIZE]
    start = SALT_SIZE if page_number == 1 else 0
    encrypted = page[start : PAGE_SIZE - RESERVE_SIZE]
    decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    if page_number == 1:
        return SQLITE_HEADER + decrypted + (b"\x00" * RESERVE_SIZE)
    return decrypted + (b"\x00" * RESERVE_SIZE)


def wal_checksum(data, endian, seed1=0, seed2=0):
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of 8 bytes")
    words = struct.unpack(endian + str(len(data) // 4) + "I", data)
    value1 = seed1
    value2 = seed2
    for index in range(0, len(words), 2):
        value1 = (value1 + words[index] + value2) & 0xFFFFFFFF
        value2 = (value2 + words[index + 1] + value1) & 0xFFFFFFFF
    return value1, value2


def decode_message_content(value, compression_type, decompressor=None):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if compression_type == 4:
        try:
            decompressor = decompressor or zstandard.ZstdDecompressor()
            return decompressor.decompress(value).decode("utf-8", errors="replace")
        except Exception:
            pass
    return value.decode("utf-8", errors="replace")


def split_group_content(content):
    if ":\n" not in content:
        return "", content
    sender, body = content.split(":\n", 1)
    if sender.startswith("wxid_") or sender.endswith("@openim"):
        return sender, body
    return "", content


def parse_mention(body, mention):
    normalized = unicodedata.normalize("NFKC", body or "")
    mention = unicodedata.normalize("NFKC", mention)
    spans = []
    offset = 0
    while True:
        index = normalized.find(mention, offset)
        if index < 0:
            break
        end = index + len(mention)
        if end >= len(normalized):
            spans.append((index, end))
        else:
            category = unicodedata.category(normalized[end])
            if normalized[end] != "_" and category[:1] not in {"L", "M", "N"}:
                spans.append((index, end))
        offset = end
    if not spans:
        return False, ""
    pieces = []
    previous = 0
    for start, end in spans:
        pieces.append(normalized[previous:start])
        pieces.append(" ")
        previous = end
    pieces.append(normalized[previous:])
    prompt = "".join(pieces).strip()
    return True, " ".join(prompt.split())


def parse_native_at_user_list(message_source):
    text = str(message_source or "").strip()
    if not text:
        return []
    try:
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, TypeError, ValueError):
        return []

    values = []
    seen = set()
    for element in root.iter():
        tag = str(element.tag).rsplit("}", 1)[-1].lower()
        if tag != "atuserlist":
            continue
        for value in re.split(r"[,;|\s]+", str(element.text or "")):
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def parse_quoted_reply(body, bot_wxid):
    reference = {
        "sender_wxid": "",
        "content": "",
    }
    try:
        root = ElementTree.fromstring(body or "")
    except (ElementTree.ParseError, TypeError, ValueError):
        return "", False, reference, False

    appmsg = root if root.tag == "appmsg" else root.find("./appmsg")
    if appmsg is None:
        return "", False, reference, False

    title = str(appmsg.findtext("./title") or "").strip()
    refermsg = appmsg.find("./refermsg")
    if refermsg is not None:
        reference["sender_wxid"] = str(
            refermsg.findtext("./chatusr") or ""
        ).strip()
        reference["content"] = str(
            refermsg.findtext("./content") or ""
        ).strip()
    reply_to_bot = bool(
        bot_wxid
        and reference["sender_wxid"]
        and hmac.compare_digest(reference["sender_wxid"], bot_wxid)
    )
    return title, reply_to_bot, reference, True


def canonical_message_text(value):
    return (
        unicodedata.normalize("NFC", str(value or ""))
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


TRACKING_BOUNDARY = "\u2063"
TRACKING_ZERO = "\u200b"
TRACKING_ONE = "\u200c"
TRACKING_BITS = 64


def request_tracking_marker(request_id):
    request_id = str(request_id or "").strip()
    if not request_id:
        return ""
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()[:8]
    bits = "".join(
        TRACKING_ONE if byte & (1 << bit) else TRACKING_ZERO
        for byte in digest
        for bit in range(7, -1, -1)
    )
    return TRACKING_BOUNDARY + bits + TRACKING_BOUNDARY


def split_tracking_marker(value):
    text = str(value or "")
    marker_length = TRACKING_BITS + 2
    if len(text) < marker_length or not text.endswith(TRACKING_BOUNDARY):
        return text, ""
    marker = text[-marker_length:]
    if marker[0] != TRACKING_BOUNDARY:
        return text, ""
    if any(character not in {TRACKING_ZERO, TRACKING_ONE} for character in marker[1:-1]):
        return text, ""
    return text[:-marker_length], marker


def visible_message_text(value):
    return split_tracking_marker(value)[0]


def tracked_message_text(text, request_id):
    return canonical_message_text(text) + request_tracking_marker(request_id)


def file_fingerprint(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return {
        "content_md5": digest.hexdigest(),
        "content_length": int(Path(path).stat().st_size),
    }


def media_message_fingerprints(value, media_type):
    try:
        root = ElementTree.fromstring(str(value or ""))
    except (ElementTree.ParseError, TypeError, ValueError):
        return []
    fingerprints = []
    if media_type == "image":
        elements = root.findall(".//img")
        pairs = (("md5", "hdlength"), ("md5", "length"))
    elif media_type == "video":
        elements = root.findall(".//videomsg")
        pairs = (("rawmd5", "rawlength"), ("md5", "length"))
    else:
        appmsg = root.find(".//appmsg")
        if appmsg is None and root.tag == "appmsg":
            appmsg = root
        if appmsg is None or str(appmsg.findtext("type") or "").strip() != "6":
            return []
        attachment = appmsg.find(".//appattach")
        elements = [attachment if attachment is not None else appmsg]
        pairs = (
            ("md5", "totallen"),
            ("filemd5", "totallen"),
            ("md5", "filelen"),
            ("filemd5", "filelen"),
        )
    for element in elements:
        for md5_name, length_name in pairs:
            content_md5 = str(
                element.attrib.get(md5_name)
                or element.findtext(md5_name)
                or ""
            ).strip().lower()
            if not content_md5:
                continue
            try:
                content_length = int(
                    element.attrib.get(length_name)
                    or element.findtext(length_name)
                    or 0
                )
            except (TypeError, ValueError):
                content_length = 0
            if media_type == "file" and content_length <= 0:
                continue
            fingerprint = {
                "content_md5": content_md5,
                "content_length": content_length,
            }
            if fingerprint not in fingerprints:
                fingerprints.append(fingerprint)
    return fingerprints


class SendUncertainError(RuntimeError):
    pass


class OutboundSuppressedError(RuntimeError):
    def __init__(self, message, barrier=None):
        super().__init__(message)
        self.barrier = dict(barrier or {})


class MediaNotSentError(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


def configured_outbound_token(config):
    config = config or {}
    environment_name = str(
        config.get("outbound_auth_token_env") or "WECHAT_CHAT_API_TOKEN"
    ).strip()
    environment_token = (
        str(os.environ.get(environment_name) or "").strip()
        if environment_name
        else ""
    )
    if environment_token:
        return environment_token
    return str(config.get("outbound_auth_token") or "").strip()


def normalize_outbound_envelope(payload, require_request_id=True):
    payload = payload or {}
    request_id = str(payload.get("request_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()

    def strict_integer(value, name, default=None):
        if value is None and default is not None:
            value = default
        if isinstance(value, bool):
            raise ValueError("%s must be an integer" % name)
        if isinstance(value, int):
            return value
        text = str(value or "").strip()
        if not re.fullmatch(r"-?\d+", text):
            raise ValueError("%s must be an integer" % name)
        return int(text)

    source_local_id = strict_integer(
        payload.get("source_local_id"),
        "source_local_id",
    )
    generation = strict_integer(
        payload.get("generation"),
        "generation",
        default=0,
    )
    if require_request_id and not request_id:
        raise ValueError("request_id is required")
    if source_local_id <= 0:
        raise ValueError("source_local_id must be positive")
    if task_id and generation <= 0:
        raise ValueError("task deliveries require a positive generation")
    if not task_id:
        generation = 0
    return {
        "request_id": request_id,
        "source_local_id": source_local_id,
        "task_id": task_id,
        "generation": generation,
    }


class OutboundControlStore:
    MODES = {"all", "media_only"}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            # The production control database lives directly under
            # /var/lib/wechat-hermes. PERSIST keeps the pre-created journal
            # reusable without granting the Chat API write access to the
            # surrounding state directory.
            self._connection.execute("PRAGMA journal_mode=PERSIST")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_barriers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    source_local_id INTEGER NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('all', 'media_only')),
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbound_barriers_room
                ON outbound_barriers(room_id, source_local_id DESC, id DESC)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbound_barriers_task
                ON outbound_barriers(room_id, task_id, generation, id DESC)
                """
            )

    @staticmethod
    def _normalize(
        room_id,
        source_local_id,
        mode,
        task_id="",
        generation=0,
        reason="",
    ):
        room_id = str(room_id or "").strip()
        task_id = str(task_id or "").strip()
        mode = str(mode or "").strip().lower()
        reason = str(reason or "").strip()[:300]
        if not room_id:
            raise ValueError("room_id is required")
        if mode not in OutboundControlStore.MODES:
            raise ValueError("barrier mode must be all or media_only")
        try:
            source_local_id = int(source_local_id)
            generation = int(generation or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("barrier cursor and generation must be integers") from exc
        if source_local_id <= 0:
            raise ValueError("source_local_id must be positive")
        if task_id and generation <= 0:
            raise ValueError("task barriers require a positive generation")
        if not task_id:
            generation = 0
        return {
            "room_id": room_id,
            "task_id": task_id,
            "generation": generation,
            "source_local_id": source_local_id,
            "mode": mode,
            "reason": reason,
        }

    def commit(
        self,
        room_id,
        source_local_id,
        mode,
        task_id="",
        generation=0,
        reason="",
    ):
        values = self._normalize(
            room_id,
            source_local_id,
            mode,
            task_id=task_id,
            generation=generation,
            reason=reason,
        )
        created_at = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO outbound_barriers(
                    room_id, task_id, generation, source_local_id,
                    mode, reason, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["room_id"],
                    values["task_id"],
                    values["generation"],
                    values["source_local_id"],
                    values["mode"],
                    values["reason"],
                    created_at,
                ),
            )
        return {
            "id": int(cursor.lastrowid),
            **values,
            "created_at": created_at,
        }

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def blocking_barrier(
        self,
        room_id,
        source_local_id,
        item_kind,
        task_id="",
        generation=0,
    ):
        room_id = str(room_id or "").strip()
        task_id = str(task_id or "").strip()
        item_kind = str(item_kind or "text").strip().lower()
        try:
            source_local_id = int(source_local_id or 0)
            generation = int(generation or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "barrier cursor and generation must be integers"
            ) from exc
        if not room_id:
            raise ValueError("room_id is required")
        if source_local_id <= 0:
            raise ValueError("source_local_id must be positive")
        if task_id and generation <= 0:
            raise ValueError("task barrier checks require a positive generation")
        media = item_kind != "text"
        with self._lock:
            if task_id and generation > 0:
                row = self._connection.execute(
                    """
                    SELECT * FROM outbound_barriers
                    WHERE room_id=? AND task_id=? AND generation=?
                      AND (mode='all' OR (mode='media_only' AND ?=1))
                    ORDER BY id DESC LIMIT 1
                    """,
                    (room_id, task_id, generation, int(media)),
                ).fetchone()
                if row is not None:
                    return self._row(row)
            row = self._connection.execute(
                """
                SELECT * FROM outbound_barriers
                WHERE room_id=? AND task_id=''
                  AND source_local_id>?
                  AND (mode='all' OR (mode='media_only' AND ?=1))
                ORDER BY source_local_id DESC, id DESC LIMIT 1
                """,
                (room_id, source_local_id, int(media)),
            ).fetchone()
        return self._row(row)

    def check(
        self,
        room_id,
        source_local_id,
        item_kind,
        task_id="",
        generation=0,
    ):
        barrier = self.blocking_barrier(
            room_id,
            source_local_id,
            item_kind,
            task_id=task_id,
            generation=generation,
        )
        return {
            "allowed": barrier is None,
            "barrier": barrier,
        }

    def health(self):
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS barrier_count, MAX(created_at) AS latest_created_at
                FROM outbound_barriers
                """
            ).fetchone()
        return {
            "ok": True,
            "path": str(self.path),
            "barrier_count": int(row["barrier_count"] or 0),
            "latest_created_at": row["latest_created_at"],
        }

    def close(self):
        with self._lock:
            self._connection.close()


class SnapshotReader:
    def __init__(self, config):
        self.config = config
        self.db_path = Path(config["db_path"])
        self.wal_path = Path(str(self.db_path) + "-wal")
        self.keys_path = Path(config["keys_file"])
        self.db_key_name = config.get("db_key_name", "message/message_0.db")
        self.group_id = config["group_id"]
        self.group_name = config.get("group_name", self.group_id)
        self.mention = config.get("mention", "@Hermes")
        self.bot_wxid = str(config.get("bot_wxid") or "").strip()
        self.table_name = "Msg_" + hashlib.md5(self.group_id.encode("utf-8")).hexdigest()
        self.cache_dir = Path(config.get("cache_dir", "~/.cache/wechat-chat-api")).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self.snapshot_path = self.cache_dir / "message.snapshot.db"
        self._lock = threading.RLock()
        self._fingerprint_value = None
        self._last_refresh_at = 0.0
        self._last_refresh_duration_ms = 0.0
        self._last_wal_frames = 0
        self.enc_key = self._load_key()

    def _load_key(self):
        data = json.loads(self.keys_path.read_text(encoding="utf-8"))
        key_info = data.get(self.db_key_name)
        if not isinstance(key_info, dict) or not key_info.get("enc_key"):
            raise RuntimeError("database key entry is missing")
        key = bytes.fromhex(key_info["enc_key"])
        if len(key) != KEY_SIZE:
            raise RuntimeError("database key has an invalid length")
        return key

    @staticmethod
    def _stat_signature(path):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def fingerprint(self):
        return (self._stat_signature(self.db_path), self._stat_signature(self.wal_path))

    def _decrypt_database(self, output_path):
        file_size = self.db_path.stat().st_size
        if file_size < PAGE_SIZE or file_size % PAGE_SIZE:
            raise SnapshotRace("encrypted database size is not page aligned")
        total_pages = file_size // PAGE_SIZE
        with self.db_path.open("rb") as source:
            first_page = source.read(PAGE_SIZE)
        salt = first_page[:SALT_SIZE]
        mac_key = derive_mac_key(self.enc_key, salt)
        if not page_hmac_valid(first_page, 1, mac_key):
            raise SnapshotRace("database page 1 HMAC verification failed")

        with self.db_path.open("rb") as source, open(output_path, "wb") as target:
            for page_number in range(1, total_pages + 1):
                page = source.read(PAGE_SIZE)
                if len(page) != PAGE_SIZE:
                    raise SnapshotRace("database changed while being read")
                target.write(decrypt_page(self.enc_key, page, page_number))
            target.flush()
            os.fsync(target.fileno())
        return salt, mac_key

    def _patch_wal(self, output_path):
        if not self.wal_path.exists() or self.wal_path.stat().st_size <= WAL_HEADER_SIZE:
            return 0
        with self.wal_path.open("rb") as handle:
            wal = handle.read()
        if len(wal) < WAL_HEADER_SIZE:
            return 0

        header = wal[:WAL_HEADER_SIZE]
        magic = struct.unpack(">I", header[:4])[0]
        if magic == 0x377F0682:
            checksum_endian = "<"
        elif magic == 0x377F0683:
            checksum_endian = ">"
        else:
            raise SnapshotRace("WAL header has an invalid magic value")
        if struct.unpack(">I", header[8:12])[0] != PAGE_SIZE:
            raise SnapshotRace("WAL page size does not match the database")

        stored_header_checksum = struct.unpack(">II", header[24:32])
        if wal_checksum(header[:24], checksum_endian) != stored_header_checksum:
            raise SnapshotRace("WAL header checksum verification failed")

        wal_salt = header[16:24]
        checksum1, checksum2 = stored_header_checksum
        frames = []
        last_commit_index = 0
        last_commit_size = 0
        offset = WAL_HEADER_SIZE
        while offset + WAL_FRAME_SIZE <= len(wal):
            frame_header = wal[offset : offset + WAL_FRAME_HEADER_SIZE]
            page = wal[
                offset + WAL_FRAME_HEADER_SIZE : offset + WAL_FRAME_SIZE
            ]
            page_number, commit_size = struct.unpack(">II", frame_header[:8])
            if frame_header[8:16] != wal_salt:
                break
            calculated = wal_checksum(
                frame_header[:8] + page,
                checksum_endian,
                checksum1,
                checksum2,
            )
            if calculated != struct.unpack(">II", frame_header[16:24]):
                break
            if page_number <= 0:
                break
            checksum1, checksum2 = calculated
            frames.append((page_number, page))
            if commit_size:
                last_commit_index = len(frames)
                last_commit_size = commit_size
            offset += WAL_FRAME_SIZE

        if not last_commit_index:
            return 0
        with open(output_path, "r+b") as target:
            for page_number, encrypted_page in frames[:last_commit_index]:
                target.seek((page_number - 1) * PAGE_SIZE)
                target.write(decrypt_page(self.enc_key, encrypted_page, page_number))
            target.truncate(last_commit_size * PAGE_SIZE)
            target.flush()
            os.fsync(target.fileno())
        return last_commit_index

    def _validate_snapshot(self, path):
        uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            connection.execute("PRAGMA schema_version").fetchone()
            connection.execute(
                "SELECT MAX(local_id) FROM [%s]" % self.table_name
            ).fetchone()

    def refresh(self, force=False):
        with self._lock:
            current = self.fingerprint()
            if (
                not force
                and self.snapshot_path.exists()
                and current == self._fingerprint_value
            ):
                return False
            last_error = None
            for attempt in range(4):
                before = self.fingerprint()
                fd, temp_name = tempfile.mkstemp(
                    prefix="snapshot.", suffix=".db", dir=str(self.cache_dir)
                )
                os.close(fd)
                try:
                    started = time.perf_counter()
                    self._decrypt_database(temp_name)
                    wal_frames = self._patch_wal(temp_name)
                    after = self.fingerprint()
                    if before != after:
                        raise SnapshotRace("database files changed during refresh")
                    self._validate_snapshot(temp_name)
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, self.snapshot_path)
                    self._fingerprint_value = after
                    self._last_refresh_at = time.time()
                    self._last_refresh_duration_ms = (
                        time.perf_counter() - started
                    ) * 1000
                    self._last_wal_frames = wal_frames
                    LOG.info(
                        "snapshot refreshed in %.1fms, committed WAL frames=%d",
                        self._last_refresh_duration_ms,
                        wal_frames,
                    )
                    return True
                except Exception as exc:
                    last_error = exc
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass
                    if attempt < 3:
                        time.sleep(0.04 * (attempt + 1))
            raise RuntimeError("unable to create a stable database snapshot") from last_error

    def _connect(self):
        self.refresh()
        uri = "file:" + urllib.parse.quote(str(self.snapshot_path)) + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection

    def latest_local_id(self):
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(local_id), 0) AS value FROM [%s]" % self.table_name
            ).fetchone()
            return int(row["value"])

    def messages_after(self, after, limit=200):
        after = max(0, int(after))
        limit = min(500, max(1, int(limit)))
        query = """
            SELECT local_id, server_id, local_type, sort_seq, real_sender_id,
                   create_time, status, origin_source, message_content,
                   WCDB_CT_message_content, source, WCDB_CT_source
            FROM [%s]
            WHERE local_id > ?
            ORDER BY local_id ASC
            LIMIT ?
        """ % self.table_name
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, (after, limit)).fetchall()
        return [self._serialize_row(row) for row in rows]

    def messages_before(self, before, limit=20):
        before = max(0, int(before))
        limit = min(100, max(1, int(limit)))
        query = """
            SELECT local_id, server_id, local_type, sort_seq, real_sender_id,
                   create_time, status, origin_source, message_content,
                   WCDB_CT_message_content, source, WCDB_CT_source
            FROM [%s]
            WHERE local_id <= ?
            ORDER BY local_id DESC
            LIMIT ?
        """ % self.table_name
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, (before, limit)).fetchall()
        return [self._serialize_row(row) for row in reversed(rows)]

    def _serialize_row(self, row):
        # Zstd decompression contexts are not safe to share across request and
        # monitor threads. Keep the context local to this serialization.
        decompressor = zstandard.ZstdDecompressor()

        def row_value(name, default=None):
            try:
                return row[name]
            except (KeyError, IndexError, TypeError):
                return default

        content = decode_message_content(
            row["message_content"],
            row["WCDB_CT_message_content"],
            decompressor,
        )
        message_source = decode_message_content(
            row_value("source"),
            row_value("WCDB_CT_source", 0),
            decompressor,
        )
        native_at_user_list = parse_native_at_user_list(message_source)
        bot_wxid = str(getattr(self, "bot_wxid", "") or "").strip()
        native_mentions_bot = bool(
            bot_wxid
            and any(
                hmac.compare_digest(value, bot_wxid)
                for value in native_at_user_list
            )
        )
        sender_wxid, body = split_group_content(content)
        source = int(row["origin_source"] or 0)
        if source == 1:
            direction = "outgoing"
        elif source == 2:
            direction = "incoming"
        else:
            direction = "unknown"
        local_type = int(row["local_type"] or 0)
        delivery_marker = ""
        reply_to_bot = False
        reply_reference = {
            "sender_wxid": "",
            "content": "",
        }
        message_type = "other"
        structured_valid = True
        if direction == "outgoing" and local_type == 1:
            body, delivery_marker = split_tracking_marker(body)
        if local_type == 1:
            message_type = "text"
        elif local_type == 49:
            message_type = "quoted_reply"
            body, reply_to_bot, reply_reference, structured_valid = (
                parse_quoted_reply(body, getattr(self, "bot_wxid", ""))
            )
        visible_mention, prompt = parse_mention(body, self.mention)
        if (native_mentions_bot or reply_to_bot) and not visible_mention:
            prompt = " ".join(body.split())
        return {
            "id": "%s:%s" % (self.group_id, row["local_id"]),
            "group_id": self.group_id,
            "local_id": int(row["local_id"]),
            "server_id": str(row["server_id"] or ""),
            "msg_svr_id": str(row["server_id"] or ""),
            "local_type": local_type,
            "type": message_type,
            "message_type": message_type,
            "sort_seq": int(row["sort_seq"] or 0),
            "sender_numeric_id": int(row["real_sender_id"] or 0),
            "sender_wxid": sender_wxid,
            "timestamp": int(row["create_time"] or 0),
            "status": int(row["status"] or 0),
            "origin_source": source,
            "direction": direction,
            "text": body,
            "delivery_marker": delivery_marker,
            "mentions_bot": native_mentions_bot,
            "native_mentions_bot": native_mentions_bot,
            "visible_mention_candidate": visible_mention,
            "native_at_user_list": native_at_user_list,
            "mention_source": (
                "msg_source_at_user_list" if native_at_user_list else ""
            ),
            "reply_to_bot": reply_to_bot,
            "reply_reference": reply_reference,
            "structured_valid": structured_valid,
            "prompt": prompt,
        }

    def health(self):
        latest = self.latest_local_id()
        return {
            "ok": True,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "latest_local_id": latest,
            "last_refresh_at": self._last_refresh_at,
            "last_refresh_duration_ms": round(self._last_refresh_duration_ms, 1),
            "committed_wal_frames": self._last_wal_frames,
        }


class EventHub:
    CLOSED = object()

    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        client = queue.Queue(maxsize=1000)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)

    def publish(self, message):
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(message)
            except queue.Full:
                LOG.warning("dropping an SSE client whose queue is full")
                self.unsubscribe(client)
                try:
                    while True:
                        client.get_nowait()
                except queue.Empty:
                    pass
                client.put_nowait(self.CLOSED)


class MessageMonitor(threading.Thread):
    def __init__(self, reader, hub, poll_seconds):
        super().__init__(name="message-monitor", daemon=True)
        self.reader = reader
        self.hub = hub
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.last_local_id = 0
        self.started_at = time.time()
        self.last_success_at = 0.0
        self.last_error_at = 0.0
        self.last_cycle_duration_ms = 0.0
        self.consecutive_failures = 0
        self.last_error_type = ""
        self._health_lock = threading.Lock()

    def run(self):
        while True:
            started = time.perf_counter()
            try:
                self.reader.refresh(force=not self.reader.snapshot_path.exists())
                if not self.last_local_id:
                    self.last_local_id = self.reader.latest_local_id()
                else:
                    messages = self.reader.messages_after(self.last_local_id, limit=500)
                    for message in messages:
                        self.hub.publish(message)
                        self.last_local_id = max(
                            self.last_local_id, message["local_id"]
                        )
                with self._health_lock:
                    self.last_success_at = time.time()
                    self.last_cycle_duration_ms = (
                        time.perf_counter() - started
                    ) * 1000
                    self.consecutive_failures = 0
                    self.last_error_type = ""
            except Exception as exc:
                with self._health_lock:
                    self.last_error_at = time.time()
                    self.last_cycle_duration_ms = (
                        time.perf_counter() - started
                    ) * 1000
                    self.consecutive_failures += 1
                    self.last_error_type = type(exc).__name__
                LOG.exception("message monitor refresh failed")
            time.sleep(self.poll_seconds)

    def health(self):
        with self._health_lock:
            return {
                "alive": self.is_alive(),
                "started_at": self.started_at,
                "last_success_at": self.last_success_at,
                "last_error_at": self.last_error_at,
                "last_cycle_duration_ms": round(
                    self.last_cycle_duration_ms, 1
                ),
                "consecutive_failures": self.consecutive_failures,
                "last_error_type": self.last_error_type,
                "last_local_id": self.last_local_id,
            }


class ReadinessMonitor(threading.Thread):
    def __init__(self, application, interval_seconds):
        super().__init__(name="readiness-monitor", daemon=True)
        self.application = application
        self.interval_seconds = max(1.0, float(interval_seconds))

    def run(self):
        while True:
            time.sleep(self.interval_seconds)
            self.application.run_self_check()


class TextSender:
    def __init__(self, config, reader=None, control_store=None):
        self.config = config
        self.reader = reader
        self.control_store = control_store
        self.display = config.get("display", ":99")
        self.window_title = config.get("window_title", "Weixin")
        self.window_class = config.get("window_class", "wechat")
        self.window_geometry = config.get("window_geometry", [0, 0, 728, 650])
        self.group_name = str(config.get("group_name", "")).strip()
        self.search_point = config.get("search_point", [135, 40])
        self.search_popup_result_point = config.get(
            "search_popup_result_point", [100, 130]
        )
        self.search_delay = float(config.get("search_delay_seconds", 0.8))
        self.search_popup_wait = max(
            0.5,
            min(
                10.0,
                float(config.get("search_popup_wait_seconds", 4.0)),
            ),
        )
        self.search_popup_poll = max(
            0.02,
            min(
                0.5,
                float(config.get("search_popup_poll_seconds", 0.06)),
            ),
        )
        configured_popup_height = int(
            config.get(
                "search_popup_min_height",
                int(self.search_popup_result_point[1]) + 10,
            )
        )
        self.search_popup_min_height = max(80, configured_popup_height)
        self.reuse_group_window = bool(
            config.get("reuse_group_window", True)
        )
        self.group_click = config.get("group_click_point", [188, 231])
        self.input_point = config.get("input_point", [480, 585])
        self.send_delay = float(config.get("send_delay_seconds", 0.25))
        self.max_text_chars = max(
            100, int(config.get("max_text_message_chars", 1800))
        )
        self.confirm_timeout = max(
            0.05, float(config.get("send_confirm_timeout_seconds", 6.0))
        )
        self.confirm_poll_seconds = max(
            0.02, float(config.get("send_confirm_poll_seconds", 0.15))
        )
        self.uncertain_retry_seconds = max(
            self.confirm_timeout,
            float(config.get("send_uncertain_retry_seconds", 15.0)),
        )
        self.media_confirm_timeout = max(
            self.confirm_timeout,
            float(config.get("media_confirm_timeout_seconds", 12.0)),
        )
        self.media_paste_delay = max(
            0.0, float(config.get("media_paste_delay_seconds", 1.2))
        )
        self.max_media_download_bytes = max(
            1024,
            int(config.get("max_media_download_bytes", 500 * 1024 * 1024)),
        )
        self.image_prepare_timeout = max(
            0.1, float(config.get("image_prepare_timeout_seconds", 4.0))
        )
        self.image_prepare_poll_seconds = max(
            0.02, float(config.get("image_prepare_poll_seconds", 0.08))
        )
        configured_input_temp = str(config.get("input_temp_dir") or "").strip()
        if configured_input_temp:
            self.input_temp_dir = Path(configured_input_temp).expanduser()
        elif config.get("db_path"):
            db_path = Path(config["db_path"]).expanduser()
            try:
                self.input_temp_dir = db_path.parents[2] / "temp" / "InputTemp"
            except IndexError:
                self.input_temp_dir = None
        else:
            self.input_temp_dir = None
        self.cache_dir = Path(
            config.get("cache_dir", "~/.cache/wechat-chat-api")
        ).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self.state_path = self.cache_dir / "send-state.json"
        self.state_backup_path = self.cache_dir / "send-state.backup.json"
        self.state_marker_path = self.cache_dir / ".send-state-initialized"
        self._lock = threading.Lock()
        self._room_locks_lock = threading.Lock()
        self._room_locks = {}
        self.enter_submit_timeout = min(
            0.75,
            max(
                0.05,
                float(config.get("enter_submit_timeout_seconds", 0.5)),
            ),
        )
        self._barrier_metrics_lock = threading.Lock()
        self._barrier_commit_latencies_ms = []
        self._send_context = threading.local()
        self._media_attempt_callback = None
        self._cleanup_stale_media_artifacts()
        self._state = self._load_state()

    def _cleanup_stale_media_artifacts(self):
        for pattern in ("outgoing-image.*", "outgoing-video.*"):
            for path in self.cache_dir.glob(pattern):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    LOG.exception("failed to remove stale media artifact %s", path)

    @staticmethod
    def _read_state_file(path):
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("send state must be a JSON object")
        if not isinstance(state.get("requests", {}), dict):
            raise ValueError("text request state must be an object")
        if not isinstance(state.get("media_requests", {}), dict):
            raise ValueError("media request state must be an object")
        try:
            revision = int(state.get("state_revision", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("send state revision must be an integer") from exc
        if revision < 0:
            raise ValueError("send state revision must not be negative")
        state.setdefault("requests", {})
        state.setdefault("media_requests", {})
        state["state_revision"] = revision
        return state

    @staticmethod
    def _state_digest(state):
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_state(self):
        primary_error = None
        backup_error = None
        primary = None
        backup = None
        try:
            primary = self._read_state_file(self.state_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            primary_error = "%s: %s" % (self.state_path.name, exc)
        try:
            backup = self._read_state_file(self.state_backup_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            backup_error = "%s: %s" % (self.state_backup_path.name, exc)

        initialized = self.state_marker_path.exists()
        if primary is None and backup is None:
            if not (primary_error or backup_error or initialized):
                self._state_load_health = {
                    "primary_ok": True,
                    "backup_ok": True,
                    "backup_matches": True,
                    "initialized": False,
                    "error_type": "",
                }
                return {
                    "requests": {},
                    "media_requests": {},
                    "state_revision": 0,
                }
            errors = [
                value
                for value in (
                    primary_error,
                    backup_error,
                    "primary state is missing" if primary_error is None else "",
                    "backup state is missing" if backup_error is None else "",
                )
                if value
            ]
            raise RuntimeError(
                "send state is missing or corrupt; refusing to risk duplicate "
                "delivery without modifying state files: %s"
                % "; ".join(errors)
            )

        repair_primary = primary is None
        repair_backup = backup is None
        if primary is not None and backup is not None:
            primary_digest = self._state_digest(primary)
            backup_digest = self._state_digest(backup)
            if hmac.compare_digest(primary_digest, backup_digest):
                selected = primary
            else:
                primary_revision = int(primary.get("state_revision", 0))
                backup_revision = int(backup.get("state_revision", 0))
                if primary_revision > backup_revision:
                    selected = primary
                    repair_backup = True
                elif backup_revision > primary_revision:
                    selected = backup
                    repair_primary = True
                elif primary_revision == 0:
                    # Legacy saves wrote primary first, then backup. A valid
                    # mismatch at revision zero therefore means primary won
                    # the last atomic replace before the process stopped.
                    selected = primary
                    repair_backup = True
                else:
                    raise RuntimeError(
                        "send state copies have the same revision but different "
                        "content; refusing to guess which delivery ledger is newer"
                    )
        else:
            selected = primary if primary is not None else backup

        try:
            if repair_backup:
                atomic_write_json(self.state_backup_path, selected)
            if repair_primary:
                atomic_write_json(self.state_path, selected)
            if repair_primary or repair_backup or not initialized:
                self.state_marker_path.write_text(
                    "initialized\n",
                    encoding="ascii",
                )
                os.chmod(self.state_marker_path, 0o600)
                initialized = True
        except Exception as exc:
            raise RuntimeError(
                "send state recovery failed before outbound delivery was enabled"
            ) from exc

        self._state_load_health = {
            "primary_ok": True,
            "backup_ok": True,
            "backup_matches": True,
            "initialized": initialized,
            "error_type": "",
        }
        return selected

    def health(self):
        load_health = dict(getattr(self, "_state_load_health", {}))
        state = self._state
        primary_ok = bool(load_health.get("primary_ok", False))
        backup_ok = bool(load_health.get("backup_ok", False))
        backup_matches = bool(load_health.get("backup_matches", False))
        initialized = bool(load_health.get("initialized", False))
        if not initialized and not self.state_path.exists():
            backup_ok = True
            backup_matches = True
        with self._barrier_metrics_lock:
            latencies = sorted(self._barrier_commit_latencies_ms)
        p95_latency = (
            latencies[max(0, ((len(latencies) * 95 + 99) // 100) - 1)]
            if latencies
            else 0.0
        )
        return {
            "ok": bool(primary_ok and backup_ok and backup_matches),
            "primary_ok": primary_ok,
            "backup_ok": backup_ok,
            "backup_matches": backup_matches,
            "initialized": initialized,
            "text_requests": len(state.get("requests", {})),
            "media_requests": len(state.get("media_requests", {})),
            "state_revision": int(state.get("state_revision", 0)),
            "barrier_commit_count": len(latencies),
            "barrier_commit_p95_ms": round(p95_latency, 1),
            "error_type": str(load_health.get("error_type") or ""),
        }

    def window_health(self):
        window_id = self._find_window()
        return {
            "ok": True,
            "window_found": True,
            "window_id": str(window_id),
        }

    def _save_state(self):
        for key in ("requests", "media_requests"):
            requests = self._state.setdefault(key, {})
            protected = {
                request_id: value
                for request_id, value in requests.items()
                if request_id.startswith("task:")
                or str(value.get("status") or "") in {"sending", "uncertain"}
            }
            evictable = [
                item
                for item in requests.items()
                if item[0] not in protected
            ]
            if len(evictable) > 500:
                evictable = sorted(
                    evictable,
                    key=lambda item: float(item[1].get("updated_at", 0)),
                )[-500:]
            self._state[key] = {
                **dict(evictable),
                **protected,
            }
        self._state["state_revision"] = (
            int(self._state.get("state_revision", 0)) + 1
        )
        try:
            atomic_write_json(self.state_backup_path, self._state)
            atomic_write_json(self.state_path, self._state)
            self.state_marker_path.write_text("initialized\n", encoding="ascii")
            os.chmod(self.state_marker_path, 0o600)
        except Exception:
            self._state_load_health = {
                "primary_ok": False,
                "backup_ok": False,
                "backup_matches": False,
                "initialized": self.state_marker_path.exists(),
                "error_type": "state_write_failed",
            }
            raise
        self._state_load_health = {
            "primary_ok": True,
            "backup_ok": True,
            "backup_matches": True,
            "initialized": True,
            "error_type": "",
        }

    def _environment(self):
        environment = os.environ.copy()
        environment["DISPLAY"] = self.display
        environment["LANG"] = "C.UTF-8"
        environment["LC_ALL"] = "C.UTF-8"
        return environment

    def _run(self, command, timeout=6):
        result = subprocess.run(
            command,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(
                "%s failed: %s"
                % (command[0], (result.stderr or result.stdout).strip()[:300])
            )
        return result

    def _find_window(self):
        result = self._run(["xdotool", "search", "--name", self.window_title])
        windows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not windows:
            raise RuntimeError("Weixin window was not found")
        return windows[0]

    def _click(self, window_id, point):
        x, y = point
        self._run(
            [
                "xdotool",
                "mousemove",
                "--window",
                window_id,
                str(x),
                str(y),
            ]
        )
        self._run(["xdotool", "click", "--window", window_id, "1"])

    def _set_clipboard(self, text):
        subprocess.run(
            ["pkill", "-9", "-x", "xclip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process = subprocess.Popen(
            [
                "xclip",
                "-selection",
                "clipboard",
                "-target",
                "UTF8_STRING",
                "-i",
            ],
            env=self._environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()
        except Exception:
            process.kill()
            raise
        time.sleep(0.07)

    def _paste(self, window_id):
        self._run(
            [
                "xdotool",
                "key",
                "--window",
                window_id,
                "--clearmodifiers",
                "ctrl+a",
            ]
        )
        self._run(
            [
                "xdotool",
                "key",
                "--window",
                window_id,
                "--clearmodifiers",
                "ctrl+v",
            ]
        )

    def _window_geometry_for(self, window_id):
        result = self._run(
            ["xdotool", "getwindowgeometry", "--shell", str(window_id)]
        )
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = int(value)
        return values

    def _window_name_for(self, window_id):
        result = self._run(["xdotool", "getwindowname", str(window_id)])
        return result.stdout.strip()

    def _visible_wechat_windows(self):
        result = self._run(
            [
                "xdotool",
                "search",
                "--onlyvisible",
                "--class",
                self.window_class,
            ]
        )
        return list(dict.fromkeys(result.stdout.split()))

    def _find_open_group_window(self, main_window_id):
        """Return the unique visible group window, when it is already open."""
        if not self.group_name:
            return None
        try:
            window_ids = self._visible_wechat_windows()
        except RuntimeError:
            return None

        candidates = []
        for window_id in window_ids:
            if window_id == str(main_window_id):
                continue
            try:
                if self._window_name_for(window_id) != self.group_name:
                    continue
                geometry = self._window_geometry_for(window_id)
            except (RuntimeError, ValueError):
                continue
            if geometry.get("WIDTH", 0) < 500 or geometry.get("HEIGHT", 0) < 400:
                continue
            candidates.append((geometry.get("WIDTH", 0) * geometry.get("HEIGHT", 0), window_id))

        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            LOG.warning(
                "multiple visible Weixin group windows matched configured title; "
                "falling back to search"
            )
        return None

    def _activate_window(self, window_id):
        self._run(["xdotool", "windowmap", str(window_id)])
        self._run(["xdotool", "windowactivate", str(window_id)])
        time.sleep(0.05)

    def _find_search_popup(self, main_window_id):
        deadline = time.monotonic() + self.search_popup_wait
        last_candidates = []
        last_error = ""
        while time.monotonic() < deadline:
            try:
                window_ids = self._visible_wechat_windows()
            except RuntimeError as exc:
                last_error = type(exc).__name__
                time.sleep(self.search_popup_poll)
                continue
            candidates = []
            for window_id in window_ids:
                if window_id == str(main_window_id):
                    continue
                try:
                    geometry = self._window_geometry_for(window_id)
                except (RuntimeError, ValueError):
                    continue
                width = geometry.get("WIDTH", 0)
                height = geometry.get("HEIGHT", 0)
                if (
                    200 <= width <= 500
                    and self.search_popup_min_height <= height <= 300
                ):
                    candidates.append((width * height, window_id, width, height))
            last_candidates = candidates
            if len(candidates) == 1:
                return candidates[0][1]
            if len(candidates) > 1:
                raise SearchPopupAmbiguousError(
                    "multiple possible Weixin search popups were found; refusing "
                    "to guess"
                )
            time.sleep(self.search_popup_poll)
        diagnostic = {
            "candidates": [
                {
                    "window_id": item[1],
                    "width": item[2],
                    "height": item[3],
                }
                for item in last_candidates
            ],
            "last_error": last_error,
        }
        LOG.warning("Weixin search popup was not found: %s", diagnostic)
        raise SearchPopupNotFoundError(
            "Weixin search results popup was not found"
        )

    def _open_group(self, window_id):
        if not self.group_name:
            self._click(window_id, self.group_click)
            time.sleep(0.18)
            return window_id

        if self.reuse_group_window:
            existing = self._find_open_group_window(window_id)
            if existing:
                self._activate_window(existing)
                return existing

        self._set_clipboard(self.group_name)
        popup_window_id = None
        for attempt in range(2):
            self._click(window_id, self.search_point)
            self._paste(window_id)
            # Start polling quickly so a transient small popup can settle while
            # the finder waits for the result row to become clickable.
            time.sleep(max(0.05, min(self.search_delay, 0.2)))
            try:
                popup_window_id = self._find_search_popup(window_id)
                break
            except SearchPopupNotFoundError:
                if attempt:
                    raise
                try:
                    self._run(
                        [
                            "xdotool",
                            "key",
                            "--window",
                            str(window_id),
                            "--clearmodifiers",
                            "Escape",
                        ]
                    )
                except RuntimeError:
                    LOG.debug("failed to clear stale Weixin search popup", exc_info=True)
                time.sleep(0.12)

        self._click(popup_window_id, self.search_popup_result_point)
        time.sleep(0.5)
        if self.reuse_group_window:
            selected = self._find_open_group_window(window_id)
            if selected:
                self._activate_window(selected)
                return selected
        return window_id

    def _activate_target_window(self):
        window_id = self._find_window()
        x, y, width, height = self.window_geometry
        for command in (
            ["xdotool", "windowmap", window_id],
            ["xdotool", "windowactivate", window_id],
            ["xdotool", "windowmove", window_id, str(x), str(y)],
            ["xdotool", "windowsize", window_id, str(width), str(height)],
        ):
            self._run(command)
        return self._open_group(window_id)

    def _room_lock(self, room_id):
        room_id = str(room_id or "").strip()
        with self._room_locks_lock:
            lock = self._room_locks.get(room_id)
            if lock is None:
                lock = threading.RLock()
                self._room_locks[room_id] = lock
            return lock

    def _current_send_context(self):
        value = getattr(self._send_context, "value", None)
        return dict(value or {})

    def _set_send_context(
        self,
        room_id="",
        source_local_id=0,
        task_id="",
        generation=0,
        item_kind="text",
    ):
        self._send_context.value = {
            "room_id": str(room_id or "").strip(),
            "source_local_id": int(source_local_id or 0),
            "task_id": str(task_id or "").strip(),
            "generation": int(generation or 0),
            "item_kind": str(item_kind or "text").strip().lower(),
        }

    def _clear_send_context(self):
        self._send_context.value = None

    def _assert_current_send_allowed(self):
        if self.control_store is None:
            return
        context = self._current_send_context()
        if not context.get("room_id"):
            raise ValueError("room_id is required for controlled delivery")
        if context.get("source_local_id", 0) <= 0:
            raise ValueError(
                "source_local_id must be positive for controlled delivery"
            )
        if context.get("task_id") and context.get("generation", 0) <= 0:
            raise ValueError(
                "task deliveries require a positive generation"
            )
        result = self.control_store.check(**context)
        if result["allowed"]:
            return
        barrier = result["barrier"] or {}
        raise OutboundSuppressedError(
            "outbound item was suppressed by barrier %s" % barrier.get("id"),
            barrier=barrier,
        )

    def commit_barrier(
        self,
        room_id,
        source_local_id,
        mode,
        task_id="",
        generation=0,
        reason="",
    ):
        if self.control_store is None:
            raise RuntimeError("outbound control store is unavailable")
        started_at = time.monotonic()
        try:
            with self._room_lock(room_id):
                return self.control_store.commit(
                    room_id,
                    source_local_id,
                    mode,
                    task_id=task_id,
                    generation=generation,
                    reason=reason,
                )
        finally:
            elapsed_ms = (time.monotonic() - started_at) * 1000
            with self._barrier_metrics_lock:
                self._barrier_commit_latencies_ms.append(elapsed_ms)
                del self._barrier_commit_latencies_ms[:-256]

    def check_barrier(
        self,
        room_id,
        source_local_id,
        item_kind,
        task_id="",
        generation=0,
    ):
        if self.control_store is None:
            raise RuntimeError("outbound control store is unavailable")
        with self._room_lock(room_id):
            return self.control_store.check(
                room_id,
                source_local_id,
                item_kind,
                task_id=task_id,
                generation=generation,
            )

    def _clear_text_composer(self, window_id):
        self._clear_media_composer(window_id)

    def _send_once(self, text):
        self._assert_current_send_allowed()
        window_id = self._activate_target_window()
        self._set_clipboard(text)
        self._click(window_id, self.input_point)
        self._assert_current_send_allowed()
        self._paste(window_id)
        time.sleep(max(0.15, self.send_delay))
        context = self._current_send_context()
        try:
            with self._room_lock(context.get("room_id")):
                self._assert_current_send_allowed()
                self._run(
                    [
                        "xdotool",
                        "key",
                        "--window",
                        window_id,
                        "--clearmodifiers",
                        "Return",
                    ],
                    timeout=self.enter_submit_timeout,
                )
        except OutboundSuppressedError:
            self._clear_text_composer(window_id)
            raise
        time.sleep(0.15)

    def _set_image_clipboard(self, path):
        subprocess.run(
            ["pkill", "-9", "-x", "xclip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.Popen(
            [
                "xclip",
                "-selection",
                "clipboard",
                "-target",
                "image/png",
                "-i",
                str(path),
            ],
            env=self._environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.2)

    def _set_file_clipboard(self, path):
        subprocess.run(
            ["pkill", "-9", "-x", "xclip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process = subprocess.Popen(
            [
                "xclip",
                "-selection",
                "clipboard",
                "-target",
                "text/uri-list",
                "-i",
            ],
            env=self._environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            process.stdin.write((Path(path).resolve().as_uri() + "\r\n").encode("utf-8"))
            process.stdin.close()
        except Exception:
            process.kill()
            raise
        time.sleep(0.2)

    def _clear_media_composer(self, window_id):
        try:
            self._click(window_id, self.input_point)
            for key in ("ctrl+a", "BackSpace"):
                self._run(
                    [
                        "xdotool",
                        "key",
                        "--window",
                        window_id,
                        "--clearmodifiers",
                        key,
                    ]
                )
        except Exception:
            LOG.exception("failed to clear unsent media from the Weixin composer")

    def _paste_media_and_send(self, window_id, before_send=None):
        self._assert_current_send_allowed()
        self._click(window_id, self.input_point)
        self._run(
            [
                "xdotool",
                "key",
                "--window",
                window_id,
                "--clearmodifiers",
                "ctrl+v",
            ]
        )
        time.sleep(self.media_paste_delay)
        if before_send is not None:
            try:
                before_send()
            except MediaNotSentError:
                self._clear_media_composer(window_id)
                raise
            except Exception as exc:
                self._clear_media_composer(window_id)
                raise MediaNotSentError(
                    "image was not sent because its confirmation fingerprint "
                    "could not be persisted"
                ) from exc
        context = self._current_send_context()
        try:
            with self._room_lock(context.get("room_id")):
                self._assert_current_send_allowed()
                self._run(
                    [
                        "xdotool",
                        "key",
                        "--window",
                        window_id,
                        "--clearmodifiers",
                        "Return",
                    ],
                    timeout=self.enter_submit_timeout,
                )
        except OutboundSuppressedError:
            self._clear_media_composer(window_id)
            raise
        time.sleep(0.5)

    def _input_temp_snapshot(self):
        if self.input_temp_dir is None:
            raise MediaNotSentError(
                "image was not sent because InputTemp could not be derived"
            )
        try:
            paths = list(self.input_temp_dir.iterdir())
        except Exception as exc:
            raise MediaNotSentError(
                "image was not sent because InputTemp could not be inspected"
            ) from exc
        snapshot = {}
        for path in paths:
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except (FileNotFoundError, OSError):
                continue
            snapshot[str(path)] = (int(stat.st_size), int(stat.st_mtime_ns))
        return snapshot

    def _capture_reencoded_image_fingerprint(self, before):
        deadline = time.monotonic() + self.image_prepare_timeout
        stable_path = None
        stable_signature = None
        stable_polls = 0
        while True:
            current = self._input_temp_snapshot()
            candidates = [
                (Path(path), signature)
                for path, signature in current.items()
                if before.get(path) != signature
            ]
            if len(candidates) > 1:
                raise MediaNotSentError(
                    "image was not sent because multiple new InputTemp files "
                    "were detected"
                )
            if len(candidates) == 1:
                path, signature = candidates[0]
                if (
                    path == stable_path
                    and signature == stable_signature
                    and signature[0] > 0
                ):
                    stable_polls += 1
                else:
                    stable_path = path
                    stable_signature = signature
                    stable_polls = 1
                if stable_polls >= 2:
                    try:
                        fingerprint = file_fingerprint(path)
                        stat = path.stat()
                    except (FileNotFoundError, OSError):
                        stable_polls = 0
                    else:
                        final_signature = (
                            int(stat.st_size),
                            int(stat.st_mtime_ns),
                        )
                        if final_signature == signature:
                            return fingerprint
                        stable_signature = final_signature
                        stable_polls = 0
            else:
                stable_path = None
                stable_signature = None
                stable_polls = 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaNotSentError(
                    "image was not sent because no unique re-encoded InputTemp "
                    "file appeared"
                )
            time.sleep(min(self.image_prepare_poll_seconds, remaining))

    def _send_image_once(self, encoded):
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("image data is not valid base64") from exc
        if not payload:
            raise ValueError("image data is empty")
        if len(payload) > 20 * 1024 * 1024:
            raise ValueError("image data exceeds 20 MiB")
        source_fd, source_name = tempfile.mkstemp(
            prefix="outgoing-image.", dir=str(self.cache_dir)
        )
        os.close(source_fd)
        output_name = source_name + ".png"
        try:
            with open(source_name, "wb") as handle:
                handle.write(payload)
            self._run(
                [
                    "convert",
                    source_name,
                    "-auto-orient",
                    "-strip",
                    output_name,
                ],
                timeout=30,
            )
            if not Path(output_name).exists() or Path(output_name).stat().st_size == 0:
                raise RuntimeError("image conversion produced no output")
            input_temp_before = self._input_temp_snapshot()
            window_id = self._activate_target_window()
            self._set_image_clipboard(output_name)
            media_info = {}

            def remember_reencoded_image():
                media_info.update(
                    self._capture_reencoded_image_fingerprint(input_temp_before)
                )
                self._publish_media_attempt(media_info)

            try:
                self._paste_media_and_send(
                    window_id,
                    before_send=remember_reencoded_image,
                )
            except MediaNotSentError:
                raise
            except Exception as exc:
                if not media_info:
                    self._clear_media_composer(window_id)
                    raise MediaNotSentError(
                        "image was not sent because the paste step failed before "
                        "a confirmation fingerprint was recorded"
                    ) from exc
                raise
            return media_info
        finally:
            for path in (source_name, output_name):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def _send_video_once(self, url):
        return self._send_remote_file_once(url, "video")

    def _send_file_once(self, url):
        return self._send_remote_file_once(url, "file")

    def _send_remote_file_once(self, url, media_type):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("%s URL must use http or https" % media_type)
        suffix = ".mp4"
        if media_type == "file":
            candidate = Path(urllib.parse.unquote(parsed.path)).suffix
            if (
                candidate
                and len(candidate) <= 16
                and re.fullmatch(r"\.[A-Za-z0-9._-]+", candidate)
            ):
                suffix = candidate
            else:
                suffix = ".bin"
        fd, video_name = tempfile.mkstemp(
            prefix="outgoing-%s." % media_type,
            suffix=suffix,
            dir=str(self.cache_dir),
        )
        os.close(fd)
        media_prepared = False
        try:
            self._run(
                [
                    "curl",
                    "-L",
                    "--fail",
                    "--connect-timeout",
                    "8",
                    "--max-time",
                    "90",
                    "--max-filesize",
                    str(self.max_media_download_bytes),
                    "-o",
                    video_name,
                    url,
                ],
                timeout=100,
            )
            size = Path(video_name).stat().st_size
            minimum_size = 1024 if media_type == "video" else 1
            if size < minimum_size:
                raise RuntimeError(
                    "downloaded %s is empty or invalid" % media_type
                )
            if size > self.max_media_download_bytes:
                raise RuntimeError(
                    "downloaded %s exceeds the configured size limit" % media_type
                )
            media_info = file_fingerprint(video_name)
            media_info["artifact_path"] = video_name
            media_prepared = True
            self._publish_media_attempt(media_info)
            window_id = self._activate_target_window()
            self._set_file_clipboard(video_name)
            self._paste_media_and_send(window_id)
            return media_info
        except Exception:
            if not media_prepared:
                try:
                    os.unlink(video_name)
                except FileNotFoundError:
                    pass
            raise

    def _publish_media_attempt(self, media_info):
        callback = self._media_attempt_callback
        if callback is not None:
            callback(dict(media_info))

    @staticmethod
    def _text_hash(text):
        return hashlib.sha256(canonical_message_text(text).encode("utf-8")).hexdigest()

    def _check_request_text(self, request_id, entry, text_hash):
        stored_hash = str(entry.get("text_hash") or "")
        if request_id and stored_hash and not hmac.compare_digest(stored_hash, text_hash):
            raise IdempotencyConflict(
                "request_id was already used with different message text"
            )

    @staticmethod
    def _check_request_context(
        request_id,
        entry,
        room_id,
        source_local_id,
        task_id,
        generation,
    ):
        if not request_id or not entry:
            return
        expected = {
            "room_id": str(room_id or "").strip(),
            "source_local_id": int(source_local_id or 0),
            "task_id": str(task_id or "").strip(),
            "generation": int(generation or 0),
        }
        for key, value in expected.items():
            if key not in entry:
                continue
            stored = entry.get(key)
            if key in {"source_local_id", "generation"}:
                try:
                    stored = int(stored or 0)
                except (TypeError, ValueError):
                    raise IdempotencyConflict(
                        "saved request envelope is invalid"
                    )
            else:
                stored = str(stored or "").strip()
            if stored != value:
                raise IdempotencyConflict(
                    "request_id was already used with a different trusted envelope"
                )

    def _scan_confirmation(self, baseline_local_id, text):
        if self.reader is None:
            raise RuntimeError("database reader is required to confirm message delivery")
        expected_text, expected_marker = split_tracking_marker(text)
        expected_text = canonical_message_text(expected_text)
        cursor = max(0, int(baseline_local_id))
        while True:
            messages = self.reader.messages_after(cursor, limit=500)
            for message in messages:
                candidate_text, embedded_marker = split_tracking_marker(
                    message.get("text")
                )
                candidate_marker = str(
                    message.get("delivery_marker") or embedded_marker
                )
                if (
                    message.get("direction") == "outgoing"
                    and int(message.get("origin_source", 0)) == 1
                    and int(message.get("local_type", 0)) == 1
                    and canonical_message_text(candidate_text) == expected_text
                    and candidate_marker == expected_marker
                ):
                    return message
            if len(messages) < 500:
                return None
            cursor = int(messages[-1]["local_id"])

    def _scan_request_confirmation(self, baseline_local_id, request_id):
        if self.reader is None:
            raise RuntimeError("database reader is required to confirm message delivery")
        expected_marker = request_tracking_marker(request_id)
        if not expected_marker:
            return None
        cursor = max(0, int(baseline_local_id))
        while True:
            messages = self.reader.messages_after(cursor, limit=500)
            for message in messages:
                _candidate_text, embedded_marker = split_tracking_marker(
                    message.get("text")
                )
                candidate_marker = str(
                    message.get("delivery_marker") or embedded_marker
                )
                if (
                    message.get("direction") == "outgoing"
                    and int(message.get("origin_source", 0)) == 1
                    and int(message.get("local_type", 0)) == 1
                    and hmac.compare_digest(
                        candidate_marker.encode("utf-8"),
                        expected_marker.encode("utf-8"),
                    )
                ):
                    return message
            if len(messages) < 500:
                return None
            cursor = int(messages[-1]["local_id"])

    def _wait_for_confirmation(self, baseline_local_id, text, timeout=None):
        timeout = self.confirm_timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        while True:
            confirmation = self._scan_confirmation(baseline_local_id, text)
            if confirmation is not None:
                return confirmation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.confirm_poll_seconds, remaining))

    def _scan_media_confirmation(self, baseline_local_id, media_type, expected):
        expected_local_type = {"image": 3, "video": 43, "file": 49}[media_type]
        expected_md5 = str((expected or {}).get("content_md5") or "").lower()
        try:
            expected_length = int((expected or {}).get("content_length") or 0)
        except (TypeError, ValueError):
            expected_length = 0
        if not expected_md5:
            return None
        cursor = max(0, int(baseline_local_id))
        while True:
            messages = self.reader.messages_after(cursor, limit=500)
            for message in messages:
                if not (
                    message.get("direction") == "outgoing"
                    and int(message.get("origin_source", 0)) == 1
                    and int(message.get("local_type", 0)) == expected_local_type
                ):
                    continue
                for fingerprint in media_message_fingerprints(
                    message.get("text"),
                    media_type,
                ):
                    same_md5 = hmac.compare_digest(
                        fingerprint["content_md5"],
                        expected_md5,
                    )
                    same_length = (
                        not expected_length
                        or int(fingerprint["content_length"]) == expected_length
                    )
                    if same_md5 and same_length:
                        return message
            if len(messages) < 500:
                return None
            cursor = int(messages[-1]["local_id"])

    def _wait_for_media_confirmation(
        self,
        baseline_local_id,
        media_type,
        expected,
        timeout=None,
    ):
        timeout = (
            self.media_confirm_timeout if timeout is None else max(0.0, float(timeout))
        )
        deadline = time.monotonic() + timeout
        while True:
            confirmation = self._scan_media_confirmation(
                baseline_local_id,
                media_type,
                expected,
            )
            if confirmation is not None:
                return confirmation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.confirm_poll_seconds, remaining))

    def _mark_sent(self, request_id, entry, confirmation, deduplicated):
        sent_at = time.time()
        confirmed_local_id = int(confirmation["local_id"])
        if request_id:
            entry.update(
                {
                    "status": "sent",
                    "updated_at": sent_at,
                    "sent_at": sent_at,
                    "confirmed_local_id": confirmed_local_id,
                }
            )
            entry.pop("error", None)
            self._state["requests"][request_id] = entry
            self._save_state()
        return {
            "ok": True,
            "status": "sent",
            "deduplicated": bool(deduplicated),
            "request_id": request_id,
            "sent_at": sent_at,
            "confirmed_local_id": confirmed_local_id,
        }

    def _mark_uncertain(self, request_id, entry, error):
        now = time.time()
        if request_id:
            entry.update(
                {
                    "status": "uncertain",
                    "updated_at": now,
                    "uncertain_since": float(entry.get("uncertain_since") or now),
                    "error": str(error)[:300],
                }
            )
            self._state["requests"][request_id] = entry
            self._save_state()
        raise SendUncertainError(str(error))

    def delivery_status(
        self,
        request_id,
        item_kind,
        room_id="",
        source_local_id=0,
        task_id="",
        generation=0,
    ):
        item_kind = str(item_kind or "").strip().lower()
        if item_kind not in {"text", "image", "video", "file"}:
            raise ValueError("item_kind must be text, image, video, or file")
        request_id = str(request_id or "").strip()
        if self.control_store is not None:
            envelope = normalize_outbound_envelope(
                {
                    "request_id": request_id,
                    "source_local_id": source_local_id,
                    "task_id": task_id,
                    "generation": generation,
                }
            )
            request_id = envelope["request_id"]
            source_local_id = envelope["source_local_id"]
            task_id = envelope["task_id"]
            generation = envelope["generation"]
        if not request_id:
            raise ValueError("request_id is required")

        with self._lock:
            state_key = "requests" if item_kind == "text" else "media_requests"
            requests = self._state.setdefault(state_key, {})
            entry = dict(requests.get(request_id) or {})
            if not entry:
                return {
                    "ok": True,
                    "status": "not_submitted",
                    "request_id": request_id,
                }
            self._check_request_context(
                request_id,
                entry,
                room_id,
                source_local_id,
                task_id,
                generation,
            )
            if item_kind != "text":
                saved_kind = str(entry.get("media_type") or "").strip().lower()
                if saved_kind and saved_kind != item_kind:
                    raise IdempotencyConflict(
                        "request_id was already used with a different media type"
                    )

            status = str(entry.get("status") or "").strip().lower()
            if status == "sent":
                return {
                    "ok": True,
                    "status": "confirmed",
                    "request_id": request_id,
                    "confirmed_local_id": entry.get("confirmed_local_id"),
                    "media_fingerprint": (
                        "%s:%s"
                        % (
                            str(entry.get("content_md5") or ""),
                            str(entry.get("content_length") or ""),
                        )
                        if item_kind != "text"
                        else ""
                    ),
                }
            if status == "suppressed":
                return {
                    "ok": True,
                    "status": "suppressed",
                    "request_id": request_id,
                }
            if status == "failed":
                return {
                    "ok": True,
                    "status": "not_submitted",
                    "request_id": request_id,
                }

            baseline = int(entry.get("baseline_local_id") or 0)
            if item_kind == "text":
                confirmation = self._scan_request_confirmation(
                    baseline,
                    request_id,
                )
            else:
                expected = {
                    "content_md5": entry.get("content_md5"),
                    "content_length": entry.get("content_length"),
                }
                confirmation = self._scan_media_confirmation(
                    baseline,
                    item_kind,
                    expected,
                )
            if confirmation is not None:
                if item_kind == "text":
                    sent = self._mark_sent(
                        request_id,
                        entry,
                        confirmation,
                        deduplicated=True,
                    )
                else:
                    sent_at = time.time()
                    confirmed_local_id = int(confirmation["local_id"])
                    entry.update(
                        {
                            "status": "sent",
                            "updated_at": sent_at,
                            "sent_at": sent_at,
                            "confirmed_local_id": confirmed_local_id,
                        }
                    )
                    entry.pop("error", None)
                    requests[request_id] = entry
                    self._save_state()
                    sent = {
                        "confirmed_local_id": confirmed_local_id,
                    }
                return {
                    "ok": True,
                    "status": "confirmed",
                    "request_id": request_id,
                    "confirmed_local_id": sent.get("confirmed_local_id"),
                    "media_fingerprint": (
                        "%s:%s"
                        % (
                            str(entry.get("content_md5") or ""),
                            str(entry.get("content_length") or ""),
                        )
                        if item_kind != "text"
                        else ""
                    ),
                }
            return {
                "ok": True,
                "status": "uncertain",
                "request_id": request_id,
                "error_type": "confirmation_not_found",
            }

    @staticmethod
    def _suppressed_result(request_id, barrier, media_type=""):
        result = {
            "ok": False,
            "status": "suppressed",
            "request_id": request_id,
            "barrier": dict(barrier or {}),
        }
        if media_type:
            result["media_type"] = media_type
        return result

    def send(
        self,
        text,
        request_id="",
        room_id="",
        source_local_id=0,
        task_id="",
        generation=0,
    ):
        text = canonical_message_text(text)
        if not text:
            raise ValueError("message text is empty")
        if len(text) > self.max_text_chars:
            raise ValueError(
                "message text exceeds %d characters" % self.max_text_chars
            )
        request_id = str(request_id or "").strip()
        if self.control_store is not None:
            envelope = normalize_outbound_envelope(
                {
                    "request_id": request_id,
                    "source_local_id": source_local_id,
                    "task_id": task_id,
                    "generation": generation,
                }
            )
            request_id = envelope["request_id"]
            source_local_id = envelope["source_local_id"]
            task_id = envelope["task_id"]
            generation = envelope["generation"]
        text_hash = self._text_hash(text)
        wire_text = tracked_message_text(text, request_id)
        with self._lock:
            requests = self._state.setdefault("requests", {})
            entry = dict(requests.get(request_id) or {})
            self._check_request_text(request_id, entry, text_hash)
            self._check_request_context(
                request_id,
                entry,
                room_id,
                source_local_id,
                task_id,
                generation,
            )
            if request_id and entry and entry.get("status") == "sent":
                return {
                    "ok": True,
                    "status": "sent",
                    "deduplicated": True,
                    "request_id": request_id,
                    "sent_at": entry.get("sent_at"),
                    "confirmed_local_id": entry.get("confirmed_local_id"),
                }
            now = time.time()
            if (
                request_id
                and entry
                and entry.get("status") == "sending"
                and now - float(entry.get("updated_at", 0)) < 30
            ):
                raise IdempotencyConflict("the same request is already being sent")

            if request_id and entry.get("status") in {"sending", "uncertain"}:
                baseline = int(entry.get("baseline_local_id", 0))
                confirmation = self._wait_for_confirmation(
                    baseline,
                    wire_text,
                    timeout=self.confirm_poll_seconds,
                )
                if confirmation is not None:
                    return self._mark_sent(
                        request_id, entry, confirmation, deduplicated=True
                    )
                raise SendUncertainError(
                    "previous text send remains uncertain; refusing to resend "
                    "automatically"
                )

            if self.reader is None:
                raise RuntimeError("database reader is required to confirm message delivery")
            baseline_local_id = self.reader.latest_local_id()
            entry = {
                "status": "sending",
                "text_hash": text_hash,
                "baseline_local_id": baseline_local_id,
                "attempted_at": now,
                "updated_at": now,
                "room_id": str(room_id or ""),
                "source_local_id": int(source_local_id or 0),
                "task_id": str(task_id or ""),
                "generation": int(generation or 0),
            }
            if request_id:
                requests[request_id] = entry
                self._save_state()
            self._set_send_context(
                room_id=room_id,
                source_local_id=source_local_id,
                task_id=task_id,
                generation=generation,
                item_kind="text",
            )
            try:
                self._send_once(wire_text)
            except OutboundSuppressedError as exc:
                now = time.time()
                entry.update(
                    {
                        "status": "suppressed",
                        "updated_at": now,
                        "barrier_id": exc.barrier.get("id"),
                    }
                )
                if request_id:
                    requests[request_id] = entry
                    self._save_state()
                raise
            except Exception as exc:
                confirmation = self._wait_for_confirmation(
                    baseline_local_id,
                    wire_text,
                    timeout=min(1.0, self.confirm_timeout),
                )
                if confirmation is not None:
                    return self._mark_sent(
                        request_id, entry, confirmation, deduplicated=False
                    )
                self._mark_uncertain(
                    request_id,
                    entry,
                    "UI send failed and delivery could not be confirmed: %s" % exc,
                )
            finally:
                self._clear_send_context()

            confirmation = self._wait_for_confirmation(
                baseline_local_id,
                wire_text,
            )
            if confirmation is None:
                self._mark_uncertain(
                    request_id,
                    entry,
                    "message delivery was not confirmed in the target group",
                )
            return self._mark_sent(
                request_id, entry, confirmation, deduplicated=False
            )

    def send_media(
        self,
        media_type,
        payload,
        request_id="",
        room_id="",
        source_local_id=0,
        task_id="",
        generation=0,
    ):
        media_type = str(media_type or "").strip().lower()
        if media_type not in {"image", "video", "file"}:
            raise ValueError("media type must be image, video, or file")
        payload = str(payload or "").strip()
        if not payload:
            raise ValueError("media payload is empty")
        request_id = str(request_id or "").strip()
        if self.control_store is not None:
            envelope = normalize_outbound_envelope(
                {
                    "request_id": request_id,
                    "source_local_id": source_local_id,
                    "task_id": task_id,
                    "generation": generation,
                }
            )
            request_id = envelope["request_id"]
            source_local_id = envelope["source_local_id"]
            task_id = envelope["task_id"]
            generation = envelope["generation"]
        content_hash = hashlib.sha256(
            (media_type + "\0" + payload).encode("utf-8")
        ).hexdigest()

        def expected_fingerprint(values):
            content_md5 = str(values.get("content_md5") or "").strip().lower()
            try:
                content_length = int(values.get("content_length") or 0)
            except (TypeError, ValueError):
                return None
            if not content_md5 or content_length <= 0:
                return None
            return {
                "content_md5": content_md5,
                "content_length": content_length,
            }

        def cleanup_artifact(path):
            if not path:
                return
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                LOG.exception("failed to remove media artifact")

        with self._lock:
            requests = self._state.setdefault("media_requests", {})
            entry = dict(requests.get(request_id) or {})
            self._check_request_context(
                request_id,
                entry,
                room_id,
                source_local_id,
                task_id,
                generation,
            )
            stored_hash = str(entry.get("content_hash") or "")
            if (
                request_id
                and stored_hash
                and not hmac.compare_digest(stored_hash, content_hash)
            ):
                raise IdempotencyConflict(
                    "request_id was already used with different media"
                )
            if request_id and entry.get("status") == "sent":
                return {
                    "ok": True,
                    "status": "sent",
                    "deduplicated": True,
                    "request_id": request_id,
                    "sent_at": entry.get("sent_at"),
                    "confirmed_local_id": entry.get("confirmed_local_id"),
                    "media_type": media_type,
                }
            now = time.time()
            if (
                request_id
                and entry.get("status") == "sending"
                and now - float(entry.get("updated_at", 0)) < 30
            ):
                raise IdempotencyConflict(
                    "the same media request is already being sent"
                )
            if request_id and entry.get("status") in {"sending", "uncertain"}:
                expected = expected_fingerprint(entry)
                if expected is None:
                    raise SendUncertainError(
                        "saved media send has no valid confirmation fingerprint; "
                        "refusing to resend"
                    )
                confirmation = self._wait_for_media_confirmation(
                    int(entry.get("baseline_local_id", 0)),
                    media_type,
                    expected,
                    timeout=self.confirm_poll_seconds,
                )
                if confirmation is not None:
                    sent_at = time.time()
                    entry.update(
                        {
                            "status": "sent",
                            "updated_at": sent_at,
                            "sent_at": sent_at,
                            "confirmed_local_id": int(confirmation["local_id"]),
                        }
                    )
                    entry.pop("error", None)
                    requests[request_id] = entry
                    self._save_state()
                    return {
                        "ok": True,
                        "status": "sent",
                        "deduplicated": True,
                        "request_id": request_id,
                        "sent_at": sent_at,
                        "confirmed_local_id": int(confirmation["local_id"]),
                        "media_type": media_type,
                    }
                raise SendUncertainError(
                    "previous media send remains uncertain; refusing to resend "
                    "automatically"
                )

            if self.reader is None:
                raise RuntimeError("database reader is required to confirm media delivery")
            baseline_local_id = self.reader.latest_local_id()
            entry = {
                "status": "sending",
                "content_hash": content_hash,
                "media_type": media_type,
                "baseline_local_id": baseline_local_id,
                "attempted_at": now,
                "updated_at": now,
                "room_id": str(room_id or ""),
                "source_local_id": int(source_local_id or 0),
                "task_id": str(task_id or ""),
                "generation": int(generation or 0),
            }
            if request_id:
                requests[request_id] = entry
                self._save_state()
            action = {
                "image": self._send_image_once,
                "video": self._send_video_once,
                "file": self._send_file_once,
            }[media_type]
            attempt_info = {}

            def remember_media_attempt(media_info):
                attempt_info.update(media_info)
                fingerprint = expected_fingerprint(media_info)
                if fingerprint is None:
                    return
                entry.update(fingerprint)
                if request_id:
                    requests[request_id] = entry
                    self._save_state()

            send_error = None
            action_result = None
            self._media_attempt_callback = remember_media_attempt
            self._set_send_context(
                room_id=room_id,
                source_local_id=source_local_id,
                task_id=task_id,
                generation=generation,
                item_kind=media_type,
            )
            try:
                action_result = action(payload)
            except Exception as exc:
                send_error = exc
            finally:
                self._media_attempt_callback = None
                self._clear_send_context()

            if isinstance(send_error, OutboundSuppressedError):
                cleanup_artifact(attempt_info.get("artifact_path"))
                now = time.time()
                entry.update(
                    {
                        "status": "suppressed",
                        "updated_at": now,
                        "barrier_id": send_error.barrier.get("id"),
                    }
                )
                if request_id:
                    requests[request_id] = entry
                    self._save_state()
                raise send_error

            if isinstance(action_result, dict):
                remember_media_attempt(action_result)
            elif action_result:
                attempt_info["artifact_path"] = str(action_result)

            expected = expected_fingerprint(attempt_info or entry)
            media_artifact = attempt_info.get("artifact_path")
            if expected is None:
                cleanup_artifact(media_artifact)
                now = time.time()
                if isinstance(send_error, MediaNotSentError):
                    error = str(send_error)
                    if request_id:
                        entry.update(
                            {
                                "status": "failed",
                                "updated_at": now,
                                "error": error[:300],
                            }
                        )
                        entry.pop("uncertain_since", None)
                        entry.pop("content_md5", None)
                        entry.pop("content_length", None)
                        requests[request_id] = entry
                        self._save_state()
                    raise send_error
                error = (
                    "media send fingerprint was not recorded; refusing to retry "
                    "automatically"
                )
                if request_id:
                    entry.update(
                        {
                            "status": "uncertain",
                            "updated_at": now,
                            "uncertain_since": now,
                            "error": error,
                        }
                    )
                    requests[request_id] = entry
                    self._save_state()
                raise SendUncertainError(error) from send_error
            confirmation = self._wait_for_media_confirmation(
                baseline_local_id,
                media_type,
                expected,
                timeout=(
                    min(1.0, self.media_confirm_timeout)
                    if send_error is not None
                    else None
                ),
            )
            if confirmation is None:
                cleanup_artifact(media_artifact)
                now = time.time()
                error = (
                    "media UI send failed and delivery could not be confirmed: %s"
                    % send_error
                    if send_error is not None
                    else "media delivery was not confirmed in the target group"
                )
                if request_id:
                    entry.update(
                        {
                            "status": "uncertain",
                            "updated_at": now,
                            "uncertain_since": now,
                            "error": str(error)[:300],
                        }
                    )
                    requests[request_id] = entry
                    self._save_state()
                raise SendUncertainError(error) from send_error

            cleanup_artifact(media_artifact)
            sent_at = time.time()
            confirmed_local_id = int(confirmation["local_id"])
            if request_id:
                entry.update(
                    {
                        "status": "sent",
                        "updated_at": sent_at,
                        "sent_at": sent_at,
                        "confirmed_local_id": confirmed_local_id,
                    }
                )
                entry.pop("error", None)
                requests[request_id] = entry
                self._save_state()
            return {
                "ok": True,
                "status": "sent",
                "deduplicated": False,
                "request_id": request_id,
                "sent_at": sent_at,
                "confirmed_local_id": confirmed_local_id,
                "media_type": media_type,
            }


class ChatApiApplication:
    def __init__(self, config):
        self.config = config
        self.started_at = time.time()
        self.ready = False
        self.degraded_reason = ""
        self.last_self_check_at = 0.0
        self.last_self_check_success_at = 0.0
        self._last_component_health = {}
        self._health_lock = threading.RLock()
        self.reader = SnapshotReader(config)
        self.hub = EventHub()
        control_path = config.get("outbound_control_db")
        if not control_path:
            control_path = (
                Path(config.get("cache_dir", "~/.cache/wechat-chat-api"))
                .expanduser()
                / "outbound-control.db"
            )
        self.control = OutboundControlStore(control_path)
        self.sender = TextSender(
            config,
            reader=self.reader,
            control_store=self.control,
        )
        self.monitor = MessageMonitor(
            self.reader,
            self.hub,
            config.get("poll_seconds", 0.15),
        )
        self.readiness_monitor = ReadinessMonitor(
            self,
            config.get("health_check_seconds", 10.0),
        )

    def run_self_check(self, force_snapshot=False):
        with self._health_lock:
            self.last_self_check_at = time.time()
            try:
                if force_snapshot:
                    self.reader.refresh(force=True)
                reader_health = self.reader.health()
                control_health = self.control.health()
                sender_health = self.sender.health()
                window_health = self.sender.window_health()
                checks = (
                    reader_health,
                    control_health,
                    sender_health,
                    window_health,
                )
                self._last_component_health = {
                    "reader": reader_health,
                    "outbound_control": control_health,
                    "send_state": sender_health,
                    "window": window_health,
                }
                if not all(bool(check.get("ok")) for check in checks):
                    failed = next(
                        (
                            str(check.get("error_type") or "self_check_failed")
                            for check in checks
                            if not bool(check.get("ok"))
                        ),
                        "self_check_failed",
                    )
                    raise RuntimeError(failed)
                self.ready = True
                self.degraded_reason = ""
                self.last_self_check_success_at = time.time()
                return True
            except Exception as exc:
                self.ready = False
                self.degraded_reason = (
                    str(exc)
                    if str(exc) and len(str(exc)) <= 80
                    else type(exc).__name__
                )
                LOG.exception(
                    "Chat API self-check failed error_type=%s",
                    type(exc).__name__,
                )
                return False

    def is_ready_for_send(self):
        return bool(self.ready and not self.degraded_reason)

    def health(self):
        with self._health_lock:
            monitor_health = self.monitor.health()
            failure_limit = max(
                1, int(self.config.get("health_monitor_failure_limit", 3))
            )
            monitor_failed = (
                monitor_health["alive"]
                and monitor_health["consecutive_failures"] >= failure_limit
            )
            monitor_dead = self.monitor.ident is not None and not monitor_health["alive"]
            degraded_reason = self.degraded_reason
            if monitor_failed:
                degraded_reason = (
                    monitor_health["last_error_type"] or "monitor_failures"
                )
            elif monitor_dead:
                degraded_reason = "monitor_stopped"
            components = dict(self._last_component_health)
            for name, operation in (
                ("reader", self.reader.health),
                ("outbound_control", self.control.health),
                ("send_state", self.sender.health),
            ):
                try:
                    components[name] = operation()
                except Exception as exc:
                    components[name] = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                    }
            failed_component = next(
                (
                    str(value.get("error_type") or f"{name}_unhealthy")
                    for name, value in components.items()
                    if not bool(value.get("ok"))
                ),
                "",
            )
            if failed_component:
                degraded_reason = failed_component
            degraded = bool(degraded_reason or not self.ready)
            reader_health = components.get("reader", {})
            control_health = components.get("outbound_control", {})
            sender_health = components.get("send_state", {})
            reader_details = {
                key: value
                for key, value in reader_health.items()
                if key != "ok"
            }
            return {
                "ok": bool(self.ready and not degraded),
                "live": True,
                "ready": bool(self.ready and not degraded),
                "degraded": degraded,
                "status": (
                    "degraded"
                    if degraded
                    else ("ready" if self.ready else "starting")
                ),
                "degraded_reason": degraded_reason,
                "started_at": self.started_at,
                "last_self_check_at": self.last_self_check_at,
                "last_self_check_success_at": self.last_self_check_success_at,
                **reader_details,
                "monitor": monitor_health,
                "send_state": sender_health,
                "outbound_control": control_health,
                "window": components.get("window", {}),
            }

    def metrics(self):
        health = self.health()
        monitor = health["monitor"]
        control = health["outbound_control"]
        send_state = health["send_state"]
        lines = [
            "# TYPE wechat_chat_api_live gauge",
            "wechat_chat_api_live 1",
            "# TYPE wechat_chat_api_ready gauge",
            "wechat_chat_api_ready %d" % int(health["ready"]),
            "# TYPE wechat_chat_api_degraded gauge",
            "wechat_chat_api_degraded %d" % int(health["degraded"]),
            "# TYPE wechat_chat_api_latest_local_id gauge",
            "wechat_chat_api_latest_local_id %d"
            % int(health.get("latest_local_id", 0)),
            "# TYPE wechat_chat_api_monitor_consecutive_failures gauge",
            "wechat_chat_api_monitor_consecutive_failures %d"
            % int(monitor["consecutive_failures"]),
            "# TYPE wechat_chat_api_snapshot_refresh_duration_ms gauge",
            "wechat_chat_api_snapshot_refresh_duration_ms %.1f"
            % float(health.get("last_refresh_duration_ms", 0)),
            "# TYPE wechat_chat_api_outbound_barriers gauge",
            "wechat_chat_api_outbound_barriers %d"
            % int(control.get("barrier_count", 0)),
            "# TYPE wechat_chat_api_send_state_requests gauge",
            'wechat_chat_api_send_state_requests{kind="text"} %d'
            % int(send_state.get("text_requests", 0)),
            'wechat_chat_api_send_state_requests{kind="media"} %d'
            % int(send_state.get("media_requests", 0)),
            "# TYPE wechat_chat_api_barrier_commit_p95_ms gauge",
            "wechat_chat_api_barrier_commit_p95_ms %.1f"
            % float(send_state.get("barrier_commit_p95_ms", 0.0)),
            "# TYPE wechat_chat_api_barrier_commit_total counter",
            "wechat_chat_api_barrier_commit_total %d"
            % int(send_state.get("barrier_commit_count", 0)),
        ]
        return "\n".join(lines) + "\n"

    def group(self):
        return {
            "id": self.reader.group_id,
            "name": self.reader.group_name,
            "latest_local_id": self.reader.latest_local_id(),
        }


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeChatChatAPI/1.0"

        def log_message(self, format_string, *args):
            if (
                len(args) >= 2
                and str(args[0]).startswith("GET /groups/")
                and "/messages?" in str(args[0])
                and str(args[1]) == "200"
            ):
                LOG.debug("%s - %s", self.client_address[0], format_string % args)
                return
            LOG.info("%s - %s", self.client_address[0], format_string % args)

        def _json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _text(self, status, payload, content_type="text/plain; charset=utf-8"):
            data = str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()

        def _error(self, status, message):
            self._json(status, {"ok": False, "error": str(message)})

        def _discard_request_body(self):
            if self.command != "POST" or getattr(
                self,
                "_request_body_consumed",
                False,
            ):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self.close_connection = True
                return
            if length <= 0:
                self._request_body_consumed = True
                return
            max_body = int(
                application.config.get("max_request_body_bytes", 32 * 1024 * 1024)
            )
            if length > max_body:
                self.close_connection = True
                return
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                self.close_connection = True
            finally:
                self._request_body_consumed = True

        def _require_outbound_auth(self):
            expected = configured_outbound_token(application.config)
            if not expected:
                self._discard_request_body()
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "status": "authentication_unavailable",
                        "error_type": "authentication_unavailable",
                        "error": "outbound authentication is not configured",
                    },
                )
                return False
            authorization = str(self.headers.get("Authorization") or "")
            scheme, separator, supplied = authorization.partition(" ")
            candidate = supplied.strip() if separator and scheme.lower() == "bearer" else ""
            if not hmac.compare_digest(
                candidate.encode("utf-8"),
                expected.encode("utf-8"),
            ):
                self._discard_request_body()
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {
                        "ok": False,
                        "status": "unauthorized",
                        "error_type": "authentication_failed",
                        "error": "outbound authentication failed",
                    },
                )
                return False
            return True

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            max_body = int(
                application.config.get("max_request_body_bytes", 32 * 1024 * 1024)
            )
            if length <= 0 or length > max_body:
                raise ValueError("request body length is invalid")
            body = self.rfile.read(length)
            self._request_body_consumed = True
            if len(body) != length:
                raise ValueError("request body ended before Content-Length")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _route_group(self, path):
            prefix = "/groups/"
            if not path.startswith(prefix):
                return None, None
            remainder = path[len(prefix) :]
            if "/" in remainder:
                encoded_group, action = remainder.split("/", 1)
            else:
                encoded_group, action = remainder, ""
            return urllib.parse.unquote(encoded_group), action

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/health":
                    if hasattr(application, "health"):
                        health = application.health()
                    else:
                        health = dict(application.reader.health())
                        health.update(
                            {
                                "live": True,
                                "ready": bool(health.get("ok")),
                                "degraded": not bool(health.get("ok")),
                                "status": (
                                    "ready" if health.get("ok") else "degraded"
                                ),
                            }
                        )
                        if getattr(application, "control", None) is not None:
                            health["outbound_control"] = (
                                application.control.health()
                            )
                    self._json(HTTPStatus.OK, health)
                    return
                if path == "/metrics" and hasattr(application, "metrics"):
                    self._text(HTTPStatus.OK, application.metrics())
                    return
                if not self._require_outbound_auth():
                    return
                if path == "/control/check":
                    params = urllib.parse.parse_qs(parsed.query)
                    room_id = params.get("room_id", [""])[0]
                    if room_id != application.reader.group_id:
                        self._error(HTTPStatus.NOT_FOUND, "room was not found")
                        return
                    result = application.sender.check_barrier(
                        room_id,
                        params.get("source_local_id", ["0"])[0],
                        params.get("item_kind", ["text"])[0],
                        task_id=params.get("task_id", [""])[0],
                        generation=params.get("generation", ["0"])[0],
                    )
                    self._json(HTTPStatus.OK, {"ok": True, **result})
                    return
                if path == "/delivery/status":
                    params = urllib.parse.parse_qs(parsed.query)
                    room_id = params.get("room_id", [""])[0]
                    if room_id != application.reader.group_id:
                        self._error(HTTPStatus.NOT_FOUND, "room was not found")
                        return
                    result = application.sender.delivery_status(
                        params.get("request_id", [""])[0],
                        params.get("item_kind", [""])[0],
                        room_id=room_id,
                        source_local_id=params.get("source_local_id", ["0"])[0],
                        task_id=params.get("task_id", [""])[0],
                        generation=params.get("generation", ["0"])[0],
                    )
                    self._json(HTTPStatus.OK, result)
                    return
                if path == "/groups":
                    self._json(HTTPStatus.OK, {"groups": [application.group()]})
                    return
                if path == "/stream":
                    self._stream(parsed)
                    return
                group_id, action = self._route_group(path)
                if group_id == application.reader.group_id and action == "messages":
                    params = urllib.parse.parse_qs(parsed.query)
                    limit = int(params.get("limit", ["200"])[0])
                    if "before" in params:
                        before = int(params["before"][0])
                        messages = application.reader.messages_before(before, limit)
                        cursor = {
                            "before": before,
                            "oldest_local_id": (
                                messages[0]["local_id"] if messages else before
                            ),
                        }
                    else:
                        after = int(params.get("after", ["0"])[0])
                        messages = application.reader.messages_after(after, limit)
                        cursor = {
                            "after": after,
                            "next_after": (
                                messages[-1]["local_id"] if messages else after
                            ),
                        }
                    self._json(
                        HTTPStatus.OK,
                        {
                            "group": application.group(),
                            "messages": messages,
                            **cursor,
                        },
                    )
                    return
                self._error(HTTPStatus.NOT_FOUND, "route was not found")
            except (ValueError, TypeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc)
            except Exception as exc:
                LOG.exception("GET %s failed", self.path)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, exc)

        def _stream(self, parsed):
            params = urllib.parse.parse_qs(parsed.query)
            if "after" in params:
                after = int(params["after"][0])
            elif self.headers.get("Last-Event-ID"):
                after = int(self.headers["Last-Event-ID"])
            else:
                after = application.reader.latest_local_id()
            client = application.hub.subscribe()
            last_sent = after
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    backlog = application.reader.messages_after(last_sent, 500)
                    for message in backlog:
                        self._write_event(message)
                        last_sent = message["local_id"]
                    if len(backlog) < 500:
                        break
                while True:
                    try:
                        message = client.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    if message is application.hub.CLOSED:
                        break
                    if message["local_id"] <= last_sent:
                        continue
                    self._write_event(message)
                    last_sent = message["local_id"]
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                application.hub.unsubscribe(client)

        def _write_event(self, message):
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            wire = "id: %s\nevent: message\ndata: %s\n\n" % (
                message["local_id"],
                payload,
            )
            self.wfile.write(wire.encode("utf-8"))
            self.wfile.flush()

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/")
            try:
                if path == "/control/barriers":
                    if not self._require_outbound_auth():
                        return
                    payload = self._read_json()
                    envelope = normalize_outbound_envelope(payload)
                    room_id = str(payload.get("room_id") or "").strip()
                    if room_id != application.reader.group_id:
                        self._error(HTTPStatus.NOT_FOUND, "room was not found")
                        return
                    barrier = application.sender.commit_barrier(
                        room_id,
                        envelope["source_local_id"],
                        payload.get("mode"),
                        task_id=envelope["task_id"],
                        generation=envelope["generation"],
                        reason=payload.get("reason", ""),
                    )
                    self._json(
                        HTTPStatus.CREATED,
                        {
                            "ok": True,
                            "status": "committed",
                            "request_id": envelope["request_id"],
                            "barrier": barrier,
                        },
                    )
                    return
                group_id, action = self._route_group(path)
                if (
                    group_id != application.reader.group_id
                    or action not in {"messages", "media"}
                ):
                    self._discard_request_body()
                    self._error(HTTPStatus.NOT_FOUND, "route was not found")
                    return
                if not self._require_outbound_auth():
                    return
                if (
                    hasattr(application, "is_ready_for_send")
                    and not application.is_ready_for_send()
                ):
                    self._discard_request_body()
                    self._error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Chat API is not ready for outbound delivery",
                    )
                    return
                payload = self._read_json()
                envelope = normalize_outbound_envelope(payload)
                if action == "media":
                    media_type = payload.get("type")
                    media_payload = (
                        payload.get("data")
                        if str(media_type).lower() == "image"
                        else payload.get("url")
                    )
                    result = application.sender.send_media(
                        media_type,
                        media_payload,
                        request_id=envelope["request_id"],
                        room_id=group_id,
                        source_local_id=envelope["source_local_id"],
                        task_id=envelope["task_id"],
                        generation=envelope["generation"],
                    )
                else:
                    result = application.sender.send(
                        payload.get("text"),
                        request_id=envelope["request_id"],
                        room_id=group_id,
                        source_local_id=envelope["source_local_id"],
                        task_id=envelope["task_id"],
                        generation=envelope["generation"],
                    )
                result["group_id"] = application.reader.group_id
                self._json(HTTPStatus.OK, result)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, exc)
            except IdempotencyConflict as exc:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "ok": False,
                        "status": "idempotency_conflict",
                        "error_type": "idempotency_conflict",
                        "retryable": False,
                        "error": str(exc),
                    },
                )
            except SendUncertainError as exc:
                self._json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "status": "uncertain",
                        "error_type": "send_uncertain",
                        "retryable": False,
                        "error": str(exc),
                    },
                )
            except OutboundSuppressedError as exc:
                self._json(
                    HTTPStatus.LOCKED,
                    {
                        "ok": False,
                        "status": "suppressed",
                        "error": str(exc),
                        "barrier": exc.barrier,
                    },
                )
            except RuntimeError as exc:
                LOG.exception("POST %s failed", self.path)
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, exc)
            except Exception as exc:
                LOG.exception("POST %s failed", self.path)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, exc)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Local structured WeChat group API")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.json")),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    faulthandler.enable(all_threads=True)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    application = ChatApiApplication(config)
    application.run_self_check(force_snapshot=True)
    application.monitor.start()
    application.readiness_monitor.start()
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8765))
    server = ThreadingHTTPServer((host, port), make_handler(application))
    LOG.info(
        "serving group %s on http://%s:%d",
        application.reader.group_id,
        host,
        port,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.control.close()


if __name__ == "__main__":
    main()
