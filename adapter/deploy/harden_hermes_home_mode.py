from __future__ import annotations

import argparse
from pathlib import Path


RELATIVE_PATH = "hermes_constants.py"
OLD = """    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
"""
NEW = """    try:
        mode_text = os.environ.get("HERMES_HOME_MODE", "").strip()
        mode = int(mode_text, 8) if mode_text else 0o700
    except ValueError:
        mode = 0o700
    try:
        os.chmod(parent, mode)
    except OSError:
        pass
"""


def harden(root: Path, *, compile_file: bool = True) -> Path:
    path = root / RELATIVE_PATH
    text = path.read_text(encoding="utf-8")
    old_count = text.count(OLD)
    new_count = text.count(NEW)
    if old_count == 1 and new_count == 0:
        path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    elif old_count == 0 and new_count == 1:
        pass
    else:
        raise RuntimeError(
            f"unexpected Hermes source at {path}: "
            f"old={old_count}, new={new_count}"
        )
    if compile_file:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Make Hermes parent-directory hardening honor HERMES_HOME_MODE."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/opt/hermes-runtime"),
    )
    args = parser.parse_args()
    print(harden(args.root.resolve(strict=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
