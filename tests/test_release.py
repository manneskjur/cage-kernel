from __future__ import annotations

import argparse
import io
import tarfile
import json
import shutil
from pathlib import Path

import _common
import _macos
import _release_sources
import _windows
import cage_kernel
import pytest
from test_macos import synthetic_kernel
from test_windows import local, paths  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd CLI is not installed")


def macos_artifacts(root: Path) -> Path:
    artifacts = root / "macos" / "kernels"
    sources = artifacts / "sources"
    sources.mkdir(parents=True)
    linux = sources / "fixture.tar"

    def archive_files(path, names):
        with tarfile.open(path, "w") as tar:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 7
                tar.addfile(info, io.BytesIO(b"fixture"))

    archive_files(
        linux,
        [
            "linux/COPYING",
            "linux/LICENSES/preferred/GPL-2.0",
            "linux/LICENSES/exceptions/Linux-syscall-note",
        ],
    )
    digest = _common.file_sha256(linux)
    linux.rename(sources / digest)
    for name in _macos.PROFILE_ORDER:
        kernel = artifacts / name / "vmlinux"
        kernel.parent.mkdir(parents=True)
        kernel.write_bytes(synthetic_kernel(_macos.KERNEL_PROFILES[name]))
        archive_files(
            kernel.parent / "apple-build.tar",
            ["LICENSE", "kernel/build.sh", "kernel/Makefile", "kernel/image/Dockerfile"],
        )
        _macos.write_build_provenance(
            kernel,
            containerization_url="https://example.invalid/containerization.git",
            containerization_revision="0" * 40,
            kernel_source_sha256=digest,
            kernel_source_url="https://example.invalid/linux.tar.xz",
        )
    return artifacts


def package(root: Path, tmp_path: Path) -> Path:
    windows = paths(root)
    _windows.create(windows)
    release = tmp_path / "release"
    args = argparse.Namespace(
        macos_artifact_dir=macos_artifacts(tmp_path),
        windows_artifact_dir=windows.artifact_dir,
        release_dir=release,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            _release_sources,
            "_azure",
            lambda tar, payload: tar.add(payload / "kernel.config", "azure-linux/kernel.config"),
        )
        assert cage_kernel.package_release(args) == 0
    return release


def test_a_release_holds_every_platform_and_nothing_else(local: Path, tmp_path: Path) -> None:  # noqa: F811
    release = package(local, tmp_path)
    assert sorted(path.name for path in release.iterdir()) == sorted(
        cage_kernel.release_asset_names()
    )
    assert all(
        name.startswith(("macos-arm64-", "windows-amd64-"))
        for name in cage_kernel.release_asset_names()
        if name
        not in {_release_sources.ASSET, cage_kernel.MANIFEST_ASSET, cage_kernel.SHA256SUMS_ASSET}
    )


def test_the_manifest_names_each_platform_and_its_asset_digests(
    local: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    release = package(local, tmp_path)
    manifest = json.loads((release / cage_kernel.MANIFEST_ASSET).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["source_archive"]["sha256"] == _common.file_sha256(
        release / _release_sources.ASSET
    )
    assert set(manifest["platforms"]) == {"macos", "windows"}
    macos = manifest["platforms"]["macos"]
    assert macos["architecture"] == "arm64"
    assert macos["containerization"]["revision"] == "0" * 40
    for name, profile in macos["profiles"].items():
        assert profile["kernel"]["sha256"] == _common.file_sha256(
            release / profile["kernel"]["name"]
        )
        assert profile["content"]["sha256"] == _common.file_sha256(
            tmp_path / "macos" / "kernels" / name / "vmlinux"
        )
    windows = manifest["platforms"]["windows"]
    assert windows["architecture"] == "amd64"
    assert windows["source"] == _windows.load_pins()["kernel"]


def test_sha256sums_covers_every_other_asset(local: Path, tmp_path: Path) -> None:  # noqa: F811
    release = package(local, tmp_path)
    lines = (release / cage_kernel.SHA256SUMS_ASSET).read_text(encoding="utf-8").splitlines()
    listed = dict(reversed(line.split("  ", 1)) for line in lines)
    assert set(listed) == set(cage_kernel.release_asset_names()) - {cage_kernel.SHA256SUMS_ASSET}
    assert all(listed[name] == _common.file_sha256(release / name) for name in listed)


def test_profiles_built_from_different_sources_are_not_released(
    local: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    artifacts = macos_artifacts(tmp_path)
    _macos.write_build_provenance(
        artifacts / "nbd" / "vmlinux",
        containerization_url="https://example.invalid/containerization.git",
        containerization_revision="1" * 40,
        kernel_source_sha256="1" * 64,
        kernel_source_url="https://example.invalid/linux.tar.xz",
    )
    with pytest.raises(SystemExit, match="rebuild every profile together"):
        _macos.package_release(artifacts, tmp_path / "out")


def test_a_kernel_without_build_provenance_is_not_released(tmp_path: Path) -> None:
    artifacts = macos_artifacts(tmp_path)
    (artifacts / "hotplug" / _macos.BUILD_PROVENANCE_NAME).unlink()
    with pytest.raises(SystemExit, match="macos create"):
        _macos.package_release(artifacts, tmp_path / "out")


def test_missing_linux_source_blocks_packaging(tmp_path: Path) -> None:
    artifacts = macos_artifacts(tmp_path)
    for file in (artifacts / "sources").iterdir():
        file.unlink()
    with (
        tarfile.open(tmp_path / "sources.tar", "w") as tar,
        pytest.raises(ValueError, match="Linux source missing"),
    ):
        _release_sources._linux(tar, artifacts)
