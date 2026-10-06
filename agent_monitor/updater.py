from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import urllib.request

from pathlib import Path
from typing import Callable


ReleaseRequester = Callable[[str, str], bytes]


def semantic_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise ValueError(f"invalid release version: {value}")
    return tuple(map(int, match.groups()))


def install_prefix(source_file: str) -> Path:
    source = Path(source_file).resolve()
    if source.parent.name == "nutthaphon-tools" and source.parent.parent.name == "lib":
        return source.parents[2]
    return Path.home() / ".local"


def release_request(url: str, accept: str, version: str) -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"agent-monitor/{version}",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def latest_release_assets(
    repository: str, version: str, requester: ReleaseRequester,
) -> tuple[str, dict[str, str]]:
    api = f"https://api.github.com/repos/{repository}/releases/latest"
    payload = json.loads(requester(api, "application/vnd.github+json"))
    tag = str(payload.get("tag_name") or "") if isinstance(payload, dict) else ""
    semantic_version(tag)
    assets = {
        str(item.get("name")): str(item.get("browser_download_url"))
        for item in payload.get("assets", [])
        if isinstance(item, dict) and item.get("name") and item.get("browser_download_url")
    }
    return tag.removeprefix("v"), assets


def safe_extract_release(archive_path: Path, destination: Path, version: str) -> Path:
    root = f"tools-{version}"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if (
                not parts or parts[0] != root or ".." in parts
                or Path(member.name).is_absolute() or member.issym() or member.islnk()
                or member.isdev()
            ):
                raise ValueError(f"unsafe release archive member: {member.name}")
        try:
            archive.extractall(destination, members=members, filter="data")
        except TypeError:
            archive.extractall(destination, members=members)
    installer = destination / root / "scripts" / "install.sh"
    if not installer.is_file():
        raise ValueError("release archive does not contain scripts/install.sh")
    return installer


def update(
    current_version: str,
    latest_loader: Callable[[], tuple[str, dict[str, str]]],
    requester: ReleaseRequester,
    *,
    force: bool = False,
    prefix: Path,
    runner: Callable[..., object] = subprocess.run,
) -> str | None:
    latest, assets = latest_loader()
    if not force and semantic_version(latest) <= semantic_version(current_version):
        return None

    archive_name = f"tools-{latest}.tar.gz"
    missing = [name for name in (archive_name, "SHA256SUMS") if name not in assets]
    if missing:
        raise ValueError(f"release v{latest} is missing: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="agent-monitor-update-") as temp_value:
        temp_dir = Path(temp_value)
        archive_path = temp_dir / archive_name
        archive_path.write_bytes(requester(assets[archive_name], "application/octet-stream"))
        checksum_text = requester(assets["SHA256SUMS"], "application/octet-stream").decode("utf-8")
        expected = next((
            match.group(1).lower()
            for line in checksum_text.splitlines()
            if (match := re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?" + re.escape(archive_name), line.strip()))
        ), "")
        actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if not expected or actual != expected:
            raise ValueError(f"SHA-256 verification failed for {archive_name}")
        installer = safe_extract_release(archive_path, temp_dir, latest)
        runner([str(installer), "--prefix", str(prefix)], check=True)
    return latest
