from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

from .media import MediaArtifact, SUPPORTED_MIME, validate_media_path


_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ProducedFile:
    artifact: MediaArtifact
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.artifact.path),
            "name": self.artifact.name,
            "mime_type": self.artifact.mime_type,
            "size_bytes": self.artifact.size_bytes,
            "sha256": self.artifact.sha256,
            "reused": self.reused,
        }


def _is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_no_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("tool path is outside the task directory") from exc
    current = root
    if _is_symlink(current):
        raise ValueError("task directory must not be a symbolic link")
    for part in relative.parts:
        current = current / part
        if _is_symlink(current):
            raise ValueError("tool path must not contain symbolic links")


def ensure_task_root(artifact_root: Path, task_id: str) -> Path:
    root = Path(artifact_root).resolve(strict=True)
    if not root.is_dir() or _is_symlink(root):
        raise ValueError("artifact root is not a regular directory")
    task_root = root / str(task_id)
    if task_root.exists() or _is_symlink(task_root):
        if _is_symlink(task_root) or not task_root.is_dir():
            raise ValueError("task artifact directory is unsafe")
    else:
        task_root.mkdir(mode=0o770)
    os.chmod(task_root, 0o770)
    resolved = task_root.resolve(strict=True)
    if not _within(resolved, root):
        raise ValueError("task artifact directory escaped the artifact root")
    return resolved


def safe_file_name(value: str, *, extensions: set[str] | None = None) -> str:
    name = unicodedata.normalize("NFKC", str(value or "").strip())
    if (
        not _FILE_NAME_RE.fullmatch(name)
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise ValueError(
            "file_name must use only ASCII letters, digits, dot, dash, and underscore"
        )
    allowed = extensions if extensions is not None else set(SUPPORTED_MIME)
    if Path(name).suffix.lower() not in allowed:
        raise ValueError("file_name extension is not allowed")
    return name


def _ensure_output_directory(task_root: Path, name: str) -> Path:
    directory = task_root / name
    if directory.exists() or _is_symlink(directory):
        if _is_symlink(directory) or not directory.is_dir():
            raise ValueError("output directory is unsafe")
    else:
        directory.mkdir(mode=0o770)
    os.chmod(directory, 0o770)
    resolved = directory.resolve(strict=True)
    _assert_no_symlink_components(task_root, resolved)
    return resolved


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _new_temporary(directory: Path, file_name: str) -> tuple[Path, BinaryIO]:
    for _ in range(20):
        token = os.urandom(8).hex()
        path = directory / (".%s.%s.tmp" % (file_name, token))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o660)
        except FileExistsError:
            continue
        return path, os.fdopen(descriptor, "wb")
    raise RuntimeError("could not allocate a temporary output file")


def _publish_temporary(temporary: Path, target: Path) -> bool:
    if target.exists() or _is_symlink(target):
        if _is_symlink(target) or not target.is_file():
            raise ValueError("output target is unsafe")
        temporary_size, temporary_hash = _sha256_file(temporary)
        target_size, target_hash = _sha256_file(target)
        if (temporary_size, temporary_hash) != (target_size, target_hash):
            raise FileExistsError("output file already exists with different content")
        temporary.unlink()
        return True
    try:
        os.link(temporary, target)
    except FileExistsError:
        return _publish_temporary(temporary, target)
    temporary.unlink()
    os.chmod(target, 0o660)
    return False


def _validate_output(
    target: Path,
    artifact_root: Path,
    task_id: str,
    max_artifact_bytes: int,
    max_image_bytes: int,
    *,
    reused: bool,
) -> ProducedFile:
    try:
        artifact = validate_media_path(
            str(target),
            artifact_root,
            task_id,
            max_artifact_bytes,
            max_image_bytes,
        )
    except Exception:
        if not reused:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        raise
    return ProducedFile(artifact=artifact, reused=reused)


