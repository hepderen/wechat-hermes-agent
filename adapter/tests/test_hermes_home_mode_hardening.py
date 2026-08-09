from pathlib import Path

import pytest

from deploy.harden_hermes_home_mode import NEW, OLD, RELATIVE_PATH, harden


def write_fixture(root: Path, body: str) -> Path:
    path = root / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import os\n\ndef secure_parent_dir(parent):\n" + body,
        encoding="utf-8",
    )
    return path


def test_hardener_makes_parent_security_honor_explicit_home_mode(tmp_path):
    path = write_fixture(tmp_path, OLD)

    assert harden(tmp_path) == path
    source = path.read_text(encoding="utf-8")
    assert OLD not in source
    assert NEW in source
    assert 'os.environ.get("HERMES_HOME_MODE", "")' in source
    assert "mode = int(mode_text, 8) if mode_text else 0o700" in source


def test_hardener_is_idempotent(tmp_path):
    path = write_fixture(tmp_path, OLD)

    harden(tmp_path)
    first = path.read_bytes()
    harden(tmp_path)

    assert path.read_bytes() == first


def test_hardener_rejects_unknown_vendor_source(tmp_path):
    write_fixture(tmp_path, "    pass\n")

    with pytest.raises(RuntimeError, match="unexpected Hermes source"):
        harden(tmp_path)
