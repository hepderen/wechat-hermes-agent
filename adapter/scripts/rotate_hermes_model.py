from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx
import yaml


DEFAULT_CONFIG = Path(
    "/var/lib/wechat-hermes/workspace/home/.hermes/config.yaml"
)
DEFAULT_BACKUP_ROOT = Path("/var/backups/wechat-hermes")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
SECRET_RE = re.compile(r"sk-[A-Za-z0-9._-]{12,}")


class RotationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RotationSecret:
    api_key: str
    requested_model: str


@dataclass(frozen=True)
class PreflightResult:
    requested_model: str
    resolved_model: str
    models_status: int
    model_count: int
    model_listed: bool
    chat_status: int
    content_nonempty: bool
    returned_model: str
    total_tokens: int | None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redact(value: object, *secrets: str) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return SECRET_RE.sub("[REDACTED]", text)


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RotationError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise RotationError(f"{label} must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise RotationError(f"{label} must be a regular file")
    return info


def _validate_private_file(path: Path, label: str) -> os.stat_result:
    info = _require_regular_file(path, label)
    if os.name == "posix":
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RotationError(f"{label} must not be readable by group or others")
        allowed_owners = {os.geteuid()}
        sudo_uid = os.getenv("SUDO_UID", "").strip()
        if sudo_uid.isdigit():
            allowed_owners.add(int(sudo_uid))
        if info.st_uid not in allowed_owners:
            raise RotationError(f"{label} has an unexpected owner")
    return info


def load_rotation_secret(path: Path) -> RotationSecret:
    _validate_private_file(path, "rotation secret file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RotationError("rotation secret file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise RotationError("rotation secret file must contain a JSON object")

    api_key = str(payload.get("api_key") or "")
    model = str(payload.get("model") or "").strip()
    if len(api_key) < 16 or len(api_key) > 4096:
        raise RotationError("provider API key has an invalid length")
    if any(char.isspace() for char in api_key):
        raise RotationError("provider API key must not contain whitespace")
    if not MODEL_ID_RE.fullmatch(model):
        raise RotationError("requested model ID has an invalid format")
    return RotationSecret(api_key=api_key, requested_model=model)


def load_config(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    info = _require_regular_file(path, "Hermes config")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RotationError("Hermes config is not valid UTF-8 YAML") from exc
    if not isinstance(payload, dict):
        raise RotationError("Hermes config must contain a YAML mapping")
    model = payload.get("model")
    if not isinstance(model, dict):
        raise RotationError("Hermes config is missing the model mapping")
    return payload, info


def validate_base_url(value: object) -> str:
    base_url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RotationError("model base URL must not contain credentials or extras")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RotationError("model base URL must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise RotationError("external model base URL must use HTTPS")
    return base_url


def _model_forms(value: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", "", value.lower())
    forms = {normalized}
    for prefix in ("gpt", "openai"):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            forms.add(normalized[len(prefix) :])
    return {item for item in forms if item}


def resolve_model_id(requested: str, available: list[str]) -> str:
    unique = sorted({str(item).strip() for item in available if str(item).strip()})
    if requested in unique:
        return requested

    case_matches = [item for item in unique if item.lower() == requested.lower()]
    if len(case_matches) == 1:
        return case_matches[0]

    requested_forms = _model_forms(requested)
    matches = [
        item for item in unique if requested_forms.intersection(_model_forms(item))
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RotationError(
            "requested model alias is ambiguous: " + ", ".join(matches[:8])
        )

    nearby = [
        item
        for item in unique
        if any(form in "".join(_model_forms(item)) for form in requested_forms)
    ][:8]
    suffix = ": " + ", ".join(nearby) if nearby else ""
    raise RotationError("requested model is not advertised by the provider" + suffix)


def model_ids_compatible(requested: str, returned: str) -> bool:
    requested_forms = _model_forms(requested)
    returned_forms = _model_forms(returned)
    return any(
        left == right or left.startswith(right) or right.startswith(left)
        for left in requested_forms
        for right in returned_forms
    )


def _response_error_type(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return "non_json_provider_error"
    if not isinstance(payload, dict):
        return "invalid_provider_error"
    error = payload.get("error") or {}
    if not isinstance(error, dict):
        return "provider_error"
    return str(error.get("type") or error.get("code") or "provider_error")[:120]


def preflight_provider(
    base_url: str,
    secret: RotationSecret,
    *,
    timeout_seconds: float = 60,
    client: httpx.Client | None = None,
) -> PreflightResult:
    base_url = validate_base_url(base_url)
    headers = {
        "Authorization": "Bearer " + secret.api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
            follow_redirects=True,
        )
    try:
        try:
            models_response = client.get(base_url + "/models", headers=headers)
        except httpx.HTTPError as exc:
            raise RotationError(
                "provider model-list request failed: " + type(exc).__name__
            ) from exc

        available: list[str] = []
        if models_response.status_code == 200:
            try:
                models_payload = models_response.json()
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RotationError("provider model list returned invalid JSON") from exc
            items = models_payload.get("data") if isinstance(models_payload, dict) else []
            if isinstance(items, list):
                available = [
                    str(item.get("id"))
                    for item in items
                    if isinstance(item, dict) and item.get("id")
                ]
        elif models_response.status_code in {401, 403}:
            raise RotationError(
                "provider model-list authentication failed with HTTP "
                + str(models_response.status_code)
            )

        resolved_model = (
            resolve_model_id(secret.requested_model, available)
            if available
            else secret.requested_model
        )
        try:
            chat_response = client.post(
                base_url + "/chat/completions",
                headers=headers,
                json={
                    "model": resolved_model,
                    "messages": [
                        {"role": "user", "content": "Return the exact text OK."}
                    ],
                    "max_tokens": 32,
                    "stream": False,
                },
            )
        except httpx.HTTPError as exc:
            raise RotationError(
                "provider chat preflight failed: " + type(exc).__name__
            ) from exc
        if chat_response.status_code != 200:
            raise RotationError(
                "provider chat preflight returned HTTP "
                + str(chat_response.status_code)
                + " ("
                + _response_error_type(chat_response)
                + ")"
            )
        try:
            chat_payload = chat_response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RotationError("provider chat preflight returned invalid JSON") from exc
        choices = chat_payload.get("choices") if isinstance(chat_payload, dict) else []
        if not isinstance(choices, list) or not choices:
            raise RotationError("provider chat preflight returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if not isinstance(message, dict):
            message = {}
        content_nonempty = bool(
            message.get("content") or message.get("reasoning_content")
        )
        if not content_nonempty:
            raise RotationError("provider chat preflight returned empty content")
        returned_model = str(chat_payload.get("model") or "")
        if returned_model and not model_ids_compatible(
            resolved_model,
            returned_model,
        ):
            raise RotationError("provider chat preflight returned another model ID")
        usage = chat_payload.get("usage") if isinstance(chat_payload, dict) else {}
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        if not isinstance(total_tokens, int):
            total_tokens = None
        return PreflightResult(
            requested_model=secret.requested_model,
            resolved_model=resolved_model,
            models_status=models_response.status_code,
            model_count=len(available),
            model_listed=resolved_model in available,
            chat_status=chat_response.status_code,
            content_nonempty=content_nonempty,
            returned_model=returned_model,
            total_tokens=total_tokens,
        )
    finally:
        if owned_client:
            client.close()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def rotation_lock(config_path: Path) -> Iterator[None]:
    lock_path = config_path.parent / ".hermes-model-rotation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(lock_path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RotationError("model rotation lock is not a regular file")
        os.chmod(lock_path, 0o600)
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def apply_rotation(
    config_path: Path,
    backup_root: Path,
    secret: RotationSecret,
    resolved_model: str,
    *,
    expected_sha256: str,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise RotationError("--expected-sha256 must contain 64 hexadecimal digits")
    if not backup_root.is_absolute():
        raise RotationError("backup root must be an absolute path")

    with rotation_lock(config_path):
        config, info = load_config(config_path)
        raw = config_path.read_bytes()
        before_sha256 = hashlib.sha256(raw).hexdigest()
        if before_sha256.lower() != expected_sha256.lower():
            raise RotationError("Hermes config changed after the recorded baseline")

        model = config["model"]
        previous_model = str(model.get("default") or "")
        previous_key = str(model.get("api_key") or "")
        if previous_model == resolved_model and previous_key == secret.api_key:
            return {
                "applied": False,
                "changed": False,
                "config_sha256": before_sha256,
                "model": resolved_model,
            }

        moment = now or dt.datetime.now(dt.timezone.utc)
        stamp = moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            backup_info = backup_root.lstat()
        except FileNotFoundError:
            backup_root.mkdir(parents=True, mode=0o700)
            os.chmod(backup_root, 0o700)
        else:
            if stat.S_ISLNK(backup_info.st_mode) or not stat.S_ISDIR(
                backup_info.st_mode
            ):
                raise RotationError("backup root must be a real directory")
            if os.name == "posix" and stat.S_IMODE(backup_info.st_mode) & 0o022:
                raise RotationError("backup root must not be group/world writable")
        backup_dir = backup_root / (
            "model-rotation-" + stamp + "-" + before_sha256[:12]
        )
        try:
            backup_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RotationError("model rotation backup directory already exists") from exc
        backup_path = backup_dir / "config.yaml.before"
        _write_private_file(backup_path, raw)

        model["api_key"] = secret.api_key
        model["default"] = resolved_model
        rendered = yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        parsed = yaml.safe_load(rendered.decode("utf-8"))
        if (
            not isinstance(parsed, dict)
            or parsed.get("model", {}).get("api_key") != secret.api_key
            or parsed.get("model", {}).get("default") != resolved_model
        ):
            raise RotationError("rendered Hermes config failed validation")

        after_sha256 = hashlib.sha256(rendered).hexdigest()
        manifest = {
            "state": "prepared",
            "timestamp_utc": stamp,
            "config_path": str(config_path),
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "previous_model": previous_model,
            "new_model": resolved_model,
            "key_changed": previous_key != secret.api_key,
        }
        manifest_path = backup_dir / "manifest.json"
        _write_private_file(
            manifest_path,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".config.yaml.rotate-",
            dir=str(config_path.parent),
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, stat.S_IMODE(info.st_mode))
            if os.name == "posix":
                if os.geteuid() == 0:
                    os.chown(temporary_name, info.st_uid, info.st_gid)
                elif info.st_uid != os.geteuid():
                    raise RotationError(
                        "run model rotation as root to preserve config ownership"
                    )
            os.replace(temporary_name, config_path)
            _fsync_directory(config_path.parent)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

        manifest["state"] = "applied"
        manifest_tmp = backup_dir / ".manifest.json.tmp"
        _write_private_file(
            manifest_tmp,
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.replace(manifest_tmp, manifest_path)
        _fsync_directory(backup_dir)
        return {
            "applied": True,
            "changed": True,
            "config_sha256": after_sha256,
            "previous_model": previous_model,
            "model": resolved_model,
            "key_changed": previous_key != secret.api_key,
            "backup_dir": str(backup_dir),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight and atomically rotate the private Hermes model credential."
        )
    )
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the validated rotation. The default is preflight only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    secret: RotationSecret | None = None
    try:
        if args.timeout_seconds <= 0:
            raise RotationError("timeout must be positive")
        if args.apply and not args.expected_sha256:
            raise RotationError("--apply requires --expected-sha256")
        secret = load_rotation_secret(args.secret_file)
        config, _ = load_config(args.config)
        base_url = validate_base_url(config["model"].get("base_url"))
        before_sha256 = file_sha256(args.config)
        preflight = preflight_provider(
            base_url,
            secret,
            timeout_seconds=args.timeout_seconds,
        )
        result: dict[str, object] = {
            "status": "validated",
            "applied": False,
            "config_sha256": before_sha256,
            "preflight": asdict(preflight),
        }
        if args.apply:
            result.update(
                apply_rotation(
                    args.config,
                    args.backup_root,
                    secret,
                    preflight.resolved_model,
                    expected_sha256=args.expected_sha256,
                )
            )
            result["status"] = "applied" if result.get("applied") else "unchanged"
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        key = secret.api_key if secret else ""
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": redact(exc, key),
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