def write_text_artifact(
    artifact_root: Path,
    task_id: str,
    file_name: str,
    content: str,
    *,
    max_text_bytes: int,
    max_artifact_bytes: int,
    max_image_bytes: int,
) -> ProducedFile:
    name = safe_file_name(file_name, extensions=_TEXT_EXTENSIONS)
    text = str(content or "")
    if not text or "\x00" in text:
        raise ValueError("text artifact content must be non-empty UTF-8 text")
    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) > int(max_text_bytes):
        raise ValueError("text artifact exceeds the configured size limit")
    if Path(name).suffix.lower() == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON artifact content is invalid") from exc

    task_root = ensure_task_root(artifact_root, task_id)
    directory = _ensure_output_directory(task_root, "generated")
    target = directory / name
    temporary, handle = _new_temporary(directory, name)
    try:
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        reused = _publish_temporary(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _validate_output(
        target,
        artifact_root,
        task_id,
        max_artifact_bytes,
        max_image_bytes,
        reused=reused,
    )


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        values = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError("download host could not be resolved") from exc
    addresses: list[str] = []
    for value in values:
        address = str(value[4][0]).split("%", 1)[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("download host resolved to an invalid address") from exc
        if not parsed.is_global:
            raise ValueError("download URL resolves to a non-public address")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError("download host did not resolve to an address")
    return tuple(addresses)


def validate_public_url(raw_url: str) -> tuple[str, urllib.parse.SplitResult, tuple[str, ...]]:
    value = str(raw_url or "").strip()
    if not value or len(value) > 4096:
        raise ValueError("download URL length is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("download URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("download URL credentials and fragments are not allowed")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("download URL host or port is invalid") from exc
    if port not in {80, 443}:
        raise ValueError("download URL port is not allowed")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".localhost", ".local", ".internal", ".home.arpa")
    ):
        raise ValueError("download URL host is not public")
    addresses = _public_addresses(host, port)
    netloc = host
    if ":" in host:
        netloc = "[" + host + "]"
    if parsed.port:
        netloc += ":" + str(port)
    normalized = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    return normalized, urllib.parse.urlsplit(normalized), addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw_socket,
            server_hostname=self.host,
        )


