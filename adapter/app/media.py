from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import json
import os
import re
import stat
import struct
import time
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


SUPPORTED_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

TEXT_MIME = {"text/plain", "text/markdown", "text/csv"}
OOXML_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
        "main": "word/document.xml",
        "root": "document",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        ),
    },
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
        "main": "xl/workbook.xml",
        "root": "workbook",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet.main+xml"
        ),
    },
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {
        "main": "ppt/presentation.xml",
        "root": "presentation",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml"
        ),
    },
}

_CHUNK_SIZE = 1024 * 1024
_MAX_JSON_VALIDATION_BYTES = 64 * 1024 * 1024
_MAX_MP4_BOXES = 100_000
_MAX_FTYP_BYTES = 4096
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_DECLARED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_REQUIRED_XML_BYTES = 4 * 1024 * 1024
_FORMAT_CHARS = "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\ufeff"
_LEGACY_MEDIA_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"m[" + _FORMAT_CHARS + r"]*"
    r"e[" + _FORMAT_CHARS + r"]*"
    r"d[" + _FORMAT_CHARS + r"]*"
    r"i[" + _FORMAT_CHARS + r"]*"
    r"a[" + _FORMAT_CHARS + r"]*\s*:",
    re.IGNORECASE,
)
_TASK_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _valid_mp4(path: Path) -> bool:
    file_size = path.stat().st_size
    if file_size < 32:
        return False

    seen: set[bytes] = set()
    offset = 0
    box_count = 0
    try:
        with path.open("rb") as handle:
            while offset < file_size:
                box_count += 1
                if box_count > _MAX_MP4_BOXES or file_size - offset < 8:
                    return False
                handle.seek(offset)
                header = handle.read(8)
                box_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset

                if box_size < header_size or box_size > file_size - offset:
                    return False
                payload_size = box_size - header_size
                if offset == 0:
                    if (
                        box_type != b"ftyp"
                        or payload_size < 8
                        or payload_size > _MAX_FTYP_BYTES
                    ):
                        return False
                    payload = handle.read(payload_size)
                    if len(payload) != payload_size:
                        return False
                    major_brand = payload[:4]
                    if not all(0x20 <= value <= 0x7E for value in major_brand):
                        return False
                    if (payload_size - 8) % 4:
                        return False
                if box_type == b"moov" and payload_size == 0:
                    return False
                if box_type == b"mdat" and payload_size == 0:
                    return False
                seen.add(box_type)
                offset += box_size
    except (OSError, struct.error):
        return False
    return offset == file_size and {b"ftyp", b"moov", b"mdat"} <= seen


def _valid_utf8_text(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                if b"\x00" in chunk:
                    return False
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _valid_json(path: Path) -> bool:
    try:
        if path.stat().st_size > _MAX_JSON_VALIDATION_BYTES:
            return False
    except OSError:
        return False
    if not _valid_utf8_text(path):
        return False
    try:
        with path.open("r", encoding="utf-8-sig", errors="strict") as handle:
            json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _valid_pdf(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 12:
            return False
        with path.open("rb") as handle:
            head = handle.read(16)
            handle.seek(max(0, size - 2048))
            tail = handle.read(2048)
    except OSError:
        return False
    return bool(
        re.match(br"^%PDF-[12]\.[0-9](?:\r|\n|\s)", head)
        and re.search(br"%%EOF\s*$", tail)
    )


def _safe_zip_infos(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo] | None:
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        return None
    result: dict[str, zipfile.ZipInfo] = {}
    declared_size = 0
    for info in infos:
        name = info.filename
        parts = PurePosixPath(name).parts
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in parts)
            or name in result
            or info.flag_bits & 0x1
        ):
            return None
        declared_size += int(info.file_size)
        if declared_size > _MAX_ZIP_DECLARED_BYTES:
            return None
        result[name] = info
    return result


def _xml_root_name(data: bytes) -> tuple[str, ElementTree.Element] | None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    return root.tag.rsplit("}", 1)[-1], root


