"""Read Microsoft Azure Linux kernel RPMs and package their LCOW guest kernel.

`_windows.py` drives this module from the pins in `windows/lcow-kernel.toml`.
It extracts `/boot/vmlinuz-*` and its configuration, and the kernel modules
the LCOW guest loads (with their `modules.dep` closure), from the RPM payload.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import re
import struct
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen

import zstandard

DEFAULT_REPO_URL = "https://packages.microsoft.com/azurelinux/3.0/prod/base/x86_64/"
DEFAULT_PACKAGE_NAME = "kernel"
DEFAULT_ARCH = "x86_64"
DEFAULT_CONFIG_REQUIREMENTS = ("CONFIG_VSOCKETS=y", "CONFIG_HYPERV_VSOCKETS=y")
DEFAULT_INITRD_MODULE_PATTERNS = (
    r"lib/modules/[^/]+/kernel/fs/9p/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/fs/netfs/netfs\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/net/9p/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/net/netfilter/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/net/ipv4/netfilter/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/net/ipv6/netfilter/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/net/bridge/netfilter/[^/]+\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/fs/smb/client/cifs\.ko(?:\..+)?",
    r"lib/modules/[^/]+/kernel/crypto/cmac\.ko(?:\..+)?",
    r"lib/modules/[^/]+/modules\..+",
)
EXPERIMENTAL_CONFIG_WARNINGS = {
    "CONFIG_HYPERV_VSM=y": (
        "Azure Linux kernel-mshv enables Hyper-V VSM. Standard LCOW utility VMs "
        "are not VSM/VTL guests; Cage currently blacklists vsm_init at boot when "
        "using this kernel."
    )
}
MANIFEST_NAME = "lcow-kernel-manifest.json"
CONFIG_COPY_NAME = "kernel.config"
KERNEL_OUTPUT_NAME = "kernel"
MODULES_ARCHIVE_NAME = "kernel-modules.cpio.gz"

_RPM_LEAD_LEN = 96
_RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
_RPM_HEADER_MAGIC = b"\x8e\xad\xe8\x01"
_RPM_INDEX_LEN = 16
_RPM_TYPE_STRING = 6
_RPM_TAG_PAYLOAD_FORMAT = 1124
_RPM_TAG_PAYLOAD_COMPRESSOR = 1125
_CPIO_NEWC_HEADER_LEN = 110
_CPIO_TRAILER = "TRAILER!!!"


@dataclass(frozen=True, slots=True)
class KernelPackage:
    name: str
    version: str
    release: str
    arch: str
    location: str
    checksum: str | None
    build_time: int

    @property
    def rpm_name(self) -> str:
        return Path(self.location).name


@dataclass(frozen=True, slots=True)
class BootKernelFiles:
    kernel_name: str
    kernel: bytes
    config_name: str | None
    config: bytes | None
    payload_format: str | None
    payload_compressor: str | None


@dataclass(frozen=True, slots=True)
class RpmPayload:
    entries: dict[str, bytes]
    payload_format: str | None
    payload_compressor: str | None


def _align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def _download_bytes(url: str) -> bytes:
    with urlopen(url, timeout=300) as response:
        return response.read()


def _download_to_file(url: str, dest: Path, *, checksum: str | None) -> Path:
    if dest.is_file() and _matches_checksum(dest, checksum):
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=dest.parent, delete=False)  # noqa: SIM115
    tmp_path = Path(tmp.name)
    try:
        try:
            with urlopen(url, timeout=300) as response:
                while chunk := response.read(1024 * 1024):
                    tmp.write(chunk)
        finally:
            tmp.close()
        _verify_checksum(tmp_path, checksum)
        tmp_path.replace(dest)
    finally:
        tmp_path.unlink(missing_ok=True)
    return dest


def _matches_checksum(path: Path, checksum: str | None) -> bool:
    if checksum is None:
        return path.is_file()
    try:
        _verify_checksum(path, checksum)
    except RuntimeError:
        return False
    return True


def _verify_checksum(path: Path, checksum: str | None) -> None:
    if checksum is None:
        return
    if not checksum.startswith("sha256:"):
        raise RuntimeError(f"unsupported package checksum {checksum!r}")
    expected = checksum.removeprefix("sha256:")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{path.name} sha256 mismatch: expected {expected}, got {actual}")


def _repo_url(repo_url: str, href: str) -> str:
    return urljoin(repo_url.rstrip("/") + "/", href)


def _primary_metadata_url(repo_url: str) -> str:
    root = ET.fromstring(_download_bytes(_repo_url(repo_url, "repodata/repomd.xml")))
    ns = {"repo": "http://linux.duke.edu/metadata/repo"}
    for data in root.findall("repo:data", ns):
        if data.attrib.get("type") != "primary":
            continue
        location = data.find("repo:location", ns)
        href = location.attrib.get("href") if location is not None else None
        if href:
            return _repo_url(repo_url, href)
    raise RuntimeError(f"repository metadata at {repo_url!r} has no primary metadata")


def _text(element: ET.Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _select_kernel_package_from_primary_xml(
    primary_xml_gz: bytes,
    *,
    package_name: str,
    arch: str,
) -> KernelPackage:
    root = ET.fromstring(gzip.decompress(primary_xml_gz))
    ns = {"common": "http://linux.duke.edu/metadata/common"}
    candidates: list[KernelPackage] = []
    for package in root.findall("common:package", ns):
        name = _text(package.find("common:name", ns))
        package_arch = _text(package.find("common:arch", ns))
        if name != package_name or package_arch != arch:
            continue
        version = package.find("common:version", ns)
        location = package.find("common:location", ns)
        checksum = package.find("common:checksum", ns)
        time = package.find("common:time", ns)
        if version is None or location is None:
            continue
        href = location.attrib.get("href")
        ver = version.attrib.get("ver")
        rel = version.attrib.get("rel")
        if not href or not ver or not rel:
            continue
        checksum_text = _text(checksum)
        checksum_type = checksum.attrib.get("type") if checksum is not None else None
        checksum_value = (
            f"{checksum_type}:{checksum_text}" if checksum_text and checksum_type else None
        )
        candidates.append(
            KernelPackage(
                name=name,
                version=ver,
                release=rel,
                arch=package_arch,
                location=href,
                checksum=checksum_value,
                build_time=int((time.attrib.get("build") if time is not None else None) or 0),
            )
        )
    if not candidates:
        raise RuntimeError(f"no {package_name!r} package for {arch!r} in primary metadata")
    return max(candidates, key=lambda item: (item.build_time, item.version, item.release))


def resolve_latest_kernel_package(
    *,
    repo_url: str,
    package_name: str = DEFAULT_PACKAGE_NAME,
    arch: str = DEFAULT_ARCH,
) -> KernelPackage:
    return _select_kernel_package_from_primary_xml(
        _download_bytes(_primary_metadata_url(repo_url)),
        package_name=package_name,
        arch=arch,
    )


def _read_rpm_header(data: bytes, offset: int, *, align_after: bool) -> tuple[dict[int, str], int]:
    if data[offset : offset + len(_RPM_HEADER_MAGIC)] != _RPM_HEADER_MAGIC:
        raise RuntimeError(f"invalid RPM header magic at offset {offset}")
    index_count, store_size = struct.unpack(">II", data[offset + 8 : offset + 16])
    index_start = offset + 16
    store_start = index_start + (index_count * _RPM_INDEX_LEN)
    store_end = store_start + store_size
    if store_end > len(data):
        raise RuntimeError("RPM header extends beyond end of file")

    values: dict[int, str] = {}
    store = data[store_start:store_end]
    for index in range(index_count):
        entry_start = index_start + (index * _RPM_INDEX_LEN)
        tag, value_type, value_offset, _count = struct.unpack(
            ">IIII", data[entry_start : entry_start + _RPM_INDEX_LEN]
        )
        if value_type != _RPM_TYPE_STRING:
            continue
        end = store.find(b"\0", value_offset)
        if end >= 0:
            values[tag] = store[value_offset:end].decode("utf-8", errors="replace")
    next_offset = _align(store_end, 8) if align_after else store_end
    return values, next_offset


def _looks_like_compressed_payload(payload: bytes, compressor: str | None) -> bool:
    if compressor == "gzip":
        return payload.startswith(b"\x1f\x8b")
    if compressor == "xz":
        return payload.startswith(b"\xfd7zXZ\x00")
    if compressor == "zstd":
        return payload.startswith(b"\x28\xb5\x2f\xfd")
    return bool(payload)


def _rpm_payload(data: bytes) -> tuple[bytes, str | None, str | None]:
    if len(data) < _RPM_LEAD_LEN or data[:4] != _RPM_LEAD_MAGIC:
        raise RuntimeError("not an RPM package")
    _, offset = _read_rpm_header(data, _RPM_LEAD_LEN, align_after=True)
    values, payload_offset = _read_rpm_header(data, offset, align_after=False)
    payload_format = values.get(_RPM_TAG_PAYLOAD_FORMAT)
    payload_compressor = values.get(_RPM_TAG_PAYLOAD_COMPRESSOR)
    payload = data[payload_offset:]
    aligned_payload_offset = _align(payload_offset, 8)
    if (
        aligned_payload_offset != payload_offset
        and not _looks_like_compressed_payload(payload, payload_compressor)
        and _looks_like_compressed_payload(data[aligned_payload_offset:], payload_compressor)
    ):
        payload = data[aligned_payload_offset:]
    return payload, payload_format, payload_compressor


def _decompress_payload(payload: bytes, compressor: str | None) -> bytes:
    if compressor == "gzip":
        return gzip.decompress(payload)
    if compressor == "xz":
        return lzma.decompress(payload)
    if compressor == "zstd":
        with zstandard.ZstdDecompressor().stream_reader(payload) as reader:
            return reader.read()
    if payload.startswith(b"070701") or payload.startswith(b"070702"):
        return payload
    raise RuntimeError(f"unsupported RPM payload compressor {compressor!r}")


def _iter_cpio_newc(payload: bytes) -> Iterable[tuple[str, bytes]]:
    offset = 0
    while offset + _CPIO_NEWC_HEADER_LEN <= len(payload):
        header = payload[offset : offset + _CPIO_NEWC_HEADER_LEN]
        if header[:6] not in {b"070701", b"070702"}:
            raise RuntimeError(f"invalid cpio newc header at offset {offset}")
        filesize = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        name_start = offset + _CPIO_NEWC_HEADER_LEN
        name_end = name_start + namesize
        if namesize <= 0 or name_end > len(payload):
            raise RuntimeError("invalid cpio entry name")
        name = payload[name_start:name_end].rstrip(b"\0").decode("utf-8", errors="replace")
        data_start = _align(name_end, 4)
        data_end = data_start + filesize
        if data_end > len(payload):
            raise RuntimeError(f"cpio entry {name!r} extends beyond archive")
        if name == _CPIO_TRAILER:
            return
        yield name.removeprefix("./"), payload[data_start:data_end]
        offset = _align(data_end, 4)


def _cpio_newc_entry(name: str, data: bytes = b"", *, mode: int = 0o100644) -> bytes:
    encoded_name = name.encode("utf-8") + b"\0"
    fields = [
        0,
        mode,
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(encoded_name),
        0,
    ]
    header = "070701" + "".join(f"{field:08x}" for field in fields)
    raw = header.encode("ascii") + encoded_name
    return raw + (b"\0" * ((-len(raw)) % 4)) + data + (b"\0" * ((-len(data)) % 4))


def _cpio_archive(entries: dict[str, bytes]) -> bytes:
    payload = bytearray()
    directories: set[str] = set()
    for name in entries:
        parts = name.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    for directory in sorted(directories):
        payload += _cpio_newc_entry(directory, mode=0o40755)
    for name, data in sorted(entries.items()):
        payload += _cpio_newc_entry(name, data)
    payload += _cpio_newc_entry(_CPIO_TRAILER, mode=0)
    return bytes(payload)


def _gzip_cpio_archive(entries: dict[str, bytes]) -> bytes:
    return gzip.compress(_cpio_archive(entries), mtime=0)


def _extract_boot_kernel_files_from_cpio(
    payload: bytes,
    *,
    payload_format: str | None,
    payload_compressor: str | None,
) -> BootKernelFiles:
    kernel: tuple[str, bytes] | None = None
    config: tuple[str, bytes] | None = None
    for name, data in _iter_cpio_newc(payload):
        if kernel is None and re.fullmatch(r"boot/vmlinuz[^/]*", name):
            kernel = (name, data)
        elif config is None and re.fullmatch(r"boot/config[^/]*", name):
            config = (name, data)
        if kernel is not None and config is not None:
            break
    if kernel is None:
        raise RuntimeError("RPM payload did not contain /boot/vmlinuz*")
    return BootKernelFiles(
        kernel_name=kernel[0],
        kernel=kernel[1],
        config_name=config[0] if config is not None else None,
        config=config[1] if config is not None else None,
        payload_format=payload_format,
        payload_compressor=payload_compressor,
    )


def extract_boot_kernel_files_from_rpm(path: Path) -> BootKernelFiles:
    payload, payload_format, payload_compressor = _rpm_payload(path.read_bytes())
    if payload_format != "cpio":
        raise RuntimeError(f"unsupported RPM payload format {payload_format!r}")
    return _extract_boot_kernel_files_from_cpio(
        _decompress_payload(payload, payload_compressor),
        payload_format=payload_format,
        payload_compressor=payload_compressor,
    )


def extract_rpm_payload(path: Path) -> RpmPayload:
    payload, payload_format, payload_compressor = _rpm_payload(path.read_bytes())
    if payload_format != "cpio":
        raise RuntimeError(f"unsupported RPM payload format {payload_format!r}")
    entries = dict(_iter_cpio_newc(_decompress_payload(payload, payload_compressor)))
    return RpmPayload(
        entries=entries,
        payload_format=payload_format,
        payload_compressor=payload_compressor,
    )


def extract_initrd_module_files_from_rpm(
    path: Path,
    *,
    patterns: tuple[str, ...] = DEFAULT_INITRD_MODULE_PATTERNS,
) -> dict[str, bytes]:
    entries = extract_rpm_payload(path).entries
    compiled = tuple(re.compile(pattern) for pattern in patterns)
    selected = {
        _uncompressed_module_name(name)
        for name in entries
        if any(pattern.fullmatch(name) for pattern in compiled)
    }
    selected.update(_module_dependency_closure(entries, selected))

    files: dict[str, bytes] = {}
    for name, data in entries.items():
        output_name = _uncompressed_module_name(name)
        if output_name not in selected:
            continue
        if name.endswith(".ko.xz"):
            files[output_name] = lzma.decompress(data)
        else:
            files[output_name] = data
    return files


def _uncompressed_module_name(name: str) -> str:
    return name.removesuffix(".xz") if name.endswith(".ko.xz") else name


def _module_dependency_closure(
    entries: dict[str, bytes],
    selected: set[str],
) -> set[str]:
    """Resolve selected kernel modules through the RPM's ``modules.dep`` graph."""

    dependency_files = [
        (name, data)
        for name, data in entries.items()
        if re.fullmatch(r"lib/modules/[^/]+/modules\.dep", name)
    ]
    dependencies: dict[str, tuple[str, ...]] = {}
    for metadata_name, data in dependency_files:
        module_root = metadata_name.removesuffix("modules.dep")
        for raw_line in data.decode("utf-8", errors="replace").splitlines():
            module, separator, raw_dependencies = raw_line.partition(":")
            if not separator or not module.strip():
                continue
            dependencies[module_root + module.strip()] = tuple(
                module_root + dependency for dependency in raw_dependencies.split() if dependency
            )

    closure: set[str] = set()
    pending = [name for name in selected if name.endswith(".ko")]
    while pending:
        module = pending.pop()
        for dependency in dependencies.get(module, ()):
            if dependency in selected or dependency in closure:
                continue
            closure.add(dependency)
            pending.append(dependency)
    return closure


