from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse

import yaml


REGISTRY_IDENTIFIER_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?"
    r"(?:@[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CAPABILITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HUB_HASH_RE = re.compile(r"^sha256:([0-9a-f]{16})$")
FULL_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
SAFE_SCAN_VERDICTS = {
    "clean",
    "pass",
    "passed",
    "safe",
    "trusted",
    "trusted-builtin",
}
CODE_SUFFIXES = {
    ".bash",
    ".bat",
    ".c",
    ".cjs",
    ".cmd",
    ".cpp",
    ".fish",
    ".go",
    ".js",
    ".jsx",
    ".lua",
    ".mjs",
    ".php",
    ".pl",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".ts",
    ".tsx",
    ".zsh",
}
BINARY_SUFFIXES = {
    ".a",
    ".class",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".node",
    ".o",
    ".pyc",
    ".so",
    ".wasm",
}
ARCHIVE_SUFFIXES = {
    ".7z",
    ".bz2",
    ".gz",
    ".rar",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
}
IMAGE_SIGNATURES = {
    ".gif": (b"GIF87a", b"GIF89a"),
    ".jpeg": (b"\xff\xd8\xff",),
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".webp": (b"RIFF",),
}
BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}
ZERO_WIDTH_CONTROLS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
}
PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"(?:ignore|override|disregard|forget|replace).{0,80}"
        r"(?:previous|prior|above|system|developer|policy|instruction|prompt)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:reveal|print|show|dump|exfiltrate|send|repeat).{0,80}"
        r"(?:system prompt|developer message|hidden instruction|conversation history)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:bypass|disable|evade|remove).{0,60}"
        r"(?:policy|safety|approval|guardrail|sandbox|restriction|filter)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:忽略|无视|覆盖|替换|忘记|跳过|绕过).{0,40}"
        r"(?:之前|以上|前面|系统|开发者|安全|规则|策略|指令|提示词)",
        re.DOTALL,
    ),
    re.compile(
        r"(?:泄露|显示|输出|打印|发送|复述).{0,40}"
        r"(?:系统提示|开发者消息|隐藏指令|内部提示|对话历史)",
        re.DOTALL,
    ),
    re.compile(
        r"(?:把|将).{0,30}(?:引用|附件|文件|下文|以下内容).{0,30}"
        r"(?:当作|视为|提升为).{0,20}(?:系统|开发者|最高优先级).{0,10}指令",
        re.DOTALL,
    ),
    re.compile(
        r"(?:^|[\s`>#*\[])(?:system|developer|assistant)\s*"
        r"(?:message|prompt|instruction)?\s*[:=]",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"(?:BEGIN|START).{0,20}(?:SYSTEM|DEVELOPER).{0,20}"
        r"(?:PROMPT|MESSAGE|INSTRUCTION)|<<\s*SYS\s*>>|\[INST\]",
        re.IGNORECASE | re.DOTALL,
    ),
)
DANGEROUS_TEXT_PATTERNS = (
    re.compile(r"(?:^|[\s`$>;&|])rm\s+(?:-[A-Za-z]*r[A-Za-z]*f|--recursive)", re.I),
    re.compile(r"\b(?:curl|wget)\b[^\n|]{0,400}\|\s*(?:ba|z|da|fi)?sh\b", re.I),
    re.compile(r"(?:^|[\s`$>;&|])(?:sudo|su\s+-|chroot)\s+", re.I),
    re.compile(r"\b(?:systemctl|crontab|update-rc\.d|launchctl|schtasks)\b", re.I),
    re.compile(r"(?:authorized_keys|/etc/shadow|/etc/passwd|/root/|\.ssh/)", re.I),
    re.compile(r"(?:\.bashrc|\.profile|\.zshrc|/etc/cron|/etc/systemd)", re.I),
    re.compile(r"\b(?:chmod\s+(?:[0-7]*[467][0-7]*|[ugoa]*\+[xs])|chown\s+root)\b", re.I),
    re.compile(r"(?:/var/run/docker\.sock|/run/docker\.sock|docker\s+exec)", re.I),
    re.compile(r"\b(?:nmap|masscan|zmap|arp-scan|netdiscover)\b", re.I),
    re.compile(r"(?:169\.254\.169\.254|metadata\.google\.internal|/latest/meta-data)", re.I),
    re.compile(r"(?:127\.0\.0\.1|localhost):8765\b", re.I),
    re.compile(r"\b(?:nc|netcat|socat)\b.{0,80}\b(?:-e|exec|listen)\b", re.I),
    re.compile(r"\b(?:mkfs(?:\.\w+)?|wipefs|fdisk|parted)\b", re.I),
    re.compile(r"\bdd\b.{0,120}\bof\s*=\s*/dev/(?:sd|vd|nvme|mapper)", re.I),
    re.compile(r"\b(?:shutdown|poweroff|halt|reboot)\b", re.I),
    re.compile(r"\b(?:iptables|ip6tables|nft)\b", re.I),
    re.compile(r"\b(?:useradd|adduser|usermod|groupadd|visudo)\b", re.I),
    re.compile(r"\b(?:apt|apt-get|yum|dnf|apk|pacman)\s+(?:install|upgrade)\b", re.I),
    re.compile(r"\b(?:pip|pip3|npm|pnpm|yarn|gem|cargo)\s+install\b", re.I),
    re.compile(r"\b(?:bash|sh|zsh|fish|python|python3|perl|ruby)\s+-c\b", re.I),
    re.compile(
        r"(?:https?://)?(?:10(?:\.\d{1,3}){3}|"
        r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])"
        r"(?:\.\d{1,3}){2})",
        re.I,
    ),
)
DANGEROUS_CODE_PATTERNS = (
    re.compile(r"\bshell\s*=\s*True\b"),
    re.compile(r"\bos\.(?:system|popen|spawn\w*)\s*\("),
    re.compile(r"\bsubprocess\.(?:Popen|run|call|check_call|check_output)\s*\("),
    re.compile(r"\b(?:eval|exec|compile)\s*\("),
    re.compile(r"\b(?:pickle|marshal)\.loads?\s*\("),
    re.compile(r"\b(?:requests|httpx|aiohttp|urllib\.request|socket|scapy|paramiko)\b"),
    re.compile(r"\b(?:child_process|node:child_process|Deno\.run|Bun\.spawn)\b"),
    re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket)\s*\("),
    re.compile(r"\bnew\s+Function\s*\("),
    re.compile(r"\b(?:winreg|ctypes)\b"),
)
BASE64_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?:[A-Za-z0-9+/_-]{4}){6,}"
    r"(?:[A-Za-z0-9+/_-]{2}==|[A-Za-z0-9+/_-]{3}=)?"
    r"(?![A-Za-z0-9+/=_-])"
)
HEX_CANDIDATE_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){16,}(?![0-9A-Fa-f])")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)\n]+)\)")


