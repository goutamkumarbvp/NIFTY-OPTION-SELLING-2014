#!/usr/bin/env python3
"""Build the distributable zip from the current git tree:  python scripts/build_release.py [out_dir]

Includes the terminal package, scripts, docs, tests, config templates and a
QUICK_START.txt; excludes runtime data, caches, the legacy archive and .env.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "NIFTY_AI_OPTIONS_TERMINAL_V30"


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    tar_bytes = subprocess.check_output(["git", "archive", "HEAD", f"--prefix={NAME}/"], cwd=ROOT)
    quick = (ROOT / "docs" / "QUICK_START.txt").read_text(encoding="utf-8").replace("{BUILD}", sha) if (ROOT / "docs" / "QUICK_START.txt").exists() else ""
    zpath = out_dir / f"{NAME}.zip"
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar, zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            rel = m.name[len(NAME) + 1:]
            if rel.startswith(("legacy/", "runtime/")) or rel.endswith(".pyc") or "__pycache__" in rel or rel == ".env":
                continue
            z.writestr(m.name, tar.extractfile(m).read())
        if quick:
            z.writestr(f"{NAME}/QUICK_START.txt", quick)
    print(f"wrote {zpath} ({zpath.stat().st_size // 1024} KB) from {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
