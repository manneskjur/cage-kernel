"""Helpers shared by every cage-kernel platform."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path

UNIT_ROOT = Path(__file__).resolve().parents[1]
#: Every platform's managed work lives under this unit's git-ignored `.local/`.
LOCAL_ROOT = UNIT_ROOT / ".local"


def repo_path(path: Path) -> str:
    try:
        return path.relative_to(UNIT_ROOT).as_posix()
    except ValueError:
        return str(path)


def tracked_files(directory: Path) -> frozenset[Path]:
    """Every file Git tracks under *directory*.

    A release's source archive carries what was committed, never whatever else
    sits beside it: ignored caches, editor files or local notes pass a clean
    `git status` but must not be published. A tree Git cannot list is refused,
    never walked instead.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--", "."],
            cwd=directory,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode("utf-8", "replace").strip()
            if isinstance(exc, subprocess.CalledProcessError)
            else str(exc)
        )
        raise SystemExit(
            f"cannot list the files Git tracks under {repo_path(directory)}; a release is "
            f"packaged only from a Git checkout: {detail}"
        ) from exc
    files = frozenset(
        directory / name for name in listing.stdout.decode("utf-8").split("\0") if name
    )
    if missing := sorted(repo_path(path) for path in files if not path.is_file()):
        raise SystemExit("tracked files are missing from the checkout: " + ", ".join(missing))
    return files


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


def add_source(archive: tarfile.TarFile, path: Path, name: str) -> None:
    """Archive build inputs without publishing the builder's local account or timestamps."""

    def public_metadata(entry: tarfile.TarInfo) -> tarfile.TarInfo:
        return entry.replace(uid=0, gid=0, uname="", gname="", mtime=0, deep=False)

    archive.add(path, name, recursive=False, filter=public_metadata)
