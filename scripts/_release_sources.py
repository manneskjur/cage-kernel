"""Collect the inputs of the published kernels at release packaging time."""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from pathlib import Path

import _lcow_rpm
import _macos
import _windows
from _common import UNIT_ROOT, add_source, file_sha256, repo_path, tracked_files

ASSET = "source-and-notices.tar"


def _text(archive: tarfile.TarFile, name: str, contents: bytes) -> None:
    if not contents.strip():
        raise ValueError(f"Source archive input is empty: {name}")
    entry = tarfile.TarInfo(name)
    entry.size = len(contents)
    archive.addfile(entry, io.BytesIO(contents))


def _linux(archive: tarfile.TarFile, kernels: Path) -> None:
    first = kernels / _macos.PROFILE_ORDER[0] / "vmlinux"
    record = _macos.read_build_provenance(first)
    digest = record.get("kernel_source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Linux source digest missing from the kernel build record")
    source = kernels / "sources" / digest
    if not source.is_file() or file_sha256(source) != digest:
        raise ValueError("Linux source missing or changed since the kernel build")
    add_source(archive, source, "linux/source.tar.xz")
    required = {"COPYING", "LICENSES/preferred/GPL-2.0", "LICENSES/exceptions/Linux-syscall-note"}
    with tarfile.open(source, "r|*") as upstream:
        for entry in upstream:
            name = entry.name.partition("/")[2]
            if name in required and entry.isfile() and entry.size:
                _text(archive, "linux/" + name, upstream.extractfile(entry).read())
                required.remove(name)
                if not required:
                    break
    if required:
        raise ValueError("Linux source lacks " + ", ".join(sorted(required)))
    for profile in _macos.PROFILE_ORDER:
        root = kernels / profile
        config = _macos.read_kernel_config(root / "vmlinux")
        if config is None:
            raise ValueError(f"{profile} kernel has no embedded configuration")
        _text(archive, f"configs/{profile}.config", config.encode())
        recipe = root / "apple-build.tar"
        build = _macos.read_build_provenance(root / "vmlinux")
        if not recipe.is_file() or file_sha256(recipe) != build.get("apple_build_sha256"):
            raise ValueError(f"Apple build sources missing or changed for {profile}")
        with tarfile.open(recipe) as files:
            for name in (
                "LICENSE",
                "kernel/build.sh",
                "kernel/Makefile",
                "kernel/image/Dockerfile",
            ):
                if name not in files.getnames() or not files.getmember(name).size:
                    raise ValueError(f"Apple build sources for {profile} lack {name}")
        add_source(archive, recipe, f"containerization/{profile}.tar")


def _azure(archive: tarfile.TarFile, payload: Path) -> None:
    pins = _windows.load_pins()
    spec = pins["kernel"]
    sources = pins["sources"]
    cache = payload.parent / "release-sources"
    rpm = _windows._download(
        spec["url"], spec["sha256"], payload.parent / "downloads" / Path(spec["url"]).name
    )
    rpm_data = rpm.read_bytes()
    _, offset = _lcow_rpm._read_rpm_header(rpm_data, 96, align_after=True)
    header, _ = _lcow_rpm._read_rpm_header(rpm_data, offset, align_after=False)
    nevr = f"{spec['version']}-{spec['release']}"
    if header.get(1044) != f"kernel-signed-{spec['arch']}-{nevr}.src.rpm":
        raise ValueError("Azure Linux kernel does not match its signed packaging recipe")
    source_name = f"kernel-{nevr}.src.rpm"
    srpm = _windows._download(sources["rpm_url"], sources["rpm_sha256"], cache / source_name)
    members = subprocess.check_output(["bsdtar", "tf", str(srpm)], encoding="utf-8").splitlines()
    if f"kernel-{spec['version']}.tar.gz" not in members:
        raise ValueError(f"Azure Linux source RPM lacks the kernel source for {nevr}")
    add_source(archive, srpm, "azure-linux/" + source_name)
    add_source(archive, payload / _windows.CONFIG_FILE, "azure-linux/kernel.config")
    # The signed RPM repackages the kernel built by the unsigned source RPM.
    # Match the packaging revision to the actual source RPM's build script.
    base = (
        f"https://raw.githubusercontent.com/microsoft/azurelinux/{sources['packaging_revision']}/"
    )
    spec_bytes = subprocess.check_output(["bsdtar", "xOf", str(srpm), "kernel.spec"])
    if _lcow_rpm._download_bytes(base + "SPECS/kernel/kernel.spec") != spec_bytes:
        raise ValueError("Azure Linux source RPM differs from its packaging revision")
    _text(archive, "azure-linux/LICENSE", _lcow_rpm._download_bytes(base + "LICENSE"))
    for name in ("kernel-signed.spec", "sha512hmac-openssl.sh"):
        data = _lcow_rpm._download_bytes(base + "SPECS-SIGNED/kernel-signed/" + name)
        if name.endswith(".spec") and not re.search(
            rb"^Version:\s*" + re.escape(spec["version"].encode()) + rb"\s*$", data, re.M
        ):
            raise ValueError("Azure Linux signing spec has a different kernel version")
        _text(archive, "azure-linux/" + name, data)


def package(destination: Path, kernels: Path, windows: Path) -> None:
    with tarfile.open(destination, "w") as archive:
        try:
            _linux(archive, kernels)
        except (OSError, tarfile.TarError) as exc:
            raise ValueError(f"Linux or Apple kernel source unavailable: {exc}") from exc
        try:
            _azure(archive, windows)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            raise ValueError(f"Azure Linux source or notices unavailable: {exc}") from exc
        # Only committed files: an ignored or untracked file beside them is
        # never published, and a named document that is not committed fails.
        tracked = tracked_files(UNIT_ROOT)
        for folder, suffix in (
            ("scripts", ".py"),
            ("macos/patches", ".patch"),
            ("windows", ".toml"),
        ):
            for path in sorted(
                path
                for path in tracked
                if path.parent == UNIT_ROOT / folder and path.suffix == suffix
            ):
                add_source(archive, path, "cage-kernel/" + path.relative_to(UNIT_ROOT).as_posix())
        public = UNIT_ROOT / "public" if (UNIT_ROOT / "public").is_dir() else UNIT_ROOT
        for path, name in (
            (UNIT_ROOT / "VERSION", "VERSION"),
            (UNIT_ROOT / "LICENSE", "LICENSE"),
            (public / "README.md", "README.md"),
        ):
            if path not in tracked:
                raise ValueError(f"{repo_path(path)} is not committed")
            add_source(archive, path, f"cage-kernel/{name}")
