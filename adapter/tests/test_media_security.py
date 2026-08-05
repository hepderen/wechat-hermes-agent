import struct
import time
import unicodedata
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from app.media import (
    ArtifactSigner,
    actual_mime,
    strip_legacy_delivery_markers,
    validate_media_path,
)


TASK_ID = "T-12AB34CD"


def mp4_box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def valid_mp4() -> bytes:
    return b"".join(
        (
            mp4_box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2"),
            mp4_box(b"moov", b"metadata"),
            mp4_box(b"mdat", b"\x00\x00\x00\x01encoded-frame"),
        )
    )


OOXML_CASES = {
    ".docx": {
        "mime": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "main": "word/document.xml",
        "root": "document",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        ),
    },
    ".xlsx": {
        "mime": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        "main": "xl/workbook.xml",
        "root": "workbook",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet.main+xml"
        ),
    },
    ".pptx": {
        "mime": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "main": "ppt/presentation.xml",
        "root": "presentation",
        "content_type": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml"
        ),
    },
}


def write_ooxml(
    path: Path,
    extension: str,
    *,
    content_type: str | None = None,
    include_main: bool = True,
) -> None:
    case = OOXML_CASES[extension]
    override = content_type if content_type is not None else case["content_type"]
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/%s" ContentType="%s"/>'
        "</Types>"
    ) % (case["main"], override)
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    main = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<%s xmlns="urn:test"/>'
    ) % case["root"]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        if include_main:
            archive.writestr(case["main"], main)


def make_task_root(tmp_path: Path) -> tuple[Path, Path]:
    artifact_root = tmp_path / "artifacts"
    task_root = artifact_root / TASK_ID
    task_root.mkdir(parents=True)
    return artifact_root, task_root


def test_pdf_requires_a_version_header_and_terminal_eof_marker(tmp_path):
    artifact_root, task_root = make_task_root(tmp_path)
    valid = task_root / "valid.pdf"
    valid.write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
    truncated = task_root / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.7\n1 0 obj\n<<>>")

    assert actual_mime(valid, "application/pdf") == "application/pdf"
    assert actual_mime(truncated, "application/pdf") == "application/octet-stream"
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(truncated),
            artifact_root,
            TASK_ID,
            1024,
        )


def test_nested_artifact_keeps_normalized_relative_path_and_same_names(
    tmp_path,
):
    artifact_root, task_root = make_task_root(tmp_path)
    nested = task_root / "renders" / "final.png"
    root_file = task_root / "final.png"
    nested.parent.mkdir()
    nested.write_bytes(b"\x89PNG\r\n\x1a\nnested")
    root_file.write_bytes(b"\x89PNG\r\n\x1a\nroot")

    nested_artifact = validate_media_path(
        str(nested.parent / ".." / "renders" / "final.png"),
        artifact_root,
        TASK_ID,
        1024,
    )
    root_artifact = validate_media_path(
        str(root_file),
        artifact_root,
        TASK_ID,
        1024,
    )

    assert nested_artifact.name == "renders/final.png"
    assert root_artifact.name == "final.png"
    assert nested_artifact.path == nested.resolve()
    assert root_artifact.path == root_file.resolve()
    assert nested_artifact.sha256 != root_artifact.sha256


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip("symbolic links are unavailable: %s" % exc)