class SkillInstallError(RuntimeError):
    pass


def validate_skill_identifier(identifier: str) -> str:
    value = str(identifier or "").strip()
    if not value or len(value) > 500 or value.startswith("-"):
        raise ValueError("invalid Skill identifier")
    parsed = urlparse(value)
    if parsed.scheme:
        raise ValueError(
            "direct Skill URLs are disabled; use a fixed registry identifier"
        )
    if not REGISTRY_IDENTIFIER_RE.fullmatch(value):
        raise ValueError("invalid registry Skill identifier")
    return value


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillInstallError("Skill metadata lock is missing or invalid") from exc
    if not isinstance(value, dict):
        raise SkillInstallError("Skill metadata lock is invalid")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> tuple[dict[str, str], str]:
    """Return the legacy dynamic-lock digest kept for lock-file compatibility."""
    files: dict[str, str] = {}
    bundle = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SkillInstallError("Skill contains a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest = _sha256(path)
        files[relative] = digest
        bundle.update(relative.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(bytes.fromhex(digest))
        bundle.update(b"\0")
    return files, bundle.hexdigest()


def _hermes_inventory(root: Path) -> tuple[dict[str, str], str]:
    """Hash exact bundle bytes using the Hermes Hub provenance algorithm."""
    files: dict[str, str] = {}
    bundle = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SkillInstallError("Skill contains a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest = _sha256(path)
        files[relative] = digest
        bundle.update(relative.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(path.read_bytes())
    return files, bundle.hexdigest()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _normalized_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    return "".join(character for character in value if character not in ZERO_WIDTH_CONTROLS)


def _decoded_text(data: bytes, suffix: str) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise SkillInstallError("Skill contains malformed UTF-16 text") from exc
    if b"\0" in data:
        signatures = IMAGE_SIGNATURES.get(suffix)
        if signatures and any(data.startswith(signature) for signature in signatures):
            if suffix == ".webp" and data[8:12] != b"WEBP":
                raise SkillInstallError("Skill contains a malformed WebP asset")
            return "\n".join(
                match.decode("ascii", errors="ignore")
                for match in re.findall(rb"[\x20-\x7e]{8,}", data)
            )
        raise SkillInstallError("Skill contains a NUL or opaque binary payload")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        signatures = IMAGE_SIGNATURES.get(suffix)
        if signatures and any(data.startswith(signature) for signature in signatures):
            return "\n".join(
                match.decode("ascii", errors="ignore")
                for match in re.findall(rb"[\x20-\x7e]{8,}", data)
            )
        raise SkillInstallError("Skill contains non-UTF-8 opaque content") from exc


def _decoded_candidates(text: str) -> Iterator[str]:
    for match in BASE64_CANDIDATE_RE.finditer(text):
        token = match.group(0)
        if len(token) > 32768:
            continue
        padded = token + "=" * (-len(token) % 4)
        for altchars in (None, b"-_"):
            try:
                decoded = base64.b64decode(
                    padded.encode("ascii"),
                    altchars=altchars,
                    validate=True,
                )
                value = decoded.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if value:
                yield value
                break
    for match in HEX_CANDIDATE_RE.finditer(text):
        try:
            value = bytes.fromhex(match.group(0)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if value:
            yield value
    if "%" in text:
        decoded_url = unquote(text)
        if decoded_url != text:
            yield decoded_url
    if "\\u" in text or "\\x" in text:
        def replace_escape(match: re.Match[str]) -> str:
            return chr(int(match.group(1), 16))

        decoded_escapes = re.sub(r"\\u([0-9a-fA-F]{4})", replace_escape, text)
        decoded_escapes = re.sub(r"\\x([0-9a-fA-F]{2})", replace_escape, decoded_escapes)
        if decoded_escapes != text:
            yield decoded_escapes


def _audit_text(
    text: str,
    *,
    code: bool,
    encoded_depth: int = 0,
    seen: set[str] | None = None,
) -> None:
    if seen is None:
        seen = set()
    fingerprint = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    if fingerprint in seen:
        return
    seen.add(fingerprint)
    if any(character in text for character in BIDI_CONTROLS):
        raise SkillInstallError("Skill contains bidirectional control characters")
    normalized = _normalized_text(text)
    if any(pattern.search(normalized) for pattern in PROMPT_INJECTION_PATTERNS):
        qualifier = "encoded " if encoded_depth else ""
        raise SkillInstallError("Skill contains %sprompt-injection instructions" % qualifier)
    if any(pattern.search(normalized) for pattern in DANGEROUS_TEXT_PATTERNS):
        qualifier = "encoded " if encoded_depth else ""
        raise SkillInstallError("Skill contains a %sprohibited command or endpoint" % qualifier)
    if code and any(pattern.search(normalized) for pattern in DANGEROUS_CODE_PATTERNS):
        raise SkillInstallError(
            "Skill code contains shell, network, or dynamic execution"
        )
    if encoded_depth < 2:
        for decoded in _decoded_candidates(normalized):
            _audit_text(
                decoded,
                code=True,
                encoded_depth=encoded_depth + 1,
                seen=seen,
            )


class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise SkillInstallError(
                "Skill manifest contains an invalid metadata key"
            ) from exc
        if duplicate:
            raise SkillInstallError("Skill manifest contains duplicate metadata keys")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _metadata_mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillInstallError(f"Skill {label} metadata is invalid")
    return value


def _frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillInstallError("Skill manifest is not valid UTF-8") from exc
    if not text.startswith("---\n"):
        raise SkillInstallError("Skill manifest has no YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise SkillInstallError("Skill manifest has unterminated YAML frontmatter")
    try:
        values = yaml.load(text[4:end], Loader=_StrictSafeLoader) or {}
    except SkillInstallError:
        raise
    except yaml.YAMLError as exc:
        raise SkillInstallError("Skill manifest contains invalid YAML") from exc
    if not isinstance(values, dict):
        raise SkillInstallError("Skill manifest metadata must be a mapping")

    metadata = _metadata_mapping(values.get("metadata"), "root")
    hermes = _metadata_mapping(metadata.get("hermes"), "Hermes")
    name = str(values.get("name") or "").strip()
    description = str(values.get("description") or "").strip()
    version = values.get("version")
    if version is None:
        version = hermes.get("version")
    capabilities = (
        values.get("capabilities")
        or values.get("allowed-tools")
        or hermes.get("capabilities")
        or []
    )
    if isinstance(capabilities, str):
        capabilities = [item for item in re.split(r"[\s,]+", capabilities) if item]
    if not isinstance(capabilities, list):
        raise SkillInstallError("Skill capabilities metadata is invalid")
    safe_capabilities: list[str] = []
    for capability in capabilities:
        candidate = str(capability or "").strip()
        if not CAPABILITY_RE.fullmatch(candidate):
            raise SkillInstallError("Skill declares an unsafe capability name")
        safe_capabilities.append(candidate)
    return {
        "name": name,
        "description": description,
        "version": str(version or "unversioned"),
        "capabilities": sorted(set(safe_capabilities)),
    }


def _discover_skill_roots(skills_root: Path) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for manifest in sorted(skills_root.rglob("SKILL.md")):
        relative = manifest.relative_to(skills_root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        root = manifest.parent
        metadata = _frontmatter(manifest)
        name = metadata["name"]
        if not SKILL_NAME_RE.fullmatch(name):
            raise SkillInstallError("Skill manifest has an unsafe name")
        if name in discovered:
            raise SkillInstallError("Skill tree contains duplicate manifest names")
        discovered[name] = root
    return discovered


class SkillInstaller:
    def __init__(
        self,
        *,
        hermes_cli: Path,
        hermes_home: Path,
        skills_root: Path | None = None,
        hub_lock: Path | None = None,
        integrity_lock: Path | None = None,
        trust_root: Path | None = None,
        sandbox_executable: Path | None = None,
        command_timeout_seconds: int = 300,
    ):
        self.hermes_cli = Path(hermes_cli)
        self.hermes_home = Path(hermes_home)
        configured_trust = trust_root or os.getenv("HERMES_SKILL_TRUST_ROOT")
        self.trust_root = Path(configured_trust) if configured_trust else None
        if skills_root is not None:
            self.skills_root = Path(skills_root)
        elif self.trust_root is not None:
            self.skills_root = self.trust_root / "current" / "skills"
        else:
            self.skills_root = self.hermes_home / ".hermes" / "skills"
        self.hub_lock = Path(
            hub_lock or self.skills_root / ".hub" / "lock.json"
        )
        if integrity_lock is not None:
            self.integrity_lock = Path(integrity_lock)
        elif self.trust_root is not None:
            self.integrity_lock = (
                self.trust_root / "current" / "skills-lock.json"
            )
        else:
            self.integrity_lock = (
                self.hermes_home / ".hermes" / "skills-lock.json"
            )
        configured_sandbox = sandbox_executable or os.getenv("HERMES_SKILL_SANDBOX")
        self.sandbox_executable = (
            Path(configured_sandbox) if configured_sandbox else None
        )
        control_root = (
            self.trust_root / "staging"
            if self.trust_root is not None
            else self.hermes_home / ".hermes"
        )
        self.transaction_file = control_root / "skill-install-transaction.json"
        self.process_lock_file = control_root / "skill-install.lock"
        self.command_timeout_seconds = max(30, int(command_timeout_seconds))
        self._lock = threading.Lock()
        self._active_home: Path | None = None
        self._active_skills_root: Path | None = None

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self.process_lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.process_lock_file.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _run(self, *args: str, allow_network: bool = False) -> None:
        if not self.hermes_cli.is_file() or not os.access(self.hermes_cli, os.X_OK):
            raise SkillInstallError("Hermes Skill CLI is unavailable")
        active_home = self._active_home or self.hermes_home
        if not active_home.is_dir():
            raise SkillInstallError("Hermes Skill staging home is unavailable")
        environment = {
            "HOME": str(active_home),
            "HERMES_HOME": str(active_home / ".hermes"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        command = [str(self.hermes_cli), *args]
        cwd = str(active_home)
        if self.sandbox_executable is not None:
            if (
                not self.sandbox_executable.is_file()
                or not os.access(self.sandbox_executable, os.X_OK)
            ):
                raise SkillInstallError("Skill audit sandbox is unavailable")
            sandbox_home = "/tmp/skill-stage"
            command = [
                str(self.sandbox_executable),
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "--dir",
                sandbox_home,
                "--bind",
                str(active_home),
                sandbox_home,
                "--tmpfs",
                "/home",
                "--tmpfs",
                "/root",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/etc/wechat-hermes",
                "--tmpfs",
                "/var/lib/wechat-hermes",
                "--setenv",
                "HOME",
                sandbox_home,
                "--setenv",
                "HERMES_HOME",
                sandbox_home + "/.hermes",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--setenv",
                "LC_ALL",
                "C.UTF-8",
                "--setenv",
                "NO_COLOR",
                "1",
                "--setenv",
                "PATH",
                environment["PATH"],
                "--chdir",
                sandbox_home,
                "--",
                *command,
            ]
            if allow_network:
                command.insert(4, "--share-net")
            environment["HOME"] = sandbox_home
            environment["HERMES_HOME"] = sandbox_home + "/.hermes"
            cwd = None
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SkillInstallError("Hermes Skill command could not complete") from exc
        if result.returncode != 0:
            raise SkillInstallError(
                "Hermes Skill command failed with exit code %d" % result.returncode
            )

    @staticmethod
    def _installed(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = lock.get("installed")
        if not isinstance(raw, dict):
            raise SkillInstallError("Skill hub lock has no installed inventory")
        installed: dict[str, dict[str, Any]] = {}
        for name, entry in raw.items():
            if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
                raise SkillInstallError(
                    "Skill hub lock contains an unsafe metadata name"
                )
            if not isinstance(entry, dict):
                raise SkillInstallError(
                    "Skill hub lock contains invalid installed metadata"
                )
            installed[name] = entry
        return installed

    @staticmethod
    def _entry_root(skills_root: Path, entry: dict[str, Any]) -> Path:
        install_path = str(entry.get("install_path") or "").strip()
        if not install_path or Path(install_path).is_absolute():
            raise SkillInstallError("installed Skill has no safe install_path")
        root = (skills_root / install_path).resolve(strict=True)
        canonical_root = skills_root.resolve(strict=True)
        if root == canonical_root or not root.is_relative_to(canonical_root):
            raise SkillInstallError("installed Skill escaped the Skills directory")
        if not root.is_dir():
            raise SkillInstallError("installed Skill path is not a directory")
        return root

    def _validate_provenance(
        self,
        *,
        name: str,
        entry: dict[str, Any],
        root: Path,
        identifier: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        verdict = entry.get("scan_verdict")
        provenance = entry.get("scan_provenance")
        if not verdict and isinstance(provenance, dict):
            verdict = provenance.get("verdict")
        verdict_value = str(verdict or "").strip().lower()
        if verdict_value not in SAFE_SCAN_VERDICTS:
            raise SkillInstallError("installed Skill did not receive a safe scan verdict")
        if not isinstance(provenance, dict) or not provenance:
            raise SkillInstallError("installed Skill has no scan provenance")
        provenance_verdict = str(provenance.get("verdict") or "").strip().lower()
        if provenance_verdict != verdict_value:
            raise SkillInstallError("Skill scan verdict and provenance disagree")
        files, bundle_hash = _hermes_inventory(root)
        expected_full = "sha256:" + bundle_hash
        expected_short = "sha256:" + bundle_hash[:16]
        content_hash = str(entry.get("content_hash") or "").strip().lower()
        if not HUB_HASH_RE.fullmatch(content_hash) or not hmac.compare_digest(
            content_hash, expected_short
        ):
            raise SkillInstallError("Skill hub content hash does not match installed bytes")
        provenance_hash = str(provenance.get("bundle_hash") or "").strip().lower()
        if not FULL_HASH_RE.fullmatch(provenance_hash) or not hmac.compare_digest(
            provenance_hash, expected_full
        ):
            raise SkillInstallError("Skill scan provenance is not bound to installed bytes")
        source = str(entry.get("source") or "").strip()
        provenance_source = str(provenance.get("source") or "").strip()
        source_url = str(provenance.get("source_url") or "").strip()
        recorded_identifier = str(entry.get("identifier") or "").strip()
        if not source or provenance_source != source:
            raise SkillInstallError("Skill source and scan provenance disagree")
        if not source_url:
            raise SkillInstallError("Skill scan provenance does not match its source")
        source_identifier = recorded_identifier or str(identifier or "").strip()
        if source_identifier and not self._source_matches_identifier(
            source_url,
            source_identifier,
        ):
            raise SkillInstallError("Skill scan provenance does not match its source")
        if not str(provenance.get("scanner_version") or "").strip():
            raise SkillInstallError("Skill scan provenance has no scanner version")
        if not str(provenance.get("scanned_at") or "").strip():
            raise SkillInstallError("Skill scan provenance has no scan time")
        findings = provenance.get("findings")
        rules = provenance.get("rules")
        if not isinstance(findings, list) or not isinstance(rules, list):
            raise SkillInstallError("Skill scan provenance has invalid findings")
        for finding in findings:
            if not isinstance(finding, dict):
                raise SkillInstallError("Skill scan provenance has an invalid finding")
            if str(finding.get("severity") or "").lower() in {"critical", "high"}:
                raise SkillInstallError("Skill scan provenance contains a blocking finding")
        return verdict_value, {
            "bundle_sha256": bundle_hash,
            "content_hash": content_hash,
            "file_count": len(files),
            "scan_provenance": provenance,
            "scan_verdict": verdict_value,
        }

    @staticmethod
    def _source_matches_identifier(source_url: str, identifier: str) -> bool:
        source_value = source_url.strip().rstrip("/")
        identifier_value = identifier.strip().rstrip("/")
        if not source_value or not identifier_value:
            return False
        source_parsed = urlparse(source_value)
        identifier_parsed = urlparse(identifier_value)
        if source_parsed.scheme or identifier_parsed.scheme:
            return source_value == identifier_value
        requested = identifier_value.rsplit("@", 1)[0].strip("/")
        canonical = source_value.rsplit("@", 1)[0].strip("/")
        return canonical == requested or canonical.endswith("/" + requested)

    def _contract_audit(self, root: Path, expected_name: str) -> dict[str, Any]:
        manifest = root / "SKILL.md"
        if manifest.is_symlink() or not manifest.is_file():
            raise SkillInstallError("Skill has no regular root SKILL.md")
        nested = [
            path
            for path in root.rglob("SKILL.md")
            if path.resolve(strict=True) != manifest.resolve(strict=True)
        ]
        if nested:
            raise SkillInstallError("Skill contains nested manifests")
        metadata = _frontmatter(manifest)
        if metadata["name"] != expected_name:
            raise SkillInstallError("Skill manifest name does not match hub metadata")
        if not metadata["description"] or len(metadata["description"]) > 2000:
            raise SkillInstallError("Skill manifest has no valid description")
        text = manifest.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>'\"")
            parsed = urlparse(target)
            if not target or target.startswith("#") or parsed.scheme in {
                "https",
                "http",
                "mailto",
            }:
                continue
            resolved = (root / unquote(target).split("#", 1)[0]).resolve(strict=False)
            canonical = root.resolve(strict=True)
            if resolved == canonical or not resolved.is_relative_to(canonical):
                raise SkillInstallError("Skill manifest contains an escaping reference")
            if not resolved.exists():
                raise SkillInstallError("Skill manifest references a missing local file")
        return metadata

    def _static_audit(self, root: Path) -> tuple[dict[str, str], str]:
        files, bundle_hash = _hermes_inventory(root)
        if not files or len(files) > 200:
            raise SkillInstallError("Skill file inventory is empty or too large")
        total_bytes = 0
        for relative in files:
            path = root / relative
            normalized_relative = unicodedata.normalize("NFKC", relative)
            if normalized_relative != relative or any(
                character in relative for character in BIDI_CONTROLS
            ):
                raise SkillInstallError("Skill contains an ambiguous file name")
            size = path.stat().st_size
            total_bytes += size
            if size > 5 * 1024 * 1024 or total_bytes > 10 * 1024 * 1024:
                raise SkillInstallError("Skill content exceeds the audit size limit")
            suffix = path.suffix.lower()
            if suffix in BINARY_SUFFIXES:
                raise SkillInstallError("Skill contains an executable binary")
            if suffix in ARCHIVE_SUFFIXES:
                raise SkillInstallError("Skill contains an opaque archive")
            data = path.read_bytes()
            text = _decoded_text(data, suffix)
            if text is not None:
                _audit_text(text, code=suffix in CODE_SUFFIXES)
            if (
                os.name != "nt"
                and os.access(path, os.X_OK)
                and suffix not in CODE_SUFFIXES
            ):
                raise SkillInstallError("Skill contains an unexpected executable file")
        return files, bundle_hash

    def _staging_parent(self) -> Path:
        if self.trust_root is not None:
            parent = self.trust_root / "staging"
        else:
            parent = self.hermes_home.parent / ".skill-staging"
        parent.mkdir(parents=True, exist_ok=True)
        return parent

    @contextmanager
    def _staging_home(self) -> Iterator[tuple[Path, Path, Path]]:
        stage_root = Path(
            tempfile.mkdtemp(prefix="stage-", dir=str(self._staging_parent()))
        )
        try:
            stage_root.chmod(0o700)
            stage_home = stage_root / "home"
            stage_hermes = stage_home / ".hermes"
            stage_skills = stage_hermes / "skills"
            stage_hermes.mkdir(parents=True)
            live_skills = self.skills_root.resolve(strict=True)
            _hermes_inventory(live_skills)
            shutil.copytree(live_skills, stage_skills, symlinks=False)
            if self.integrity_lock.exists():
                shutil.copy2(self.integrity_lock, stage_hermes / "skills-lock.json")
            else:
                _atomic_write_json(
                    stage_hermes / "skills-lock.json",
                    {"lock_version": 1, "skills": [], "dynamic_skills": []},
                )
            self._active_home = stage_home
            self._active_skills_root = stage_skills
            yield stage_home, stage_skills, stage_hermes / "skills-lock.json"
        finally:
            self._active_home = None
            self._active_skills_root = None
            shutil.rmtree(stage_root, ignore_errors=True)

    def _update_integrity_lock(
        self,
        path: Path,
        installed: list[dict[str, Any]],
    ) -> None:
        if path.exists():
            lock = _json_object(path)
        else:
            lock = {"lock_version": 1, "skills": []}
        names = {str(item["name"]) for item in installed}
        static = lock.get("skills")
        if not isinstance(static, list):
            static = []
        lock["skills"] = [
            item
            for item in static
            if isinstance(item, dict) and str(item.get("name") or "") not in names
        ]
        dynamic = lock.get("dynamic_skills")
        if not isinstance(dynamic, list):
            dynamic = []
        dynamic = [
            item
            for item in dynamic
            if isinstance(item, dict) and str(item.get("name") or "") not in names
        ]
        dynamic.extend(installed)
        revoked = lock.get("revoked_skills")
        if not isinstance(revoked, list):
            revoked = []
        lock["revoked_skills"] = [
            item
            for item in revoked
            if isinstance(item, dict) and str(item.get("name") or "") not in names
        ]
        lock["dynamic_skills"] = sorted(
            dynamic,
            key=lambda item: str(item.get("name") or ""),
        )
        lock["updated_at"] = time.time()
        _atomic_write_json(path, lock)

    def _remove_from_integrity_lock(
        self,
        path: Path,
        *,
        name: str,
        previous: dict[str, Any],
    ) -> None:
        lock = _json_object(path)
        for key in ("skills", "dynamic_skills"):
            raw = lock.get(key)
            lock[key] = [
                item
                for item in raw
                if isinstance(item, dict) and str(item.get("name") or "") != name
            ] if isinstance(raw, list) else []
        revoked = lock.get("revoked_skills")
        if not isinstance(revoked, list):
            revoked = []
        revoked = [
            item
            for item in revoked
            if isinstance(item, dict) and str(item.get("name") or "") != name
        ]
        revoked.append(
            {
                "name": name,
                "bundle_sha256": previous["bundle_sha256"],
                "revoked_at": time.time(),
            }
        )
        lock["revoked_skills"] = sorted(revoked, key=lambda item: item["name"])
        lock["updated_at"] = time.time()
        _atomic_write_json(path, lock)

    def _write_transaction(
        self,
        value: dict[str, Any] | Path,
        *,
        had_integrity_lock: bool | None = None,
    ) -> None:
        if isinstance(value, Path):
            value = {
                "backup_root": str(value),
                "had_integrity_lock": bool(had_integrity_lock),
            }
        elif had_integrity_lock is not None:
            value = {**value, "had_integrity_lock": bool(had_integrity_lock)}
        _atomic_write_json(self.transaction_file, value)

    def _clear_transaction(self) -> None:
        self.transaction_file.unlink(missing_ok=True)

    @staticmethod
    def _safe_release_path(root: Path, value: Any) -> Path:
        candidate = Path(str(value or "")).resolve(strict=False)
        releases = (root / "releases").resolve(strict=True)
        if candidate.parent != releases:
            raise SkillInstallError("Skill transaction release path is invalid")
        return candidate

    @staticmethod
    def _switch_symlink(link: Path, target: str) -> None:
        temporary = link.with_name(link.name + ".next-" + uuid.uuid4().hex)
        try:
            os.symlink(target, temporary, target_is_directory=True)
            os.replace(temporary, link)
            _fsync_directory(link.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _recover_incomplete(self) -> bool:
        if not self.transaction_file.exists():
            return False
        transaction = _json_object(self.transaction_file)
        mode = str(transaction.get("mode") or "")
        if mode == "managed":
            if self.trust_root is None:
                raise SkillInstallError("managed Skill transaction has no trust root")
            root = self.trust_root.resolve(strict=True)
            current = root / "current"
            previous_target = str(transaction.get("previous_target") or "")
            new_release = self._safe_release_path(root, transaction.get("new_release"))
            if not previous_target.startswith("releases/"):
                raise SkillInstallError("Skill transaction previous target is invalid")
            self._switch_symlink(current, previous_target)
            if new_release.exists():
                shutil.rmtree(new_release)
            self._clear_transaction()
            return True
        if mode == "unmanaged":
            live = self.skills_root
            expected_parent = live.parent.resolve(strict=True)
            backup = Path(str(transaction.get("backup_skills") or "")).resolve(
                strict=False
            )
            candidate = Path(str(transaction.get("candidate_skills") or "")).resolve(
                strict=False
            )
            if (
                backup.parent != expected_parent
                or candidate.parent != expected_parent
                or not backup.name.startswith(".skill-backup-")
                or not candidate.name.startswith(".skill-publish-")
            ):
                raise SkillInstallError("Skill transaction path is invalid")
            if backup.is_dir():
                if live.exists():
                    shutil.rmtree(live)
                os.replace(backup, live)
            candidate_integrity = Path(
                str(transaction.get("backup_integrity") or "")
            ).resolve(strict=False)
            if candidate_integrity.is_file():
                if bool(transaction.get("had_integrity_lock")):
                    self.integrity_lock.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(candidate_integrity, self.integrity_lock)
                else:
                    self.integrity_lock.unlink(missing_ok=True)
                    candidate_integrity.unlink(missing_ok=True)
            if candidate.exists():
                shutil.rmtree(candidate)
            self._clear_transaction()
            return True
        if "backup_root" in transaction:
            backup_root = Path(str(transaction.get("backup_root") or ""))
            expected_parent = self.skills_root.parent.resolve(strict=True)
            resolved_backup = backup_root.resolve(strict=True)
            if (
                resolved_backup.parent != expected_parent
                or not resolved_backup.name.startswith(".skill-install-")
            ):
                raise SkillInstallError("legacy Skill transaction path is invalid")
            backup_skills = resolved_backup / "skills"
            if self.skills_root.exists():
                shutil.rmtree(self.skills_root)
            shutil.copytree(backup_skills, self.skills_root)
            integrity_backup = resolved_backup / "integrity-lock"
            if bool(transaction.get("had_integrity_lock", integrity_backup.exists())):
                shutil.copy2(integrity_backup, self.integrity_lock)
            else:
                self.integrity_lock.unlink(missing_ok=True)
            shutil.rmtree(resolved_backup)
            self._clear_transaction()
            return True
        raise SkillInstallError("Skill transaction metadata is invalid")

    def recover_incomplete(self) -> bool:
        with self._lock, self._process_lock():
            return self._recover_incomplete()

    @staticmethod
    def _secure_managed_release(root: Path, group_gid: int) -> None:
        paths = [root, *root.rglob("*")]
        executable: dict[Path, bool] = {}
        for path in paths:
            if path.is_symlink():
                raise SkillInstallError("refusing to publish a symbolic link")
            metadata = path.stat()
            if not (path.is_dir() or path.is_file()):
                raise SkillInstallError("refusing to publish a special file")
            executable[path] = bool(metadata.st_mode & 0o111)

        if os.name != "nt":
            for path in paths:
                try:
                    os.chown(path, -1, group_gid)
                except OSError as exc:
                    raise SkillInstallError(
                        "managed Skill release group could not be applied"
                    ) from exc

        for path in paths:
            if path.is_file():
                path.chmod(0o550 if executable[path] else 0o440)
        for path in sorted(
            (candidate for candidate in paths if candidate.is_dir()),
            key=lambda candidate: len(candidate.parts),
            reverse=True,
        ):
            path.chmod(0o550)

        for path in paths:
            metadata = path.stat()
            expected_mode = (
                0o550
                if path.is_dir() or executable[path]
                else 0o440
            )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise SkillInstallError(
                    "managed Skill release permissions could not be applied"
                )
            if os.name != "nt" and metadata.st_gid != group_gid:
                raise SkillInstallError(
                    "managed Skill release group does not match runtime group"
                )

    def _publish_managed(self, stage_skills: Path, stage_integrity: Path) -> None:
        assert self.trust_root is not None
        root = self.trust_root.resolve(strict=True)
        releases = (root / "releases").resolve(strict=True)
        current = root / "current"
        if not current.is_symlink():
            raise SkillInstallError("Skill trust root has no atomic current pointer")
        previous_target = os.readlink(current)
        if not previous_target.startswith("releases/"):
            raise SkillInstallError("Skill trust current pointer is invalid")
        release_name = "release-%d-%s" % (time.time_ns(), uuid.uuid4().hex[:12])
        pending = releases / (".pending-" + release_name)
        release = releases / release_name
        pending.mkdir(mode=0o750)
        shutil.copytree(stage_skills, pending / "skills", symlinks=False)
        shutil.copy2(stage_integrity, pending / "skills-lock.json")
        self._secure_managed_release(pending, releases.stat().st_gid)
        os.replace(pending, release)
        self._write_transaction(
            {
                "mode": "managed",
                "previous_target": previous_target,
                "new_release": str(release),
            }
        )
        try:
            self._switch_symlink(current, "releases/" + release_name)
        except Exception:
            shutil.rmtree(release, ignore_errors=True)
            self._clear_transaction()
            raise
        self._clear_transaction()

    def _publish_unmanaged(self, stage_skills: Path, stage_integrity: Path) -> None:
        live = self.skills_root
        parent = live.parent.resolve(strict=True)
        nonce = uuid.uuid4().hex
        candidate = parent / (".skill-publish-" + nonce)
        backup = parent / (".skill-backup-" + nonce)
        backup_integrity = parent / (".skill-integrity-backup-" + nonce)
        shutil.copytree(stage_skills, candidate, symlinks=False)
        if self.integrity_lock.exists():
            shutil.copy2(self.integrity_lock, backup_integrity)
            had_integrity_lock = True
        else:
            backup_integrity.write_bytes(b"")
            had_integrity_lock = False
        self._write_transaction(
            {
                "mode": "unmanaged",
                "backup_skills": str(backup),
                "candidate_skills": str(candidate),
                "backup_integrity": str(backup_integrity),
                "had_integrity_lock": had_integrity_lock,
            }
        )
        try:
            os.replace(live, backup)
            os.replace(candidate, live)
            if stage_integrity.exists():
                temporary = self.integrity_lock.with_name(
                    self.integrity_lock.name + ".next-" + nonce
                )
                shutil.copy2(stage_integrity, temporary)
                os.replace(temporary, self.integrity_lock)
            shutil.rmtree(backup)
            backup_integrity.unlink(missing_ok=True)
            self._clear_transaction()
        except Exception:
            if live.exists():
                shutil.rmtree(live)
            if backup.exists():
                os.replace(backup, live)
            if backup_integrity.exists():
                if backup_integrity.stat().st_size:
                    os.replace(backup_integrity, self.integrity_lock)
                else:
                    self.integrity_lock.unlink(missing_ok=True)
                    backup_integrity.unlink(missing_ok=True)
            if candidate.exists():
                shutil.rmtree(candidate)
            self._clear_transaction()
            raise

    def _publish(self, stage_skills: Path, stage_integrity: Path) -> None:
        if self.trust_root is not None:
            self._publish_managed(stage_skills, stage_integrity)
        else:
            self._publish_unmanaged(stage_skills, stage_integrity)

    @staticmethod
    def _metadata_entries(lock: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
        entries: dict[str, dict[str, Any]] = {}
        for key in ("skills", "dynamic_skills"):
            raw = lock.get(key)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    raise SkillInstallError("Skills integrity lock contains invalid entries")
                name = str(item.get("name") or "").strip()
                if not SKILL_NAME_RE.fullmatch(name) or name in entries:
                    raise SkillInstallError("Skills integrity lock contains unsafe names")
                entries[name] = item
        revoked: set[str] = set()
        raw_revoked = lock.get("revoked_skills")
        if isinstance(raw_revoked, list):
            for item in raw_revoked:
                if not isinstance(item, dict):
                    raise SkillInstallError("Skills integrity lock has invalid revocations")
                name = str(item.get("name") or "").strip()
                if not SKILL_NAME_RE.fullmatch(name):
                    raise SkillInstallError("Skills integrity lock has an unsafe revocation")
                revoked.add(name)
        return entries, revoked

    def _inventory_at(
        self,
        skills_root: Path,
        integrity_path: Path,
    ) -> list[dict[str, Any]]:
        integrity = _json_object(integrity_path)
        metadata_entries, revoked = self._metadata_entries(integrity)
        hub_path = skills_root / ".hub" / "lock.json"
        hub = self._installed(_json_object(hub_path))
        roots = _discover_skill_roots(skills_root)
        present_revoked = revoked.intersection(roots)
        if present_revoked:
            raise SkillInstallError(
                "revoked Skill is present in live tree: "
                + ", ".join(sorted(present_revoked))
            )
        inventory: list[dict[str, Any]] = []
        for name, root in sorted(roots.items()):
            metadata = _frontmatter(root / "SKILL.md")
            files, bundle_hash = _hermes_inventory(root)
            locked = metadata_entries.get(name, {})
            install_path = root.relative_to(skills_root).as_posix()
            locked_path = str(locked.get("install_path") or "").strip()
            if locked_path and locked_path != install_path:
                raise SkillInstallError("Skill integrity path does not match live tree")
            hub_entry = hub.get(name)
            if hub_entry is not None:
                verdict, provenance_audit = self._validate_provenance(
                    name=name,
                    entry=hub_entry,
                    root=root,
                )
                source = str(
                    hub_entry.get("identifier")
                    or hub_entry.get("source")
                    or locked.get("source")
                    or "hub"
                )
                audit = {
                    **provenance_audit,
                    "integrity_lock_present": bool(locked),
                    "verdict": verdict,
                }
            else:
                source = str(locked.get("source") or "builtin")
                audit = {
                    "bundle_sha256": bundle_hash,
                    "file_count": len(files),
                    "integrity_lock_present": bool(locked),
                    "verdict": "trusted-live-inventory",
                }
            locked_bundle = str(locked.get("bundle_sha256") or "").lower()
            algorithm = str(locked.get("bundle_algorithm") or "")
            if (
                locked_bundle
                and algorithm == "hermes-full-v1"
                and not hmac.compare_digest(locked_bundle, bundle_hash)
            ):
                raise SkillInstallError("Skill differs from its integrity lock")
            inventory.append(
                {
                    "name": name,
                    "version": metadata["version"],
                    "source": source,
                    "bundle_sha256": bundle_hash,
                    "sha256": bundle_hash,
                    "capabilities": metadata["capabilities"],
                    "audit": audit,
                    "install_path": install_path,
                }
            )
        absent_locked = set(metadata_entries) - set(roots)
        if absent_locked:
            raise SkillInstallError(
                "Skills integrity lock references missing content: "
                + ", ".join(sorted(absent_locked))
            )
        absent_hub = set(hub) - set(roots)
        if absent_hub:
            raise SkillInstallError(
                "Skill hub lock references missing content: "
                + ", ".join(sorted(absent_hub))
            )
        return inventory

    def inventory_current(self) -> list[dict[str, Any]]:
        with self._lock, self._process_lock():
            self._recover_incomplete()
            return self._inventory_at(
                self.skills_root.resolve(strict=True),
                self.integrity_lock.resolve(strict=True),
            )

    @staticmethod
    def _snapshot_identity(
        snapshot: Any,
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        if not isinstance(snapshot, list):
            raise SkillInstallError("Skill snapshot must be a list")
        expected: dict[str, tuple[str, tuple[str, ...]]] = {}
        for item in snapshot:
            if not isinstance(item, dict):
                raise SkillInstallError("Skill snapshot contains an invalid entry")
            name = str(item.get("name") or "").strip()
            digest = str(
                item.get("sha256") or item.get("bundle_sha256") or ""
            ).strip().lower()
            capabilities = item.get("capabilities")
            if capabilities is None:
                capabilities = []
            if (
                not SKILL_NAME_RE.fullmatch(name)
                or not SHA256_RE.fullmatch(digest)
                or not isinstance(capabilities, list)
                or any(
                    not isinstance(value, str)
                    or not CAPABILITY_RE.fullmatch(value.strip())
                    for value in capabilities
                )
            ):
                raise SkillInstallError("Skill snapshot contains invalid identity data")
            if name in expected:
                raise SkillInstallError("Skill snapshot contains duplicate names")
            if item.get("enabled") is False or item.get("revoked_at"):
                raise SkillInstallError("Skill snapshot contains a revoked Skill")
            expected[name] = (
                digest,
                tuple(sorted({value.strip() for value in capabilities})),
            )
        return expected

    @staticmethod
    def _inventory_identity(
        inventory: list[dict[str, Any]],
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        return {
            str(item["name"]): (
                str(item["bundle_sha256"]).lower(),
                tuple(
                    sorted(
                        {
                            str(value).strip()
                            for value in item.get("capabilities", [])
                        }
                    )
                ),
            )
            for item in inventory
        }

    @classmethod
    def _verify_inventory_snapshot(
        cls,
        expected: dict[str, tuple[str, tuple[str, ...]]],
        inventory: list[dict[str, Any]],
    ) -> None:
        current = cls._inventory_identity(inventory)
        missing = set(expected) - set(current)
        extra = set(current) - set(expected)
        if missing:
            raise SkillInstallError(
                "Skill snapshot content is missing: " + ", ".join(sorted(missing))
            )
        if extra:
            raise SkillInstallError(
                "live tree contains Skills outside the task snapshot: "
                + ", ".join(sorted(extra))
            )
        for name, identity in expected.items():
            if not hmac.compare_digest(identity[0], current[name][0]):
                raise SkillInstallError("Skill snapshot hash mismatch for " + name)
            if identity[1] != current[name][1]:
                raise SkillInstallError(
                    "Skill snapshot capabilities mismatch for " + name
                )

    def verify_snapshot(self, snapshot: Any) -> list[dict[str, Any]]:
        expected = self._snapshot_identity(snapshot)
        current = self.inventory_current()
        self._verify_inventory_snapshot(expected, current)
        return current

    def activate_snapshot_runtime(self, snapshot: Any) -> dict[str, Any]:
        if self.trust_root is None:
            inventory = self.verify_snapshot(snapshot)
            return {
                "inventory": inventory,
                "skills_root": str(self.skills_root.resolve(strict=True)),
                "changed": False,
            }
        expected = self._snapshot_identity(snapshot)
        with self._lock, self._process_lock():
            self._recover_incomplete()
            root = self.trust_root.resolve(strict=True)
            releases = (root / "releases").resolve(strict=True)
            matches: list[tuple[Path, list[dict[str, Any]]]] = []
            for release in sorted(
                (path for path in releases.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            ):
                skills = release / "skills"
                integrity = release / "skills-lock.json"
                if not skills.is_dir() or not integrity.is_file():
                    continue
                try:
                    inventory = self._inventory_at(
                        skills.resolve(strict=True),
                        integrity.resolve(strict=True),
                    )
                    self._verify_inventory_snapshot(expected, inventory)
                except (SkillInstallError, OSError, ValueError, UnicodeError):
                    continue
                matches.append((release, inventory))
                break
            if not matches:
                raise SkillInstallError(
                    "no trusted Skill release matches the task snapshot"
                )
            release, inventory = matches[0]
            active = root / "active"
            target = "releases/" + release.name
            if active.is_symlink() and os.readlink(active) == target:
                return {
                    "inventory": inventory,
                    "skills_root": str((release / "skills").resolve(strict=True)),
                    "changed": False,
                }
            if active.exists() and not active.is_symlink():
                raise SkillInstallError("Skill trust active pointer is not a symlink")
            self._switch_symlink(active, target)
            return {
                "inventory": inventory,
                "skills_root": str((release / "skills").resolve(strict=True)),
                "changed": True,
            }

    def activate_snapshot(self, snapshot: Any) -> list[dict[str, Any]]:
        return list(self.activate_snapshot_runtime(snapshot)["inventory"])

    @staticmethod
    def _check_install_canceled(
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if cancel_requested is not None and cancel_requested():
            raise SkillInstallError("Skill installation was canceled")

    def install(
        self,
        identifier: str,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        value = validate_skill_identifier(identifier)
        self._check_install_canceled(cancel_requested)
        with self._lock, self._process_lock():
            self._recover_incomplete()
            self._check_install_canceled(cancel_requested)
            live_skills = self.skills_root.resolve(strict=True)
            before_lock = _json_object(live_skills / ".hub" / "lock.json")
            before_installed = self._installed(before_lock)
            before_roots = _discover_skill_roots(live_skills)
            before_hashes = {
                name: _hermes_inventory(root)[1]
                for name, root in before_roots.items()
            }
            with self._staging_home() as (
                _stage_home,
                stage_skills,
                stage_integrity,
            ):
                self._run(
                    "skills",
                    "install",
                    "--yes",
                    value,
                    allow_network=True,
                )
                self._check_install_canceled(cancel_requested)
                after_lock_path = stage_skills / ".hub" / "lock.json"
                after_lock = _json_object(after_lock_path)
                after_installed = self._installed(after_lock)
                removed_names = sorted(set(before_installed) - set(after_installed))
                if removed_names:
                    raise SkillInstallError(
                        "Skill installation removed existing metadata"
                    )
                changed_names = sorted(
                    name
                    for name, entry in after_installed.items()
                    if before_installed.get(name) != entry
                )
                if not changed_names:
                    raise SkillInstallError(
                        "Skill installation made no verifiable metadata change"
                    )
                after_roots = _discover_skill_roots(stage_skills)
                if set(before_roots) - set(after_roots):
                    raise SkillInstallError("Skill installation removed existing content")
                unexpected = set(after_roots) - set(before_roots) - set(changed_names)
                if unexpected:
                    raise SkillInstallError("Skill installation created untracked content")
                for name, digest in before_hashes.items():
                    if name in changed_names:
                        continue
                    if _hermes_inventory(after_roots[name])[1] != digest:
                        raise SkillInstallError(
                            "Skill installation modified unrelated content"
                        )

                audited: list[dict[str, Any]] = []
                for name in changed_names:
                    self._check_install_canceled(cancel_requested)
                    entry = after_installed[name]
                    root = self._entry_root(stage_skills, entry)
                    metadata = self._contract_audit(root, name)
                    self._static_audit(root)
                    self._validate_provenance(
                        name=name,
                        entry=entry,
                        root=root,
                        identifier=value,
                    )
                    self._run("skills", "audit", "--deep", name)
                    self._check_install_canceled(cancel_requested)
                    self._run("curator", "pin", name)
                    self._check_install_canceled(cancel_requested)
                    final_lock = self._installed(_json_object(after_lock_path))
                    final_entry = final_lock.get(name)
                    if final_entry is None:
                        raise SkillInstallError("Skill disappeared while being pinned")
                    final_root = self._entry_root(stage_skills, final_entry)
                    final_metadata = self._contract_audit(final_root, name)
                    final_files, final_bundle_hash = self._static_audit(final_root)
                    verdict, provenance_audit = self._validate_provenance(
                        name=name,
                        entry=final_entry,
                        root=final_root,
                        identifier=value,
                    )
                    if final_metadata != metadata:
                        raise SkillInstallError(
                            "Skill metadata changed while it was being pinned"
                        )
                    source = str(
                        final_entry.get("identifier")
                        or final_entry.get("source")
                        or value
                    )
                    audit = {
                        **provenance_audit,
                        "contract": "passed",
                        "deep_audit": "passed",
                        "pinned": True,
                    }
                    audited.append(
                        {
                            "name": name,
                            "version": metadata["version"],
                            "source": source,
                            "install_path": final_root.relative_to(
                                stage_skills
                            ).as_posix(),
                            "content_hash": str(final_entry["content_hash"]),
                            "bundle_sha256": final_bundle_hash,
                            "sha256": final_bundle_hash,
                            "bundle_algorithm": "hermes-full-v1",
                            "file_count": len(final_files),
                            "capabilities": metadata["capabilities"],
                            "audit": audit,
                            "scan_verdict": verdict,
                            "pinned": True,
                        }
                    )
                self._update_integrity_lock(stage_integrity, audited)
                self._inventory_at(stage_skills, stage_integrity)
                self._check_install_canceled(cancel_requested)
                self._publish(stage_skills, stage_integrity)
                return {
                    "installed": audited,
                    "identifier_sha256": hashlib.sha256(
                        value.encode("utf-8")
                    ).hexdigest(),
                }

    def revoke(self, name: str) -> dict[str, Any]:
        skill_name = str(name or "").strip()
        if not SKILL_NAME_RE.fullmatch(skill_name):
            raise ValueError("invalid Skill name")
        with self._lock, self._process_lock():
            self._recover_incomplete()
            current = {
                item["name"]: item
                for item in self._inventory_at(
                    self.skills_root.resolve(strict=True),
                    self.integrity_lock.resolve(strict=True),
                )
            }
            previous = current.get(skill_name)
            if previous is None:
                raise SkillInstallError("Skill is not installed")
            with self._staging_home() as (
                _stage_home,
                stage_skills,
                stage_integrity,
            ):
                stage_inventory = {
                    item["name"]: item
                    for item in self._inventory_at(stage_skills, stage_integrity)
                }
                entry = stage_inventory[skill_name]
                root = (stage_skills / entry["install_path"]).resolve(strict=True)
                if root == stage_skills.resolve(strict=True) or not root.is_relative_to(
                    stage_skills.resolve(strict=True)
                ):
                    raise SkillInstallError("refusing to revoke an unsafe Skill path")
                shutil.rmtree(root)
                hub_path = stage_skills / ".hub" / "lock.json"
                hub_lock = _json_object(hub_path)
                installed = self._installed(hub_lock)
                installed.pop(skill_name, None)
                hub_lock["installed"] = installed
                _atomic_write_json(hub_path, hub_lock)
                self._remove_from_integrity_lock(
                    stage_integrity,
                    name=skill_name,
                    previous=previous,
                )
                remaining = self._inventory_at(stage_skills, stage_integrity)
                if skill_name in {item["name"] for item in remaining}:
                    raise SkillInstallError("Skill revocation did not remove live content")
                self._publish(stage_skills, stage_integrity)
                return {
                    "name": skill_name,
                    "revoked": True,
                    "version": previous["version"],
                    "source": previous["source"],
                    "bundle_sha256": previous["bundle_sha256"],
                    "sha256": previous["bundle_sha256"],
                    "capabilities": previous["capabilities"],
                    "audit": {
                        **previous["audit"],
                        "revoked_at": time.time(),
                    },
                }
