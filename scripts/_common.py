"""Helpers shared by every cage-kernel platform."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

UNIT_ROOT = Path(__file__).resolve().parents[1]
#: Every platform's managed work lives under this unit's git-ignored `.local/`.
LOCAL_ROOT = UNIT_ROOT / ".local"


def repo_path(path: Path) -> str:
    try:
        return path.relative_to(UNIT_ROOT).as_posix()
    except ValueError:
        return str(path)


def run(
    argv: list[str],
    *,
    cwd: Path = UNIT_ROOT,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    display = " ".join(argv)
    print(f"+ {display}", flush=True)
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=capture,
        check=True,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, object]:
    """The digest and size a release records for one file."""
    return {"sha256": file_sha256(path), "size": path.stat().st_size}


def zstd_compress(source: Path, destination: Path) -> None:
    if shutil.which("zstd") is None:
        raise SystemExit("`zstd` CLI is required to package cage-kernel release assets")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["zstd", "-19", "--force", "--quiet", "-o", str(destination), str(source)])


def ensure_under_local(path: Path) -> Path:
    """Refuse to manage *path* unless it is inside this unit's `.local/`."""
    resolved = path.resolve(strict=False)
    allowed_root = LOCAL_ROOT.resolve(strict=False)
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise SystemExit(f"refusing to manage a directory outside .local/: {repo_path(resolved)}")
    return resolved
