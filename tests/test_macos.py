from __future__ import annotations

import argparse
import gzip
import subprocess
from pathlib import Path

import _macos
import pytest

SCUTIL_OUTPUT = """
resolver #1
  nameserver[0] : 10.0.0.1
  nameserver[1] : 127.0.0.1
resolver #2
  nameserver[0] : 10.0.0.1
  nameserver[1] : fe80::1
  nameserver[2] : 2001:4860:4860::8888
"""


def synthetic_kernel(profile: _macos.KernelProfile, *, form: str = "plain") -> bytes:
    config = "\n".join(["# CONFIG_TEST=y", *sorted(profile.required_config_lines), ""]) + "\n"
    if form == "plain":
        return b"prefix IKCFG_ST" + config.encode() + b"IKCFG_ED suffix"
    if form == "compressed":
        return b"prefix IKCFG_ST" + gzip.compress(config.encode()) + b"IKCFG_ED suffix"
    return b"prefix" + gzip.compress(config.encode()) + b"suffix"


def test_patches_carry_their_profile_config_and_touch_only_the_kernel_config() -> None:
    _macos.validate_patch_file()


def test_nameservers_are_parsed_and_loopback_and_link_local_are_dropped() -> None:
    parsed = _macos.parse_scutil_nameservers(SCUTIL_OUTPUT)
    assert parsed == ["10.0.0.1", "127.0.0.1", "10.0.0.1", "fe80::1", "2001:4860:4860::8888"]
    assert _macos.sanitize_nameservers(parsed) == ["10.0.0.1", "2001:4860:4860::8888"]


def test_container_service_failures_are_told_apart_from_dns_failures() -> None:
    service = subprocess.CalledProcessError(
        1, ["container", "build"], stderr='Error: interrupted: "XPC connection error"\n'
    )
    dns = subprocess.CalledProcessError(
        1, ["container", "build"], stderr="Temporary failure resolving 'ports.ubuntu.com'\n"
    )
    assert _macos.is_container_service_failure(service)
    assert not _macos.is_container_service_failure(dns)


def test_kernel_source_prefers_the_latest_non_eol_release_then_its_snapshot() -> None:
    def release(version: str, *, eol: bool = False, moniker: str = "longterm") -> dict:
        return {
            "moniker": moniker,
            "version": version,
            "iseol": eol,
            "source": f"https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-{version}.tar.xz",
        }

    releases = [
        release("6.18.36"),
        release("6.18.37"),
        release("6.18.99-rc1", moniker="mainline"),
        release("6.18.38", eol=True),
    ]
    assert _macos.kernel_source_candidates(releases, "6.18")[:2] == [
        "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.37.tar.xz",
        _macos.kernel_snapshot_url("6.18.37"),
    ]


def test_create_builds_every_profile_unless_one_is_named() -> None:
    selected = _macos.selected_create_profiles(argparse.Namespace(profiles=[]))
    assert [profile.name for profile in selected] == list(_macos.PROFILE_ORDER)
    with pytest.raises(SystemExit, match="--no-prepare"):
        _macos.selected_create_profiles(argparse.Namespace(profiles=[], no_prepare=True))


@pytest.mark.parametrize("name", _macos.PROFILE_ORDER)
def test_every_profile_requires_hotplug_and_forbids_vsock_loopback(name: str) -> None:
    lines = _macos.KERNEL_PROFILES[name].required_config_lines
    assert lines >= _macos.HOTPLUG_CONFIG_LINES
    assert lines >= _macos.RUNTIME_SECURITY_CONFIG_LINES
    assert (lines >= _macos.NBD_TRANSPORT_CONFIG_LINES) == (name != "hotplug")


@pytest.mark.parametrize("form", ["plain", "compressed", "stream"])
def test_embedded_kernel_config_is_found_in_every_encoding(tmp_path: Path, form: str) -> None:
    profile = _macos.KERNEL_PROFILES["nbd-cifs"]
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(synthetic_kernel(profile, form=form))
    _macos.verify_kernel(kernel, profile)


def test_a_kernel_missing_profile_config_is_refused(tmp_path: Path) -> None:
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(synthetic_kernel(_macos.KERNEL_PROFILES["hotplug"]))
    with pytest.raises(SystemExit):
        _macos.verify_kernel(kernel, _macos.KERNEL_PROFILES["nbd-cifs"])


def test_release_asset_names_name_the_platform_and_architecture() -> None:
    assert _macos.release_asset_names() == tuple(
        f"macos-arm64-{name}-vmlinux.zst" for name in _macos.PROFILE_ORDER
    )


def test_the_managed_checkout_stays_inside_local(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"outside \.local"):
        _macos.ensure_safe_work_dir(tmp_path)