def _validate_kernel_config(config: bytes | None, requirements: tuple[str, ...]) -> list[str]:
    if not requirements:
        return []
    if config is None:
        return list(requirements)
    lines = _kernel_config_lines(config)
    return [requirement for requirement in requirements if requirement not in lines]


def _kernel_config_warnings(config: bytes | None) -> list[str]:
    if config is None:
        return []
    lines = _kernel_config_lines(config)
    return [warning for option, warning in EXPERIMENTAL_CONFIG_WARNINGS.items() if option in lines]


def _kernel_config_lines(config: bytes) -> set[str]:
    try:
        text = config.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("LCOW kernel configuration is not valid UTF-8") from exc
    return {line.strip() for line in text.splitlines()}


def _write_output_file(path: Path, data: bytes, *, force: bool) -> None:
    if path.is_file():
        if path.read_bytes() == data:
            return
        if not force:
            raise RuntimeError(f"{path} already exists with different content; rerun with --force")
    path.write_bytes(data)


def package_kernel(
    *,
    rpm_path: Path,
    output_dir: Path,
    package: KernelPackage | None,
    force: bool,
    requirements: tuple[str, ...],
) -> Path:
    files = extract_boot_kernel_files_from_rpm(rpm_path)
    missing = _validate_kernel_config(files.config, requirements)
    if missing:
        raise RuntimeError(
            f"{rpm_path.name} does not satisfy LCOW kernel config requirements: "
            + ", ".join(missing)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    kernel_path = output_dir / KERNEL_OUTPUT_NAME
    _write_output_file(kernel_path, files.kernel, force=force)
    if files.config is not None:
        _write_output_file(output_dir / CONFIG_COPY_NAME, files.config, force=force)
    module_files = extract_initrd_module_files_from_rpm(rpm_path)
    if module_files:
        _write_output_file(
            output_dir / MODULES_ARCHIVE_NAME,
            _gzip_cpio_archive(module_files),
            force=force,
        )

    manifest = {
        "schema": 1,
        "source": (
            "packages.microsoft.com/azurelinux/"
            + (package.name if package is not None else DEFAULT_PACKAGE_NAME)
        ),
        "rpm": rpm_path.name,
        "package": None
        if package is None
        else {
            "name": package.name,
            "version": package.version,
            "release": package.release,
            "arch": package.arch,
            "location": package.location,
            "checksum": package.checksum,
            "build_time": package.build_time,
        },
        "source_files": {
            "kernel": files.kernel_name,
            "config": files.config_name,
            "payload_format": files.payload_format,
            "payload_compressor": files.payload_compressor,
        },
        "output_files": {
            "kernel": KERNEL_OUTPUT_NAME,
            "config": CONFIG_COPY_NAME if files.config is not None else None,
            "initrd_modules": MODULES_ARCHIVE_NAME if module_files else None,
        },
        "initrd_module_files": sorted(module_files),
        "kernel_sha256": hashlib.sha256(files.kernel).hexdigest(),
        "required_config": list(requirements),
        "warnings": _kernel_config_warnings(files.config),
    }
    _write_output_file(
        output_dir / MANIFEST_NAME,
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        force=force,
    )
    return kernel_path