def test_rejects_intermediate_and_final_symlink_components(tmp_path):
    artifact_root, task_root = make_task_root(tmp_path)
    real_dir = task_root / "real"
    real_dir.mkdir()
    real_file = real_dir / "frame.png"
    real_file.write_bytes(b"\x89PNG\r\n\x1a\ncontent")

    linked_dir = task_root / "linked"
    _symlink_or_skip(linked_dir, real_dir, directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        validate_media_path(
            str(linked_dir / "frame.png"),
            artifact_root,
            TASK_ID,
            1024,
        )

    linked_file = task_root / "linked.png"
    _symlink_or_skip(linked_file, real_file)
    with pytest.raises(ValueError, match="symbolic link"):
        validate_media_path(
            str(linked_file),
            artifact_root,
            TASK_ID,
            1024,
        )


def test_rejects_symlink_task_directory(tmp_path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    real_task = tmp_path / "real-task"
    real_task.mkdir()
    file_path = real_task / "frame.png"
    file_path.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    _symlink_or_skip(artifact_root / TASK_ID, real_task, directory=True)

    with pytest.raises(ValueError, match="task artifact directory"):
        validate_media_path(
            str(file_path),
            artifact_root,
            TASK_ID,
            1024,
        )


def test_mp4_requires_consistent_top_level_box_structure(tmp_path):
    artifact_root, task_root = make_task_root(tmp_path)
    video = task_root / "final.mp4"
    video.write_bytes(valid_mp4())
    artifact = validate_media_path(
        str(video),
        artifact_root,
        TASK_ID,
        4096,
    )
    assert artifact.mime_type == "video/mp4"

    forged = task_root / "forged.mp4"
    forged.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert actual_mime(forged, "video/mp4") == "application/octet-stream"
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(forged),
            artifact_root,
            TASK_ID,
            4096,
        )

    missing_moov = task_root / "missing-moov.mp4"
    missing_moov.write_bytes(
        mp4_box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + mp4_box(b"mdat", b"payload")
    )
    assert actual_mime(missing_moov, "video/mp4") == (
        "application/octet-stream"
    )

    invalid_size = task_root / "invalid-size.mp4"
    invalid_size.write_bytes(
        struct.pack(">I4s", 7, b"ftyp")
        + mp4_box(b"moov", b"x")
        + mp4_box(b"mdat", b"x")
    )
    assert actual_mime(invalid_size, "video/mp4") == (
        "application/octet-stream"
    )

    oversized_ftyp = task_root / "oversized-ftyp.mp4"
    oversized_ftyp.write_bytes(
        mp4_box(b"ftyp", b"isom\x00\x00\x02\x00" + b"isom" * 1100)
        + mp4_box(b"moov", b"x")
        + mp4_box(b"mdat", b"x")
    )
    assert actual_mime(oversized_ftyp, "video/mp4") == (
        "application/octet-stream"
    )


@pytest.mark.parametrize("extension", sorted(OOXML_CASES))
def test_ooxml_requires_real_package_entries_and_content_type(
    tmp_path,
    extension,
):
    artifact_root, task_root = make_task_root(tmp_path)
    valid = task_root / ("valid" + extension)
    write_ooxml(valid, extension)
    artifact = validate_media_path(
        str(valid),
        artifact_root,
        TASK_ID,
        1024 * 1024,
    )
    assert artifact.mime_type == OOXML_CASES[extension]["mime"]

    generic = task_root / ("generic" + extension)
    with zipfile.ZipFile(generic, "w") as archive:
        archive.writestr("payload.txt", "not an OOXML package")
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(generic),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )

    wrong_type = task_root / ("wrong-type" + extension)
    write_ooxml(
        wrong_type,
        extension,
        content_type="application/octet-stream",
    )
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(wrong_type),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )

    missing_main = task_root / ("missing-main" + extension)
    write_ooxml(missing_main, extension, include_main=False)
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(missing_main),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )


def test_json_is_fully_parsed_and_text_checks_the_entire_file(tmp_path):
    artifact_root, task_root = make_task_root(tmp_path)
    valid_json = task_root / "valid.json"
    valid_json.write_text('{"items": [1, 2, 3]}', encoding="utf-8")
    assert validate_media_path(
        str(valid_json),
        artifact_root,
        TASK_ID,
        1024 * 1024,
    ).mime_type == "application/json"

    trailing_json = task_root / "trailing.json"
    trailing_json.write_text('{"ok": true} not-json', encoding="utf-8")
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(trailing_json),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )

    late_binary = task_root / "late-binary.txt"
    late_binary.write_bytes(b"a" * 5000 + b"\xff")
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(late_binary),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )

    late_nul = task_root / "late-nul.md"
    late_nul.write_bytes(b"a" * 5000 + b"\x00tail")
    with pytest.raises(ValueError, match="MIME"):
        validate_media_path(
            str(late_nul),
            artifact_root,
            TASK_ID,
            1024 * 1024,
        )


def test_valid_empty_zip_is_still_recognized(tmp_path):
    artifact_root, task_root = make_task_root(tmp_path)
    archive_path = task_root / "empty.zip"
    with zipfile.ZipFile(archive_path, "w"):
        pass
    artifact = validate_media_path(
        str(archive_path),
        artifact_root,
        TASK_ID,
        1024,
    )
    assert artifact.mime_type == "application/zip"


def test_legacy_media_markers_are_removed_without_path_disclosure():
    output = "\n".join(
        (
            "正常摘要",
            "结果见 MEDIA:/var/lib/private/final.mp4",
            "> `ＭＥＤＩＡ：C:\\secret\\frame.png`",
            "```text",
            "M\u200bE\u200bD\u200bI\u200bA:/hidden/in-fence.zip",
            "```",
            "保留这行",
        )
    )

    cleaned, removed = strip_legacy_delivery_markers(output)

    assert removed is True
    normalized = unicodedata.normalize("NFKC", cleaned).lower()
    assert "media:" not in normalized
    for secret in ("private", "secret", "hidden", "final.mp4", "frame.png"):
        assert secret not in normalized
    assert "正常摘要" in cleaned
    assert "结果见" in cleaned
    assert "保留这行" in cleaned
    assert cleaned.count("```") == 2


