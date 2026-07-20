#!/usr/bin/env python3
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md",
    "monitoring.py",
    "tools/codex_telemetry.py",
    "scripts/install.sh",
    "scripts/install-apple-container.sh",
)


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", sys.argv[1]):
        raise SystemExit("usage: build-release.py X.Y.Z")
    version = sys.argv[1]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / f"tools-{version}.tar.gz"
    prefix = f"tools-{version}"

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative in sorted(FILES):
            path = ROOT / relative
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{relative}")
            info.size = len(data)
            info.mode = 0o755 if relative.endswith((".sh", ".py")) else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))

    with output.open("wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as zipped:
            zipped.write(raw.getvalue())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (dist / "SHA256SUMS").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(output)
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
