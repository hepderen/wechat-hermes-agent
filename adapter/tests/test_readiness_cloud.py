from pathlib import Path

import pytest

from scripts import readiness_cloud
from app.media import MediaArtifact


def test_mcp_import_uses_project_root_and_restores_accessible_cwd(
    monkeypatch,
    tmp_path,
):
    imported_from = []
    marker = object()

    def fake_import(name):
        imported_from.append((name, Path.cwd()))
        return marker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(readiness_cloud.importlib, "import_module", fake_import)

    result = readiness_cloud.load_mcp_server()

    assert result is marker
    assert imported_from == [("mcp_server", readiness_cloud.PROJECT_ROOT)]
    assert Path.cwd() == tmp_path


def test_mcp_import_keeps_accessible_project_cwd_when_original_is_restricted(
    monkeypatch,
    tmp_path,
):
    imported_from = []
    marker = object()

    def fake_import(name):
        imported_from.append((name, Path.cwd()))
        return marker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(readiness_cloud.os, "access", lambda *_args: False)
    monkeypatch.setattr(readiness_cloud.importlib, "import_module", fake_import)

    result = readiness_cloud.load_mcp_server()

    assert result is marker
    assert imported_from == [("mcp_server", readiness_cloud.PROJECT_ROOT)]
    assert Path.cwd() == readiness_cloud.PROJECT_ROOT


def test_readiness_compares_structured_artifact_metadata(tmp_path):
    artifact_path = tmp_path / "result.mp4"
    expected = MediaArtifact(
        artifact_path,
        artifact_path.name,
        "video/mp4",
        123,
        "a" * 64,
    )
    actual = MediaArtifact(
        artifact_path,
        artifact_path.name,
        "video/mp4",
        123,
        "a" * 64,
    )

    readiness_cloud.require_same_artifact(expected, actual)

    mismatched = MediaArtifact(
        artifact_path,
        artifact_path.name,
        "video/mp4",
        124,
        "a" * 64,
    )
    with pytest.raises(RuntimeError, match="different size"):
        readiness_cloud.require_same_artifact(expected, mismatched)
