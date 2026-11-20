"""Windows LCOW guest kernel: package, verify and release the Azure Linux amd64 kernel.

Cage boots this kernel in a Hyper-V utility VM through HCS. It is Microsoft's
Azure Linux kernel RPM, pinned by content in `windows/lcow-kernel.toml`. The
RPM is unpacked here, not built: the payload is the kernel, its configuration,
and the loadable modules the LCOW guest needs (9p, nftables, CIFS and their
`modules.dep` closure). The `cage-init` unit merges those modules into the
hcsshim initrd it builds, taking them from this unit's published release.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import _lcow_rpm
from _common import (
    LOCAL_ROOT,
    UNIT_ROOT,
    ensure_under_local,
    file_record,
    repo_path,
    zstd_compress,
)

PLATFORM = "windows"
ARCHITECTURE = "amd64"
PINS_PATH = UNIT_ROOT / PLATFORM / "lcow-kernel.toml"
PINS_SCHEMA = 1
DEFAULT_WORK_DIR = LOCAL_ROOT / "cage-kernel" / PLATFORM
DEFAULT_ARTIFACT_DIR = DEFAULT_WORK_DIR / "lcow"
REQUIRED_CONFIG = _lcow_rpm.DEFAULT_CONFIG_REQUIREMENTS
MINIMUM_KERNEL_SIZE = 1024 * 1024

KERNEL_FILE = _lcow_rpm.KERNEL_OUTPUT_NAME
CONFIG_FILE = _lcow_rpm.CONFIG_COPY_NAME
MODULES_FILE = _lcow_rpm.MODULES_ARCHIVE_NAME
KERNEL_MANIFEST_FILE = _lcow_rpm.MANIFEST_NAME
INPUTS_FILE = "inputs.json"
PAYLOAD_FILES = frozenset(
    {KERNEL_FILE, CONFIG_FILE, MODULES_FILE, KERNEL_MANIFEST_FILE, INPUTS_FILE}
)

KERNEL_ASSET = f"{PLATFORM}-{ARCHITECTURE}-lcow-kernel.zst"
MODULES_ASSET = f"{PLATFORM}-{ARCHITECTURE}-lcow-modules.cpio.gz"
CONFIG_ASSET = f"{PLATFORM}-{ARCHITECTURE}-lcow-kernel.config"

#: LCOW features this kernel may build as loadable modules, and the modules each
#: then needs in the guest. A feature built in (`=y`) needs none.
MODULAR_CONFIG = {
    "9p": frozenset({"CONFIG_9P_FS=m", "CONFIG_NET_9P=m", "CONFIG_NET_9P_VIRTIO=m"}),
    "nft": frozenset({"CONFIG_NF_TABLES=m", "CONFIG_NFT_CT=m"}),
    "cifs": frozenset({"CONFIG_CIFS=m"}),
}
MODULE_PATTERNS = {
    "9p": (
        re.compile(r"lib/modules/[^/]+/kernel/fs/9p/9p\.ko"),
        re.compile(r"lib/modules/[^/]+/kernel/fs/netfs/netfs\.ko"),
        re.compile(r"lib/modules/[^/]+/kernel/net/9p/9pnet\.ko"),
        re.compile(r"lib/modules/[^/]+/kernel/net/9p/9pnet_fd\.ko"),
        re.compile(r"lib/modules/[^/]+/kernel/net/9p/9pnet_virtio\.ko"),
    ),
    "nft": (
        re.compile(r"lib/modules/[^/]+/kernel/net/netfilter/nf_tables\.ko"),
        re.compile(r"lib/modules/[^/]+/kernel/net/netfilter/nft_ct\.ko"),
    ),
    "cifs": (re.compile(r"lib/modules/[^/]+/kernel/fs/smb/client/cifs\.ko"),),
}


def load_pins() -> dict[str, Any]:
    """The pinned kernel RPM; refuses an unknown schema or a pin without a digest."""
    path = PINS_PATH
    pins = tomllib.loads(path.read_text(encoding="utf-8"))
    if pins.get("schema") != PINS_SCHEMA:
        raise SystemExit(f"{repo_path(path)}: unsupported schema {pins.get('schema')!r}")
    kernel = pins.get("kernel")
    if not isinstance(kernel, dict):
        raise SystemExit(f"{repo_path(path)}: missing [kernel]")
    for key in ("name", "version", "release", "arch", "url"):
        if not isinstance(kernel.get(key), str) or not kernel[key]:
            raise SystemExit(f"{repo_path(path)}: [kernel] {key} must be a non-empty string")
    if not isinstance(kernel.get("build_time"), int):
        raise SystemExit(f"{repo_path(path)}: [kernel] build_time must be an integer")
    sha256 = kernel.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise SystemExit(f"{repo_path(path)}: [kernel] sha256 must be a SHA-256 digest")
    return pins


def kernel_pin(pins: dict[str, Any]) -> dict[str, Any]:
    """The validated `[kernel]` table of *pins* from `load_pins`."""
    kernel = pins["kernel"]
    if not isinstance(kernel, dict):
        raise SystemExit(f"{repo_path(PINS_PATH)}: missing [kernel]")
    return kernel


def recorded_module_files(manifest: object) -> list[str] | None:
    """The module files the packager's manifest lists, or None if it lists none."""
    files = manifest.get("initrd_module_files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        return None
    return sorted(files)


def modular_features(config: str) -> list[str]:
    """The LCOW features *config* builds as loadable modules."""
    lines = {line.strip() for line in config.splitlines()}
    return sorted(feature for feature, options in MODULAR_CONFIG.items() if lines & options)


def payload_problems(payload: Path, *, pins: dict[str, Any]) -> list[str]:
    """Why *payload* is not a complete LCOW kernel built from *pins*; empty if it is."""
    try:
        files = {path.name for path in payload.iterdir()}
    except OSError as exc:
        return [f"cannot read {payload}: {exc}"]
    if files != PAYLOAD_FILES:
        return [
            f"payload files differ: missing {sorted(PAYLOAD_FILES - files)}, "
            f"unexpected {sorted(files - PAYLOAD_FILES)}"
        ]
    if any(not (payload / name).is_file() or (payload / name).is_symlink() for name in files):
        return ["payload entries must be regular files"]
    errors: list[str] = []
    try:
        config = (payload / CONFIG_FILE).read_text(encoding="utf-8")
        manifest = json.loads((payload / KERNEL_MANIFEST_FILE).read_text(encoding="utf-8"))
        inputs = json.loads((payload / INPUTS_FILE).read_text(encoding="utf-8"))
        archive = gzip.decompress((payload / MODULES_FILE).read_bytes())
        archived = {name for name, _ in _lcow_rpm._iter_cpio_newc(archive)}
    except (OSError, ValueError, EOFError, RuntimeError) as exc:
        return [f"payload is unreadable: {exc}"]
    if not isinstance(inputs, dict) or inputs.get("pins") != pins:
        errors.append(f"it was built from other pins than {repo_path(PINS_PATH)}")
    lines = {line.strip() for line in config.splitlines()}
    errors += [f"kernel lacks {option}" for option in REQUIRED_CONFIG if option not in lines]
    if (payload / KERNEL_FILE).stat().st_size < MINIMUM_KERNEL_SIZE:
        errors.append("kernel is implausibly small")
    module_files = recorded_module_files(manifest)
    if module_files is None:
        return [*errors, f"{KERNEL_MANIFEST_FILE} does not list the module files"]
    if missing := sorted(set(module_files) - archived):
        errors.append(f"modules archive lacks {missing[:5]}")
    for feature in modular_features(config):
        if not all(
            any(pattern.fullmatch(name) for name in module_files)
            for pattern in MODULE_PATTERNS[feature]
        ):
            errors.append(f"{feature} is modular but its kernel modules are missing")
    return errors


def _download(url: str, sha256: str, destination: Path) -> Path:
    return _lcow_rpm._download_to_file(url, destination, checksum=f"sha256:{sha256}")


def create(args: argparse.Namespace) -> int:
    pins = load_pins()
    spec = kernel_pin(pins)
    work_dir = ensure_under_local(args.work_dir)
    artifact_dir = ensure_under_local(args.artifact_dir)
    rpm = _download(spec["url"], spec["sha256"], work_dir / "downloads" / Path(spec["url"]).name)
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lcow-kernel-", dir=work_dir) as scratch:
        staged = Path(scratch) / "payload"
        _lcow_rpm.package_kernel(
            rpm_path=rpm,
            output_dir=staged,
            package=_lcow_rpm.KernelPackage(
                name=spec["name"],
                version=spec["version"],
                release=spec["release"],
                arch=spec["arch"],
                location=Path(spec["url"]).name,
                checksum=f"sha256:{spec['sha256']}",
                build_time=spec["build_time"],
            ),
            force=True,
            requirements=REQUIRED_CONFIG,
        )
        (staged / INPUTS_FILE).write_text(
            json.dumps({"pins": pins}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if errors := payload_problems(staged, pins=pins):
            raise SystemExit("packaged LCOW kernel is not usable: " + "; ".join(errors))
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(artifact_dir)
    print(f"Wrote the Windows LCOW kernel to {repo_path(artifact_dir)}")
    return 0


def verify(args: argparse.Namespace) -> int:
    errors = payload_problems(args.artifact_dir.resolve(strict=False), pins=load_pins())
    for error in errors:
        print(f"windows lcow kernel: {error}")
    return 1 if errors else 0


def latest(args: argparse.Namespace) -> int:
    """Print `[kernel]` pins for the newest kernel RPM in the Azure Linux repository."""
    package = _lcow_rpm.resolve_latest_kernel_package(
        repo_url=args.repo_url, package_name=args.package_name, arch=args.arch
    )
    if package.checksum is None or not package.checksum.startswith("sha256:"):
        raise SystemExit(f"{package.rpm_name} has no SHA-256 in the repository metadata")
    url = _lcow_rpm._repo_url(args.repo_url, package.location)
    print("[kernel]")
    for key, value in (
        ("name", package.name),
        ("version", package.version),
        ("release", package.release),
        ("arch", package.arch),
        ("url", url),
        ("sha256", package.checksum.removeprefix("sha256:")),
    ):
        print(f'{key} = "{value}"')
    print(f"build_time = {package.build_time}")
    return 0


# -- Release -----------------------------------------------------------------


def release_asset_names() -> tuple[str, ...]:
    return (KERNEL_ASSET, MODULES_ASSET, CONFIG_ASSET)


def package_release(artifact_dir: Path, release_dir: Path) -> dict[str, object]:
    """Write the LCOW kernel's release assets; return this platform's manifest entry."""
    pins = load_pins()
    payload = artifact_dir.resolve(strict=False)
    if errors := payload_problems(payload, pins=pins):
        raise SystemExit(
            f"{repo_path(payload)} is not a releasable LCOW kernel: {'; '.join(errors)}; "
            "rebuild with `cage_kernel.py windows create`"
        )
    kernel = release_dir / KERNEL_ASSET
    zstd_compress(payload / KERNEL_FILE, kernel)
    modules = release_dir / MODULES_ASSET
    shutil.copyfile(payload / MODULES_FILE, modules)
    config = release_dir / CONFIG_ASSET
    shutil.copyfile(payload / CONFIG_FILE, config)
    module_files = recorded_module_files(
        json.loads((payload / KERNEL_MANIFEST_FILE).read_text(encoding="utf-8"))
    )
    if module_files is None:
        raise SystemExit(f"{KERNEL_MANIFEST_FILE} does not list the module files")
    config_text = config.read_text(encoding="utf-8")
    return {
        "architecture": ARCHITECTURE,
        "source": kernel_pin(pins),
        "required_config": sorted(REQUIRED_CONFIG),
        "modular_features": modular_features(config_text),
        "kernel": {"name": kernel.name, **file_record(kernel)},
        "content": file_record(payload / KERNEL_FILE),
        "modules": {
            "name": modules.name,
            **file_record(modules),
            "files": module_files,
        },
        "config": {"name": config.name, **file_record(config)},
    }


# -- Command line ------------------------------------------------------------


def add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=f"download and scratch directory, default: {repo_path(DEFAULT_WORK_DIR)}",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=f"packaged kernel directory, default: {repo_path(DEFAULT_ARTIFACT_DIR)}",
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add the `windows` command group."""
    group = subparsers.add_parser("windows", help="Windows LCOW amd64 guest kernel")
    commands = group.add_subparsers(dest="windows_command", required=True)

    create_command = commands.add_parser("create", help="package the pinned kernel RPM")
    add_paths(create_command)
    create_command.set_defaults(handler=create)

    verify_command = commands.add_parser("verify", help="verify the packaged kernel")
    add_paths(verify_command)
    verify_command.set_defaults(handler=verify)

    latest_command = commands.add_parser(
        "latest", help="print pins for the newest kernel RPM, to bump lcow-kernel.toml"
    )
    latest_command.add_argument("--repo-url", default=_lcow_rpm.DEFAULT_REPO_URL)
    latest_command.add_argument("--package-name", default=_lcow_rpm.DEFAULT_PACKAGE_NAME)
    latest_command.add_argument("--arch", default=_lcow_rpm.DEFAULT_ARCH)
    latest_command.set_defaults(handler=latest)
