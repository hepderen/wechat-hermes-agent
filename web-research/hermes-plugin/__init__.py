"""Hermes web provider registration."""

import sys
from pathlib import Path


_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from .provider import WechatCloudWebProvider


def register(ctx):
    ctx.register_web_search_provider(WechatCloudWebProvider())