def test_marker_free_output_is_unchanged():
    value = "普通文本\n没有旧式交付协议"
    assert strip_legacy_delivery_markers(value) == (value, False)


def signer_metadata() -> dict[str, object]:
    return {
        "artifact_id": "A-1234567890ABCDEF",
        "sha256": "a" * 64,
        "size_bytes": 12345,
        "mime_type": "video/mp4",
        "version_token": "generation-2:artifact-7",
    }


def test_artifact_signer_v2_binds_every_artifact_field():
    signer = ArtifactSigner("top-secret", "http://127.0.0.1:8000")
    expires = int(time.time()) + 600
    metadata = signer_metadata()
    signature = signer.signature(
        TASK_ID,
        "renders/final.mp4",
        expires,
        **metadata,
    )
    assert signer.verify(
        TASK_ID,
        "renders/final.mp4",
        expires,
        signature,
        **metadata,
    )

    mutations = (
        (TASK_ID + "X", "renders/final.mp4", expires, metadata),
        (TASK_ID, "renders/other.mp4", expires, metadata),
        (TASK_ID, "renders/final.mp4", expires + 1, metadata),
        (
            TASK_ID,
            "renders/final.mp4",
            expires,
            {**metadata, "artifact_id": "A-FEDCBA0987654321"},
        ),
        (
            TASK_ID,
            "renders/final.mp4",
            expires,
            {**metadata, "sha256": "b" * 64},
        ),
        (
            TASK_ID,
            "renders/final.mp4",
            expires,
            {**metadata, "size_bytes": 12346},
        ),
        (
            TASK_ID,
            "renders/final.mp4",
            expires,
            {**metadata, "mime_type": "application/octet-stream"},
        ),
        (
            TASK_ID,
            "renders/final.mp4",
            expires,
            {**metadata, "version_token": "generation-3:artifact-7"},
        ),
    )
    for task_id, name, changed_expiry, changed_metadata in mutations:
        assert not signer.verify(
            task_id,
            name,
            changed_expiry,
            signature,
            **changed_metadata,
        )


def test_artifact_signer_url_carries_v2_binding_and_nested_name():
    signer = ArtifactSigner("top-secret", "http://127.0.0.1:8000")
    expires = int(time.time()) + 600
    metadata = signer_metadata()
    url = signer.url(
        TASK_ID,
        "renders/final.mp4",
        expires,
        **metadata,
    )
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.path.endswith("/renders%2Ffinal.mp4")
    for key, value in metadata.items():
        assert query[key] == [str(value)]
    assert query["expires"] == [str(expires)]
    assert signer.verify(
        TASK_ID,
        "renders/final.mp4",
        expires,
        query["signature"][0],
        **metadata,
    )


def test_artifact_signer_keeps_legacy_calls_during_migration():
    signer = ArtifactSigner("top-secret", "http://127.0.0.1:8000")
    expires = int(time.time()) + 600
    legacy = signer.signature(TASK_ID, "final.png", expires)
    assert signer.verify(TASK_ID, "final.png", expires, legacy)

    metadata = signer_metadata()
    v2 = signer.signature(TASK_ID, "final.png", expires, **metadata)
    assert not signer.verify(TASK_ID, "final.png", expires, v2)
    assert not signer.verify(
        TASK_ID,
        "final.png",
        expires,
        legacy,
        **metadata,
    )
    with pytest.raises(ValueError, match="complete artifact metadata"):
        signer.signature(
            TASK_ID,
            "final.png",
            expires,
            artifact_id="A-INCOMPLETE",
        )


def test_artifact_signer_can_bind_an_opaque_version_token_only():
    signer = ArtifactSigner("top-secret", "http://127.0.0.1:8000")
    expires = int(time.time()) + 600
    signature = signer.signature(
        TASK_ID,
        "nested/final.zip",
        expires,
        version_token="artifact-version:42",
    )
    assert signer.verify(
        TASK_ID,
        "nested/final.zip",
        expires,
        signature,
        version_token="artifact-version:42",
    )
    assert not signer.verify(
        TASK_ID,
        "nested/final.zip",
        expires,
        signature,
        version_token="artifact-version:43",
    )
    assert not signer.verify(TASK_ID, "nested/final.zip", "invalid", signature)
