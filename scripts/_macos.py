"""macOS ContainerKit guest kernels: build, verify and install the arm64 profiles.

Cage boots these kernels in Apple Virtualization through ContainerKit. Each
profile is the pinned `apple/containerization` guest kernel plus Cage's
kernel-config patches under `macos/patches/`.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import hashlib
import http.client
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from _common import (
    LOCAL_ROOT,
    UNIT_ROOT,
    ensure_under_local,
    file_record,
    repo_path,
    run,
    zstd_compress,
)

PLATFORM = "macos"
ARCHITECTURE = "arm64"
DEFAULT_WORK_DIR = LOCAL_ROOT / "cage-kernel" / PLATFORM
DEFAULT_CONTAINERIZATION_URL = "https://github.com/apple/containerization.git"
DEFAULT_CONTAINERIZATION_REVISION = "d9868bb657fac3b55ed5dcec97c8eb8a08e78bf5"
HOTPLUG_PATCH_PATH = UNIT_ROOT / PLATFORM / "patches" / "containerization-hotplug-guest.patch"
NBD_PATCH_PATH = UNIT_ROOT / PLATFORM / "patches" / "containerization-nbd-guest.patch"
CIFS_PATCH_PATH = UNIT_ROOT / PLATFORM / "patches" / "containerization-cifs-guest.patch"
DEFAULT_PROFILE_NAME = "hotplug"
DEFAULT_ARTIFACT_DIR = DEFAULT_WORK_DIR / "kernels"
DEFAULT_INSTALL_ROOT = LOCAL_ROOT / "cage"
DEFAULT_INSTALL_DIR = DEFAULT_INSTALL_ROOT / "kernels"
DEFAULT_INSTALL_PATH = DEFAULT_INSTALL_ROOT / "vmlinux"
KERNEL_RELEASES_URL = "https://www.kernel.org/releases.json"
DEFAULT_KERNEL_SOURCE_SERIES = "6.18"
KERNEL_BUILD_IMAGE = "kernel-build:0.1"
KERNEL_BUILD_BASE_IMAGE = "ubuntu:focal"
UBUNTU_DNS_PROBE_IMAGE = "ubuntu:focal"
UBUNTU_DNS_PROBE_HOSTS = ("ports.ubuntu.com", "archive.ubuntu.com")
CONTAINER_SERVICE_FAILURE_MARKERS = (
    "XPC connection error",
    "Connection invalid",
    "container system start",
)
KERNEL_BUILD_PACKAGES = (
    "autoconf",
    "bc",
    "binutils-multiarch",
    "binutils-aarch64-linux-gnu",
    "bison",
    "flex",
    "gcc",
    "xz-utils",
    "gcc-aarch64-linux-gnu",
    "git",
    "libncurses-dev",
    "make",
    "openssl",
    "python-is-python3",
)
HOTPLUG_CONFIG_LINES = frozenset(
    {
        "CONFIG_SCSI=y",
        "CONFIG_BLK_DEV_SD=y",
        "CONFIG_USB=y",
        "CONFIG_USB_XHCI_HCD=y",
        "CONFIG_USB_STORAGE=y",
        "CONFIG_USB_UAS=y",
    }
)
RUNTIME_SECURITY_CONFIG_LINES = frozenset({"# CONFIG_VSOCKETS_LOOPBACK is not set"})
NBD_TRANSPORT_CONFIG_LINES = frozenset({"CONFIG_BLK_DEV_NBD=y"})
NBD_CONFIG_LINES = NBD_TRANSPORT_CONFIG_LINES | HOTPLUG_CONFIG_LINES
CIFS_CONFIG_LINES = frozenset(
    {
        "CONFIG_CIFS=y",
        "CONFIG_CIFS_ALLOW_INSECURE_LEGACY=y",
        "CONFIG_CIFS_UPCALL=y",
        "CONFIG_CIFS_XATTR=y",
        "CONFIG_CIFS_POSIX=y",
        "CONFIG_CIFS_DFS_UPCALL=y",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class KernelPatch:
    path: Path
    required_config_lines: frozenset[str]


@dataclasses.dataclass(frozen=True, slots=True)
class KernelProfile:
    name: str
    description: str
    patches: tuple[KernelPatch, ...]
    required_config_lines: frozenset[str]


HOTPLUG_PATCH = KernelPatch(HOTPLUG_PATCH_PATH, HOTPLUG_CONFIG_LINES)
NBD_PATCH = KernelPatch(NBD_PATCH_PATH, NBD_TRANSPORT_CONFIG_LINES)
CIFS_PATCH = KernelPatch(CIFS_PATCH_PATH, CIFS_CONFIG_LINES)
KERNEL_PROFILES = {
    "hotplug": KernelProfile(
        name="hotplug",
        description="Apple guest kernel with Cage hotplug direct-volume support",
        patches=(HOTPLUG_PATCH,),
        required_config_lines=HOTPLUG_CONFIG_LINES | RUNTIME_SECURITY_CONFIG_LINES,
    ),
    "nbd": KernelProfile(
        name="nbd",
        description="Apple guest kernel with Cage NBD and hotplug direct-volume support",
        patches=(HOTPLUG_PATCH, NBD_PATCH),
        required_config_lines=NBD_CONFIG_LINES | RUNTIME_SECURITY_CONFIG_LINES,
    ),
    "nbd-cifs": KernelProfile(
        name="nbd-cifs",
        description="Cage NBD and hotplug direct-volume kernel with SMB/CIFS guest mounts",
        patches=(HOTPLUG_PATCH, NBD_PATCH, CIFS_PATCH),
        required_config_lines=NBD_CONFIG_LINES | CIFS_CONFIG_LINES | RUNTIME_SECURITY_CONFIG_LINES,
    ),
}
PROFILE_ORDER = ("hotplug", "nbd", "nbd-cifs")


def run_probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
    display = " ".join(argv)
    print(f"+ {display}", flush=True)
    return subprocess.run(
        argv,
        cwd=UNIT_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def run_container(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    display = " ".join(argv)
    print(f"+ {display}", flush=True)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            argv,
            output=proc.stdout,
            stderr=proc.stderr,
        )
    return proc


def managed_checkout(args: argparse.Namespace) -> Path:
    return args.work_dir.resolve() / "containerization"


def built_kernel_path(args: argparse.Namespace) -> Path:
    return managed_checkout(args) / "kernel" / "vmlinux-arm64"


def kernel_dir(args: argparse.Namespace) -> Path:
    return managed_checkout(args) / "kernel"


def profile_from_args(args: argparse.Namespace) -> KernelProfile:
    name = getattr(args, "profile", DEFAULT_PROFILE_NAME)
    try:
        return KERNEL_PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(PROFILE_ORDER)
        raise SystemExit(f"unknown kernel profile {name!r}; choose one of: {choices}") from exc


def profile_artifact_path(args: argparse.Namespace, profile: KernelProfile) -> Path:
    artifact_dir = getattr(args, "artifact_dir", None) or DEFAULT_ARTIFACT_DIR
    return artifact_dir.resolve(strict=False) / profile.name / "vmlinux"


def default_install_path(profile: KernelProfile) -> Path:
    if profile.name == DEFAULT_PROFILE_NAME:
        return DEFAULT_INSTALL_PATH
    return DEFAULT_INSTALL_DIR / profile.name / "vmlinux"


def install_destination(args: argparse.Namespace, profile: KernelProfile) -> Path:
    install_path = getattr(args, "install_path", None)
    if install_path is not None:
        return install_path.resolve(strict=False)
    return default_install_path(profile).resolve(strict=False)


def default_verify_source(args: argparse.Namespace, profile: KernelProfile) -> Path:
    artifact = profile_artifact_path(args, profile)
    if artifact.is_file():
        return artifact
    return built_kernel_path(args)


def ensure_safe_work_dir(work_dir: Path) -> None:
    ensure_under_local(work_dir)


def checkout_revision(checkout: Path, revision: str) -> None:
    try:
        run(["git", "checkout", "--detach", revision], cwd=checkout)
    except subprocess.CalledProcessError:
        run(["git", "fetch", "origin", revision], cwd=checkout)
        run(["git", "checkout", "--detach", revision], cwd=checkout)


def checkout_config_lines(checkout: Path) -> set[str]:
    config = checkout / "kernel" / "config-arm64"
    try:
        return set(config.read_text(encoding="utf-8").splitlines())
    except OSError as exc:
        raise SystemExit(f"could not read {repo_path(config)}: {exc}") from exc


def apply_profile_patches(checkout: Path, profile: KernelProfile) -> None:
    for patch in profile.patches:
        if patch.required_config_lines <= checkout_config_lines(checkout):
            print(f"Skipping {repo_path(patch.path)}; required config is already present")
            continue
        run(["git", "apply", "--check", str(patch.path)], cwd=checkout)
        run(["git", "apply", str(patch.path)], cwd=checkout)


def prepare(args: argparse.Namespace) -> int:
    profile = profile_from_args(args)
    ensure_safe_work_dir(args.work_dir)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    checkout = managed_checkout(args)

    if (checkout / ".git").is_dir():
        run(["git", "fetch", "--tags", "origin"], cwd=checkout)
    else:
        run(
            [
                "git",
                "clone",
                "--no-checkout",
                args.containerization_url,
                str(checkout),
            ],
            cwd=UNIT_ROOT,
        )

    checkout_revision(checkout, args.containerization_revision)
    run(["git", "reset", "--hard", args.containerization_revision], cwd=checkout)
    run(["git", "clean", "-fdx"], cwd=checkout)
    apply_profile_patches(checkout, profile)
    print(f"Prepared {repo_path(checkout)} at {args.containerization_revision} for {profile.name}")
    return 0


def read_kernel_config(kernel: str | Path) -> str | None:
    try:
        data = Path(kernel).read_bytes()
    except OSError:
        return None

    start = data.find(b"IKCFG_ST")
    if start >= 0:
        start += len(b"IKCFG_ST")
        end = data.find(b"IKCFG_ED", start)
        if end >= 0:
            config = data[start:end]
            if config.startswith(b"\x1f\x8b\x08"):
                try:
                    config = zlib.decompress(config, 16 + zlib.MAX_WBITS)
                except zlib.error:
                    return None
            return config.decode(errors="replace")

    for match in re.finditer(b"\x1f\x8b\x08", data):
        try:
            config = zlib.decompress(data[match.start() :], 16 + zlib.MAX_WBITS)
        except zlib.error:
            continue
        if config.startswith(b"#") and b"CONFIG_" in config:
            return config.decode(errors="replace")
    return None


def kernel_config_lines(kernel: Path) -> set[str]:
    config = read_kernel_config(kernel)
    if config is None:
        raise SystemExit(f"{repo_path(kernel)} does not contain an embedded kernel config")
    return set(config.splitlines())


def verify_kernel(kernel: Path, profile: KernelProfile) -> None:
    if not kernel.is_file():
        raise SystemExit(f"kernel image not found: {repo_path(kernel)}")
    options = kernel_config_lines(kernel)
    missing = sorted(profile.required_config_lines - options)
    if missing:
        rendered = ", ".join(missing)
        raise SystemExit(
            f"{repo_path(kernel)} does not satisfy profile {profile.name!r}; "
            f"missing required config: {rendered}"
        )
    print(f"{repo_path(kernel)} satisfies Cage kernel profile {profile.name}")


def verify(args: argparse.Namespace) -> int:
    profile = profile_from_args(args)
    kernel = args.kernel if args.kernel is not None else default_verify_source(args, profile)
    verify_kernel(kernel.resolve(strict=False), profile)
    return 0


def parse_scutil_nameservers(output: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r"^\s*nameserver\[\d+\]\s*:\s*(\S+)\s*$", output, re.MULTILINE)
    ]


def read_macos_nameservers() -> list[str]:
    try:
        proc = subprocess.run(
            ["scutil", "--dns"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_scutil_nameservers(proc.stdout)


def read_resolv_conf_nameservers() -> list[str]:
    try:
        lines = Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    servers: list[str] = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            servers.append(parts[1])
    return servers


def sanitize_nameservers(candidates: list[str] | tuple[str, ...]) -> list[str]:
    servers: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        try:
            ip = ipaddress.ip_address(raw.strip())
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            continue
        text = str(ip)
        if text in seen:
            continue
        seen.add(text)
        servers.append(text)
    return servers


def discovered_nameservers() -> list[str]:
    if sys.platform == "darwin":
        return sanitize_nameservers(read_macos_nameservers())
    return sanitize_nameservers(read_resolv_conf_nameservers())


def build_nameservers(args: argparse.Namespace) -> list[str]:
    explicit = getattr(args, "dns", None)
    if explicit:
        servers = sanitize_nameservers(tuple(explicit))
        if len(servers) != len(explicit):
            print(
                "Ignoring invalid, duplicate, or unsafe --dns values",
                file=sys.stderr,
            )
        if servers:
            return servers
        raise SystemExit("no usable --dns values were provided")
    return discovered_nameservers()


def dns_args(nameservers: list[str]) -> list[str]:
    command: list[str] = []
    for nameserver in nameservers:
        command.extend(["--dns", nameserver])
    return command


def kernel_git_version(kernel: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(kernel), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def version_key(version: str) -> tuple[int, ...]:
    numeric = version.split("-", 1)[0]
    return tuple(int(part) for part in numeric.split(".") if part.isdigit())


def kernel_snapshot_url(version: str) -> str:
    return (
        "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/"
        f"snapshot/linux-{version}.tar.gz"
    )


def kernel_source_candidates(releases: list[object], series: str) -> list[str]:
    prefix = f"{series}."
    candidates: list[tuple[tuple[int, ...], int, str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("iseol") is True:
            continue
        if release.get("moniker") not in {"stable", "longterm"}:
            continue
        version = release.get("version")
        source = release.get("source")
        if not isinstance(version, str):
            continue
        if version != series and not version.startswith(prefix):
            continue
        key = version_key(version)
        if isinstance(source, str) and (source.endswith(".tar.xz") or source.endswith(".tar.gz")):
            candidates.append((key, 0, source))
        candidates.append((key, 1, kernel_snapshot_url(version)))
    ordered = sorted(
        candidates,
        key=lambda candidate: (candidate[0], -candidate[1]),
        reverse=True,
    )
    return [url for _, _, url in ordered]


def source_url_available(url: str) -> bool:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "cage-kernel"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return False


def select_kernel_source_url(releases: list[object], series: str) -> str | None:
    for candidate in kernel_source_candidates(releases, series):
        if source_url_available(candidate):
            return candidate
    return None


def kernel_releases() -> list[object]:
    try:
        with urllib.request.urlopen(KERNEL_RELEASES_URL, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        UnicodeDecodeError,
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
        json.JSONDecodeError,
    ) as exc:
        raise SystemExit(f"could not read {KERNEL_RELEASES_URL}: {exc}") from exc
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, list):
        raise SystemExit(f"{KERNEL_RELEASES_URL} did not contain a releases list")
    return releases


def resolve_kernel_source_url(args: argparse.Namespace) -> str:
    explicit = getattr(args, "kernel_source_url", None)
    if explicit:
        return explicit
    series = getattr(args, "kernel_source_series", DEFAULT_KERNEL_SOURCE_SERIES)
    selected = select_kernel_source_url(kernel_releases(), series)
    if selected is None:
        raise SystemExit(
            f"kernel.org did not report a non-EOL stable/longterm {series} source tarball; "
            "use --kernel-source-url to provide an explicit archive"
        )
    return selected


def container_failure_output(exc: subprocess.CalledProcessError) -> str:
    output = exc.output if isinstance(exc.output, str) else ""
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    return "\n".join(part for part in (output, stderr) if part)


def is_container_service_failure(exc: subprocess.CalledProcessError) -> bool:
    text = container_failure_output(exc)
    return any(marker in text for marker in CONTAINER_SERVICE_FAILURE_MARKERS)


def report_container_failure(exc: subprocess.CalledProcessError, nameservers: list[str]) -> None:
    if is_container_service_failure(exc):
        print(
            "Apple `container` service is not available. Start it with "
            "`container system start`, then rerun the cage-kernel command.",
            file=sys.stderr,
        )
        print(
            f"failed command: {' '.join(str(part) for part in exc.cmd)}",
            file=sys.stderr,
        )
        return

    rendered = ", ".join(nameservers) if nameservers else "none"
    print(
        "container command failed during kernel build. "
        f"DNS servers passed to `container`: {rendered}. "
        "If apt reported name resolution failures, retry with one or more explicit "
        "`--dns <ip>` values or run `diagnose-dns`.",
        file=sys.stderr,
    )
    print(
        f"failed command: {' '.join(str(part) for part in exc.cmd)}",
        file=sys.stderr,
    )


def build_kernel_image(kernel: Path, nameservers: list[str]) -> None:
    command = [
        "container",
        "build",
        *dns_args(nameservers),
        "-f",
        "image/Dockerfile",
        "-t",
        KERNEL_BUILD_IMAGE,
        "image/",
    ]
    run_container(command, cwd=kernel)


def valid_kernel_source_archive(source: Path) -> bool:
    try:
        with tarfile.open(source, "r:*") as archive:
            return any(member.isfile() for member in archive)
    except (OSError, tarfile.TarError):
        return False


def kernel_source_cache_path(args: argparse.Namespace, source_url: str) -> Path:
    source_id = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    return args.work_dir.resolve() / "sources" / f"{source_id}.tar.xz"


def download_kernel_source(destination: Path, source_url: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f"{destination.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        run(
            [
                "curl",
                "-fL",
                "--show-error",
                "-o",
                str(tmp),
                source_url,
            ]
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"failed to download kernel source from {source_url}") from exc
    if not valid_kernel_source_archive(tmp):
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"downloaded kernel source is not a valid .tar.xz archive: {source_url}")
    tmp.replace(destination)


def ensure_kernel_source(args: argparse.Namespace, kernel: Path, source_url: str) -> None:
    cached = kernel_source_cache_path(args, source_url)
    if cached.exists() and not valid_kernel_source_archive(cached):
        print(f"Removing invalid cached kernel source: {repo_path(cached)}", file=sys.stderr)
        cached.unlink()
    if not cached.exists():
        download_kernel_source(cached, source_url)

    source = kernel / "source.tar.xz"
    if source.exists():
        if valid_kernel_source_archive(source):
            return
        print(f"Removing invalid cached kernel source: {repo_path(source)}", file=sys.stderr)
        source.unlink()
    shutil.copy2(cached, source)


def run_kernel_build_with_image(kernel: Path, nameservers: list[str]) -> None:
    command = [
        "container",
        "run",
        *dns_args(nameservers),
        "--cpus",
        "8",
        "--rm",
        "--memory",
        "16g",
        "-v",
        f"{kernel}:/kernel",
        "--env",
        f"LOCALVERSION=-cz-{kernel_git_version(kernel)}",
        "--cwd",
        "/kernel",
        KERNEL_BUILD_IMAGE,
        "/bin/bash",
        "-c",
        "./build.sh",
    ]
    run_container(command, cwd=kernel)


def direct_toolchain_script() -> str:
    packages = " ".join(KERNEL_BUILD_PACKAGES)
    return " && ".join(
        [
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update",
            f"apt-get install -y {packages}",
            "apt-get clean",
            "rm -rf /var/lib/apt/lists/*",
            "cp /kernel/image/sources.list /etc/apt/sources.list",
            "apt-get update",
            "dpkg --add-architecture arm64",
            "apt-get install -y libelf-dev:arm64",
            "apt-get clean",
            "rm -rf /var/lib/apt/lists/*",
            "./build.sh",
        ]
    )


def run_kernel_build_direct(kernel: Path, nameservers: list[str]) -> None:
    command = [
        "container",
        "run",
        *dns_args(nameservers),
        "--cpus",
        "8",
        "--rm",
        "--memory",
        "16g",
        "-v",
        f"{kernel}:/kernel",
        "--env",
        f"LOCALVERSION=-cz-{kernel_git_version(kernel)}",
        "--cwd",
        "/kernel",
        KERNEL_BUILD_BASE_IMAGE,
        "/bin/bash",
        "-lc",
        direct_toolchain_script(),
    ]
    run_container(command, cwd=kernel)


def build(args: argparse.Namespace) -> int:
    profile = profile_from_args(args)
    if shutil.which("container") is None:
        raise SystemExit("`container` CLI is required to build apple/containerization/kernel")
    ensure_container_system_available()
    if not args.no_prepare:
        prepare(args)
    nameservers = build_nameservers(args)
    rendered = ", ".join(nameservers) if nameservers else "container defaults"
    print(f"Using container DNS servers: {rendered}")
    kernel_source_url = resolve_kernel_source_url(args)
    print(f"Using kernel source: {kernel_source_url}")
    kernel = kernel_dir(args)
    try:
        ensure_kernel_source(args, kernel, kernel_source_url)
        try:
            build_kernel_image(kernel, nameservers)
        except subprocess.CalledProcessError as exc:
            report_container_failure(exc, nameservers)
            if is_container_service_failure(exc):
                raise SystemExit(exc.returncode) from exc
            print(
                "Falling back to direct Ubuntu build container with explicit DNS; "
                "this uses the same package recipe as the upstream Dockerfile.",
                file=sys.stderr,
            )
            try:
                run_kernel_build_direct(kernel, nameservers)
            except subprocess.CalledProcessError as direct_exc:
                report_container_failure(direct_exc, nameservers)
                raise
        else:
            try:
                run_kernel_build_with_image(kernel, nameservers)
            except subprocess.CalledProcessError as exc:
                report_container_failure(exc, nameservers)
                raise
    except subprocess.CalledProcessError:
        raise
    source = built_kernel_path(args)
    verify_kernel(source, profile)
    artifact = profile_artifact_path(args, profile)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, artifact)
    write_build_provenance(
        artifact,
        containerization_url=args.containerization_url,
        containerization_revision=args.containerization_revision,
        kernel_source_url=kernel_source_url,
    )
    print(f"Wrote {profile.name} kernel artifact to {repo_path(artifact)}")
    return 0


def install_local(args: argparse.Namespace) -> int:
    profile = profile_from_args(args)
    source = args.kernel if args.kernel is not None else default_verify_source(args, profile)
    verify_kernel(source, profile)
    destination = install_destination(args, profile)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print(f"Installed {profile.name} kernel {repo_path(source)} to {repo_path(destination)}")
    return 0


def selected_create_profiles(args: argparse.Namespace) -> list[KernelProfile]:
    requested = getattr(args, "profiles", None) or list(PROFILE_ORDER)
    profiles = [KERNEL_PROFILES[name] for name in requested]
    if getattr(args, "no_prepare", False) and len(profiles) > 1:
        raise SystemExit("--no-prepare can only be used with a single --profile")
    return profiles


def create(args: argparse.Namespace) -> int:
    for profile in selected_create_profiles(args):
        build_args = argparse.Namespace(**vars(args))
        build_args.profile = profile.name
        build(build_args)
        if getattr(args, "install_local", False):
            install_args = argparse.Namespace(**vars(args))
            install_args.profile = profile.name
            install_args.kernel = profile_artifact_path(args, profile)
            install_local(install_args)
    return 0


def list_profiles(_: argparse.Namespace) -> int:
    for name in PROFILE_ORDER:
        profile = KERNEL_PROFILES[name]
        marker = " (default)" if name == DEFAULT_PROFILE_NAME else ""
        print(f"{name}{marker}: {profile.description}")
    return 0


def probe_dns(nameservers: list[str]) -> subprocess.CompletedProcess[str]:
    script = "cat /etc/resolv.conf; " + "; ".join(
        f"getent hosts {host}" for host in UBUNTU_DNS_PROBE_HOSTS
    )
    command = [
        "container",
        "run",
        *dns_args(nameservers),
        "--rm",
        UBUNTU_DNS_PROBE_IMAGE,
        "/bin/bash",
        "-lc",
        script,
    ]
    return run_probe(command)


def print_probe_result(title: str, proc: subprocess.CompletedProcess[str]) -> None:
    status = "ok" if proc.returncode == 0 else f"failed ({proc.returncode})"
    print(f"\n== {title}: {status} ==")
    output = proc.stdout.strip()
    error = proc.stderr.strip()
    if output:
        print(output)
    if error:
        print(error)


def diagnose_dns(args: argparse.Namespace) -> int:
    if shutil.which("container") is None:
        raise SystemExit("`container` CLI is required for DNS diagnostics")

    nameservers = build_nameservers(args)
    rendered = ", ".join(nameservers) if nameservers else "none"
    print(f"Host DNS servers discovered: {rendered}")

    print_probe_result("container system status", run_probe(["container", "system", "status"]))
    print_probe_result("container builder status", run_probe(["container", "builder", "status"]))
    print_probe_result("default container DNS probe", probe_dns([]))
    print_probe_result("explicit host DNS probe", probe_dns(nameservers))
    return 0


def ensure_container_system_available() -> None:
    proc = run_probe(["container", "system", "status"])
    if proc.returncode == 0:
        return
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    raise SystemExit(
        "Apple `container` system is not available. Start it with "
        "`container system start`, then rerun the cage-kernel command."
    )


def validate_patch_file() -> None:
    for profile in KERNEL_PROFILES.values():
        for patch in profile.patches:
            text = patch.path.read_text(encoding="utf-8")
            missing = sorted(line for line in patch.required_config_lines if line not in text)
            if missing:
                rendered = ", ".join(missing)
                raise SystemExit(f"{repo_path(patch.path)} is missing required lines: {rendered}")
            if "Sources/Containerization" in text or "Sources/" in text:
                raise SystemExit(
                    f"{repo_path(patch.path)} must be a kernel/config-arm64 patch only"
                )




# -- Release -----------------------------------------------------------------

#: Written beside each built profile kernel: what it was built from. Packaging
#: records this rather than re-resolving sources, which may have moved since.
BUILD_PROVENANCE_NAME = "build.json"


def write_build_provenance(
    kernel: Path,
    *,
    containerization_url: str,
    containerization_revision: str,
    kernel_source_url: str,
) -> None:
    provenance = {
        "containerization": {"url": containerization_url, "revision": containerization_revision},
        "kernel_source_url": kernel_source_url,
    }
    (kernel.parent / BUILD_PROVENANCE_NAME).write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_build_provenance(kernel: Path) -> dict[str, object]:
    path = kernel.parent / BUILD_PROVENANCE_NAME
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"{repo_path(path)} is missing or unreadable; rebuild with "
            "`cage_kernel.py macos create`"
        ) from exc
    if not isinstance(provenance, dict):
        raise SystemExit(f"{repo_path(path)} is not a JSON object")
    return provenance


def release_asset_name(profile: KernelProfile) -> str:
    return f"{PLATFORM}-{ARCHITECTURE}-{profile.name}-vmlinux.zst"


def release_asset_names() -> tuple[str, ...]:
    return tuple(release_asset_name(KERNEL_PROFILES[name]) for name in PROFILE_ORDER)


def package_release(artifact_dir: Path, release_dir: Path) -> dict[str, object]:
    """Write every profile's release asset; return this platform's manifest entry."""
    profiles: dict[str, object] = {}
    provenance: dict[str, object] | None = None
    for name in PROFILE_ORDER:
        profile = KERNEL_PROFILES[name]
        kernel = artifact_dir.resolve(strict=False) / profile.name / "vmlinux"
        verify_kernel(kernel, profile)
        built_from = read_build_provenance(kernel)
        if provenance is None:
            provenance = built_from
        elif built_from != provenance:
            raise SystemExit(
                f"the {profile.name} kernel was built from other sources than "
                f"{PROFILE_ORDER[0]}; rebuild every profile together"
            )
        compressed = release_dir / release_asset_name(profile)
        zstd_compress(kernel, compressed)
        profiles[profile.name] = {
            "description": profile.description,
            "required_config": sorted(profile.required_config_lines),
            "kernel": {"name": compressed.name, **file_record(compressed)},
            "content": file_record(kernel),
        }
    if provenance is None:
        raise SystemExit("the macOS release has no profiles")
    return {"architecture": ARCHITECTURE, **provenance, "profiles": profiles}


# -- Command line ------------------------------------------------------------


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=f"managed checkout directory, default: {repo_path(DEFAULT_WORK_DIR)}",
    )
    parser.add_argument(
        "--containerization-url",
        default=DEFAULT_CONTAINERIZATION_URL,
        help="upstream apple/containerization Git URL",
    )
    parser.add_argument(
        "--containerization-revision",
        default=DEFAULT_CONTAINERIZATION_REVISION,
        help="upstream apple/containerization revision to build",
    )


def add_profile_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=PROFILE_ORDER,
        default=DEFAULT_PROFILE_NAME,
        help=f"kernel profile to use, default: {DEFAULT_PROFILE_NAME}",
    )


def add_artifact_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help=f"directory for created profile artifacts, default: {repo_path(DEFAULT_ARTIFACT_DIR)}",
    )


def add_kernel_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--kernel-source-series",
        default=DEFAULT_KERNEL_SOURCE_SERIES,
        help=(
            "kernel.org stable/longterm series to resolve dynamically, "
            f"default: {DEFAULT_KERNEL_SOURCE_SERIES}"
        ),
    )
    parser.add_argument(
        "--kernel-source-url",
        help=(
            "exact Linux kernel source tarball URL; overrides dynamic "
            "--kernel-source-series resolution"
        ),
    )


def add_install_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--install-path",
        type=Path,
        default=None,
        help=(
            "exact local Cage kernel install path; by default hotplug installs to "
            "this unit’s .local/cage/vmlinux and other profiles install under "
            "this unit’s .local/cage/kernels/<profile>/vmlinux"
        ),
    )


def add_dns_option(parser: argparse.ArgumentParser, *, help: str) -> None:
    parser.add_argument("--dns", action="append", default=[], help=help)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add the `macos` command group."""
    group = subparsers.add_parser("macos", help="macOS ContainerKit arm64 guest kernels")
    commands = group.add_subparsers(dest="macos_command", required=True)

    prepare_command = commands.add_parser("prepare")
    add_common_options(prepare_command)
    add_profile_option(prepare_command)
    prepare_command.set_defaults(handler=prepare)

    build_command = commands.add_parser("build")
    add_common_options(build_command)
    add_profile_option(build_command)
    add_artifact_option(build_command)
    add_install_options(build_command)
    add_kernel_source_options(build_command)
    add_dns_option(
        build_command, help="DNS nameserver IP to pass to Apple container; may be repeated"
    )
    build_command.add_argument(
        "--no-prepare", action="store_true", help="reuse the existing prepared checkout"
    )
    build_command.set_defaults(handler=build)

    verify_parser = commands.add_parser("verify")
    add_common_options(verify_parser)
    add_profile_option(verify_parser)
    add_artifact_option(verify_parser)
    verify_parser.add_argument("--kernel", type=Path, help="kernel image to verify")
    verify_parser.set_defaults(handler=verify)

    install_command = commands.add_parser("install-local")
    add_common_options(install_command)
    add_profile_option(install_command)
    add_artifact_option(install_command)
    add_install_options(install_command)
    install_command.add_argument("--kernel", type=Path, help="kernel image to install")
    install_command.set_defaults(handler=install_local)

    create_command = commands.add_parser("create")
    add_common_options(create_command)
    add_artifact_option(create_command)
    add_install_options(create_command)
    add_kernel_source_options(create_command)
    create_command.add_argument(
        "--profile",
        action="append",
        choices=PROFILE_ORDER,
        dest="profiles",
        default=[],
        help="kernel profile to create; may be repeated; default: all profiles",
    )
    add_dns_option(
        create_command, help="DNS nameserver IP to pass to Apple container; may be repeated"
    )
    create_command.add_argument(
        "--no-prepare",
        action="store_true",
        help="reuse the existing prepared checkout; only valid with one --profile",
    )
    create_command.add_argument(
        "--install-local",
        action="store_true",
        help="install each created profile for local Cage use",
    )
    create_command.set_defaults(handler=create)

    list_command = commands.add_parser("list-profiles")
    list_command.set_defaults(handler=list_profiles)

    diagnose_command = commands.add_parser("diagnose-dns")
    add_dns_option(
        diagnose_command,
        help="DNS nameserver IP to pass to the explicit DNS probe; may be repeated",
    )
    diagnose_command.set_defaults(handler=diagnose_dns)
