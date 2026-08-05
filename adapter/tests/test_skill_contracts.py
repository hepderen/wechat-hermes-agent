import re
import unicodedata
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOTS = (
    ROOT / "skills" / "wechat-group-operations",
    ROOT / "skills" / "douyin-video-production",
)


def test_wechat_skill_pins_async_media_contract():
    skill_root = ROOT / "skills" / "wechat-group-operations"
    skill_path = skill_root / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["metadata"]["hermes"]["version"] == "1.2.0"
    assert "wechat_register_artifact" in text
    assert "sync chat" in text.lower()
    assert "MEDIA:" not in text
    assert "wechat_send_text" not in text
    assert (skill_root / "references" / "media-delivery.md").is_file()


def test_wechat_skill_remains_cloud_only():
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL_ROOTS[0].rglob("*")
        if path.is_file()
    ).lower()

    for forbidden in ("jianying", "鍓槧", "workstation", "powershell", "18766"):
        assert forbidden not in content


def test_custom_skill_trees_forbid_legacy_media_and_direct_wechat_send():
    format_chars = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
    direct_send_patterns = (
        re.compile(
            r"\bwechat_send_(?:text|image|video|file|media|message)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b(?:127\.0\.0\.1|localhost):8765\b", re.IGNORECASE),
        re.compile(r"/(?:api/)?send(?:/|\b)", re.IGNORECASE),
        re.compile(
            r"\b(?:curl|wget)\b[^\n]*(?:wechat|8765|/send)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:chat_api|wechat_client)\.send_?"
            r"(?:text|image|video|file|media|message)\b",
            re.IGNORECASE,
        ),
    )
    legacy_marker = re.compile(
        r"(?<![A-Za-z0-9_])"
        r"m[" + format_chars + r"]*"
        r"e[" + format_chars + r"]*"
        r"d[" + format_chars + r"]*"
        r"i[" + format_chars + r"]*"
        r"a[" + format_chars + r"]*\s*:",
        re.IGNORECASE,
    )

    scanned = []
    for skill_root in SKILL_ROOTS:
        for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
            text = unicodedata.normalize(
                "NFKC",
                path.read_text(encoding="utf-8"),
            )
            scanned.append(path)
            assert legacy_marker.search(text) is None, path
            for pattern in direct_send_patterns:
                assert pattern.search(text) is None, path

    assert any(path.parent.name == "references" for path in scanned)
    assert {path.name for path in SKILL_ROOTS} == {
        "douyin-video-production",
        "wechat-group-operations",
    }