def _read_required_xml(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> tuple[str, ElementTree.Element] | None:
    if info.file_size <= 0 or info.file_size > _MAX_REQUIRED_XML_BYTES:
        return None
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        return None
    if len(data) != info.file_size or b"\x00" in data:
        return None
    return _xml_root_name(data)


def _valid_ooxml(path: Path, expected: str) -> bool:
    contract = OOXML_MIME[expected]
    required = {
        "[Content_Types].xml": "Types",
        "_rels/.rels": "Relationships",
        contract["main"]: contract["root"],
    }
    try:
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            infos = _safe_zip_infos(archive)
            if infos is None or not required.keys() <= infos.keys():
                return False
            parsed: dict[str, ElementTree.Element] = {}
            for name, expected_root in required.items():
                result = _read_required_xml(archive, infos[name])
                if result is None or result[0] != expected_root:
                    return False
                parsed[name] = result[1]
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False

    main_part = "/" + str(contract["main"])
    for element in parsed["[Content_Types].xml"].iter():
        if (
            element.tag.rsplit("}", 1)[-1] == "Override"
            and element.attrib.get("PartName") == main_part
            and element.attrib.get("ContentType") == contract["content_type"]
        ):
            return True
    return False


def _valid_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            return _safe_zip_infos(archive) is not None
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def actual_mime(path: Path, expected: str = "") -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return "application/octet-stream"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if expected == "video/mp4" and _valid_mp4(path):
        return "video/mp4"
    if expected == "application/pdf" and _valid_pdf(path):
        return "application/pdf"
    if expected in OOXML_MIME and _valid_ooxml(path, expected):
        return expected
    if expected == "application/zip" and _valid_zip(path):
        return "application/zip"
    if expected == "application/json" and _valid_json(path):
        return "application/json"
    if expected in TEXT_MIME and _valid_utf8_text(path):
        return expected
    return "application/octet-stream"


@dataclass(frozen=True)
class MediaArtifact:
    path: Path
    name: str
    mime_type: str
    size_bytes: int
    sha256: str

    @property
    def media_type(self) -> str:
        if self.mime_type.startswith("image/"):
            return "image"
        if self.mime_type.startswith("video/"):
            return "video"
        return "file"


def _markdown_only(value: str) -> bool:
    return not value.strip(" \t>`*_~#[](){}+-")


def strip_legacy_delivery_markers(output: str) -> tuple[str, bool]:
    kept: list[str] = []
    removed = False
    for original_line in str(output or "").splitlines():
        normalized = unicodedata.normalize("NFKC", original_line)
        match = _LEGACY_MEDIA_RE.search(normalized)
        if match is None:
            kept.append(original_line)
            continue
        removed = True
        prefix = normalized[: match.start()].rstrip()
        prefix = prefix.rstrip("`*_~ ")
        if prefix and not _markdown_only(prefix):
            kept.append(prefix)
    return "\n".join(kept).strip(), removed


def _is_link_component(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _assert_no_link_components(task_root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(task_root)
    except ValueError as exc:
        raise ValueError(
            "media path is outside the task artifact directory"
        ) from exc
    current = task_root
    if _is_link_component(current):
        raise ValueError("task artifact directory must not be a symbolic link")
    for part in relative.parts:
        current = current / part
        if _is_link_component(current):
            raise ValueError(
                "artifact path components must not be symbolic links"
            )


def validate_media_path(
    raw_path: str,
    artifact_root: Path,
    task_id: str,
    max_bytes: int,
    max_image_bytes: int | None = None,
) -> MediaArtifact:
    task_value = str(task_id or "")
    if (
        not _TASK_COMPONENT_RE.fullmatch(task_value)
        or task_value in {".", ".."}
    ):
        raise ValueError("invalid task artifact directory")

    try:
        root = artifact_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("artifact root does not exist") from exc
    task_candidate = root / task_value
    if _is_link_component(task_candidate):
        raise ValueError("task artifact directory must not be a symbolic link")
    try:
        task_root = task_candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("task artifact directory does not exist") from exc
    if not task_root.is_dir() or not is_within(task_root, root):
        raise ValueError("invalid task artifact directory")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise ValueError("artifact path must be absolute")
    normalized = _normalized_absolute(candidate)
    _assert_no_link_components(task_root, normalized)
    try:
        path = normalized.resolve(strict=True)
    except OSError as exc:
        raise ValueError("media artifact does not exist") from exc
    _assert_no_link_components(task_root, normalized)
    if not is_within(path, task_root):
        raise ValueError("media path is outside the task artifact directory")

    try:
        before = path.stat()
    except OSError as exc:
        raise ValueError("media artifact is not readable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("media artifact must be a regular file")
    size = int(before.st_size)
    if size <= 0 or size > int(max_bytes):
        raise ValueError("media artifact size is outside the allowed range")

    expected = SUPPORTED_MIME.get(path.suffix.lower())
    if not expected:
        raise ValueError("media extension is not allowed")
    detected = actual_mime(path, expected)
    if detected != expected:
        raise ValueError("media extension does not match the file MIME type")
    if detected.startswith("image/") and max_image_bytes is not None:
        if size > int(max_image_bytes):
            raise ValueError("image artifact exceeds the transport size limit")

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise ValueError("media artifact is not readable") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise ValueError("media artifact changed during validation")
    _assert_no_link_components(task_root, normalized)

    relative = path.relative_to(task_root)
    name = PurePosixPath(*relative.parts).as_posix()
    if not name or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts):
        raise ValueError("invalid artifact relative path")
    return MediaArtifact(path, name, detected, size, digest.hexdigest())


def image_base64(artifact: MediaArtifact) -> str:
    return base64.b64encode(artifact.path.read_bytes()).decode("ascii")


def _normalized_artifact_name(name: str) -> str:
    value = str(name or "")
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("invalid artifact name")
    return path.as_posix()


def _binding_values(
    *,
    artifact_id: str,
    sha256: str,
    size_bytes: int | None,
    mime_type: str,
    version_token: str,
) -> dict[str, object] | None:
    supplied_metadata = any(
        (
            str(artifact_id or ""),
            str(sha256 or ""),
            size_bytes is not None,
            str(mime_type or ""),
        )
    )
    token = str(version_token or "").strip()
    if not supplied_metadata and not token:
        return None
    if supplied_metadata and not (
        str(artifact_id or "").strip()
        and _SHA256_RE.fullmatch(str(sha256 or "").strip())
        and size_bytes is not None
        and int(size_bytes) > 0
        and str(mime_type or "").strip()
    ):
        raise ValueError("complete artifact metadata is required for signing")
    if len(token) > 512:
        raise ValueError("artifact version token is too long")
    return {
        "artifact_id": str(artifact_id or "").strip(),
        "sha256": str(sha256 or "").strip().lower(),
        "size_bytes": int(size_bytes or 0),
        "mime_type": str(mime_type or "").strip().lower(),
        "version_token": token,
    }


class ArtifactSigner:
    def __init__(self, secret: str, public_base_url: str):
        self.secret = secret.encode("utf-8")
        self.public_base_url = public_base_url.rstrip("/")

    def _payload(
        self,
        task_id: str,
        name: str,
        expires: int,
        *,
        artifact_id: str = "",
        sha256: str = "",
        size_bytes: int | None = None,
        mime_type: str = "",
        version_token: str = "",
    ) -> bytes:
        binding = _binding_values(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            version_token=version_token,
        )
        if binding is None:
            return ("%s\n%s\n%d" % (task_id, name, int(expires))).encode(
                "utf-8"
            )
        payload = {
            "version": 2,
            "task_id": str(task_id),
            "name": _normalized_artifact_name(name),
            "expires": int(expires),
            **binding,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def signature(
        self,
        task_id: str,
        name: str,
        expires: int,
        *,
        artifact_id: str = "",
        sha256: str = "",
        size_bytes: int | None = None,
        mime_type: str = "",
        version_token: str = "",
    ) -> str:
        payload = self._payload(
            task_id,
            name,
            expires,
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            version_token=version_token,
        )
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def verify(
        self,
        task_id: str,
        name: str,
        expires: int,
        supplied: str,
        *,
        artifact_id: str = "",
        sha256: str = "",
        size_bytes: int | None = None,
        mime_type: str = "",
        version_token: str = "",
    ) -> bool:
        try:
            expired = int(expires) < int(time.time())
        except (TypeError, ValueError):
            return False
        if expired:
            return False
        try:
            expected = self.signature(
                task_id,
                name,
                expires,
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
                version_token=version_token,
            )
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(expected, str(supplied or ""))

    def url(
        self,
        task_id: str,
        name: str,
        expires: int,
        *,
        artifact_id: str = "",
        sha256: str = "",
        size_bytes: int | None = None,
        mime_type: str = "",
        version_token: str = "",
    ) -> str:
        binding = _binding_values(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            version_token=version_token,
        )
        artifact_name = (
            _normalized_artifact_name(name) if binding is not None else str(name)
        )
        token = self.signature(
            task_id,
            artifact_name,
            expires,
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            version_token=version_token,
        )
        query: dict[str, object] = {
            "expires": int(expires),
            "signature": token,
        }
        if binding is not None:
            query.update(binding)
        quoted_task = urllib.parse.quote(str(task_id), safe="")
        quoted_name = urllib.parse.quote(artifact_name, safe="")
        return (
            "%s/internal/artifacts/%s/%s?%s"
            % (
                self.public_base_url,
                quoted_task,
                quoted_name,
                urllib.parse.urlencode(query),
            )
        )

    def immutable_url(
        self,
        *,
        artifact_id: str,
        task_id: str,
        generation: int,
        name: str,
        sha256: str,
        size_bytes: int,
        mime_type: str,
        expires: int,
    ) -> str:
        version_token = "generation:%d" % int(generation)
        signature = self.signature(
            task_id,
            name,
            expires,
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            mime_type=mime_type,
            version_token=version_token,
        )
        query = urllib.parse.urlencode(
            {
                "expires": int(expires),
                "signature": signature,
                "version_token": version_token,
            }
        )
        return "%s/internal/artifacts/%s?%s" % (
            self.public_base_url,
            urllib.parse.quote(str(artifact_id), safe=""),
            query,
        )