def _request_once(
    parsed: urllib.parse.SplitResult,
    addresses: tuple[str, ...],
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> tuple[int, dict[str, str]]:
    host = str(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    last_error: Exception | None = None
    for address in addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                host,
                address,
                port,
                timeout_seconds,
            )
        else:
            connection = _PinnedHTTPConnection(
                host,
                address,
                port,
                timeout_seconds,
            )
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "User-Agent": "wechat-hermes-fetch/1.0",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            headers = {
                key.lower(): value.strip()
                for key, value in response.getheaders()
            }
            status_code = int(response.status)
            if status_code in _REDIRECT_STATUSES:
                return status_code, headers
            if status_code != 200:
                raise RuntimeError(
                    "download server returned HTTP %d" % status_code
                )
            if headers.get("content-encoding", "").lower() not in {"", "identity"}:
                raise ValueError("download response used an unsupported content encoding")
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise ValueError("download response content length is invalid") from exc
                if declared < 0 or declared > int(max_bytes):
                    raise ValueError("download response exceeds the configured size limit")
            size = 0
            flags = os.O_WRONLY | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags)
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    chunk = response.read(min(_CHUNK_SIZE, int(max_bytes) - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > int(max_bytes):
                        raise ValueError("download response exceeds the configured size limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if content_length and size != declared:
                raise ValueError(
                    "download response length did not match Content-Length"
                )
            return status_code, headers
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
            continue
        finally:
            connection.close()
    raise RuntimeError("download connection failed") from last_error


def download_public_artifact(
    artifact_root: Path,
    task_id: str,
    url: str,
    file_name: str,
    *,
    max_download_bytes: int,
    max_artifact_bytes: int,
    max_image_bytes: int,
    timeout_seconds: float = 30,
    max_redirects: int = 5,
) -> ProducedFile:
    name = safe_file_name(file_name)
    task_root = ensure_task_root(artifact_root, task_id)
    directory = _ensure_output_directory(task_root, "downloads")
    target = directory / name
    temporary, handle = _new_temporary(directory, name)
    handle.close()
    current = str(url or "")
    try:
        for redirect_count in range(max(0, int(max_redirects)) + 1):
            normalized, parsed, addresses = validate_public_url(current)
            status_code, headers = _request_once(
                parsed,
                addresses,
                temporary,
                max_bytes=min(int(max_download_bytes), int(max_artifact_bytes)),
                timeout_seconds=max(1.0, float(timeout_seconds)),
            )
            if status_code not in _REDIRECT_STATUSES:
                break
            location = str(headers.get("location") or "").strip()
            if not location or redirect_count >= int(max_redirects):
                raise ValueError("download redirect limit was exceeded")
            current = urllib.parse.urljoin(normalized, location)
        else:
            raise ValueError("download redirect limit was exceeded")
        reused = _publish_temporary(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _validate_output(
        target,
        artifact_root,
        task_id,
        max_artifact_bytes,
        max_image_bytes,
        reused=reused,
    )


def _resolve_sources(task_root: Path, source_paths: Iterable[str]) -> list[Path]:
    selected: dict[str, Path] = {}
    for raw in source_paths:
        value = str(raw or "").strip()
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = task_root / candidate
        normalized = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
        if not _within(normalized, task_root):
            raise ValueError("archive source is outside the task directory")
        _assert_no_symlink_components(task_root, normalized)
        try:
            mode = normalized.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError("archive source does not exist") from exc
        if stat.S_ISLNK(mode):
            raise ValueError("archive sources must not be symbolic links")
        if stat.S_ISREG(mode):
            selected[str(normalized)] = normalized
            continue
        if not stat.S_ISDIR(mode):
            raise ValueError("archive sources must be regular files or directories")
        for base, directories, files in os.walk(normalized, followlinks=False):
            base_path = Path(base)
            for directory in list(directories):
                child = base_path / directory
                if _is_symlink(child):
                    raise ValueError("archive source contains a symbolic link")
            for file_name in files:
                child = base_path / file_name
                mode = child.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ValueError("archive source contains a symbolic link")
                if not stat.S_ISREG(mode):
                    raise ValueError("archive source contains a special file")
                selected[str(child)] = child
    if not selected:
        raise ValueError("at least one archive source file is required")
    return [selected[key] for key in sorted(selected)]


def _write_zip_entry(
    archive: zipfile.ZipFile,
    path: Path,
    archive_name: str,
) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("archive source changed during packaging")
        info = zipfile.ZipInfo(
            archive_name,
            date_time=(1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100640 << 16
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with archive.open(info, "w", force_zip64=True) as destination:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    destination.write(chunk)
        return int(metadata.st_size)
    finally:
        os.close(descriptor)


def create_zip_artifact(
    artifact_root: Path,
    task_id: str,
    archive_name: str,
    source_paths: Iterable[str],
    *,
    max_files: int,
    max_source_bytes: int,
    max_artifact_bytes: int,
    max_image_bytes: int,
) -> ProducedFile:
    name = safe_file_name(archive_name, extensions={".zip"})
    task_root = ensure_task_root(artifact_root, task_id)
    directory = _ensure_output_directory(task_root, "generated")
    target = directory / name
    sources = _resolve_sources(task_root, source_paths)
    if len(sources) > int(max_files):
        raise ValueError("archive source file count exceeds the configured limit")
    declared_size = sum(path.lstat().st_size for path in sources)
    if declared_size > int(max_source_bytes):
        raise ValueError("archive source size exceeds the configured limit")
    if target in sources:
        raise ValueError("archive cannot include its own output file")

    temporary, handle = _new_temporary(directory, name)
    handle.close()
    try:
        written_size = 0
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for source in sources:
                relative = source.relative_to(task_root)
                member_name = PurePosixPath(*relative.parts).as_posix()
                if any(part in {"", ".", ".."} for part in PurePosixPath(member_name).parts):
                    raise ValueError("archive member path is unsafe")
                written_size += _write_zip_entry(archive, source, member_name)
                if written_size > int(max_source_bytes):
                    raise ValueError("archive source changed beyond the size limit")
        if temporary.stat().st_size > int(max_artifact_bytes):
            raise ValueError("archive output exceeds the artifact size limit")
        reused = _publish_temporary(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _validate_output(
        target,
        artifact_root,
        task_id,
        max_artifact_bytes,
        max_image_bytes,
        reused=reused,
    )
