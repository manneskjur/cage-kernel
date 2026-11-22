from __future__ import annotations

import gzip
import json
from pathlib import Path

import _lcow_rpm
import pytest
from fixtures import cpio_payload, fake_rpm


def test_select_kernel_package_from_primary_xml_uses_latest_build_time() -> None:
    module = _lcow_rpm
    primary = b"""<?xml version="1.0" encoding="UTF-8"?>
    <metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">
      <package type="rpm">
        <name>kernel-mshv</name><arch>x86_64</arch>
        <version epoch="0" ver="6.6.100.mshv1" rel="1.azl3"/>
        <checksum type="sha256">aaa</checksum>
        <time file="1" build="100"/>
        <location href="Packages/k/kernel-mshv-6.6.100.mshv1-1.azl3.x86_64.rpm"/>
      </package>
      <package type="rpm">
        <name>kernel-mshv</name><arch>x86_64</arch>
        <version epoch="0" ver="6.6.121.mshv1" rel="1.azl3"/>
        <checksum type="sha256">bbb</checksum>
        <time file="2" build="200"/>
        <location href="Packages/k/kernel-mshv-6.6.121.mshv1-1.azl3.x86_64.rpm"/>
      </package>
    </metadata>
    """

    package = module._select_kernel_package_from_primary_xml(
        gzip.compress(primary),
        package_name="kernel-mshv",
        arch="x86_64",
    )

    assert package.version == "6.6.121.mshv1"
    assert package.checksum == "sha256:bbb"
    assert package.rpm_name == "kernel-mshv-6.6.121.mshv1-1.azl3.x86_64.rpm"


def test_extract_boot_kernel_files_from_rpm_reads_kernel_and_config(tmp_path: Path) -> None:
    module = _lcow_rpm
    rpm = tmp_path / "kernel-mshv.rpm"
    rpm.write_bytes(fake_rpm(cpio_payload()))

    files = module.extract_boot_kernel_files_from_rpm(rpm)

    assert files.kernel_name == "boot/vmlinuz-6.6.121.mshv1-1.azl3"
    assert files.kernel == b"kernel-bytes"
    assert files.config_name == "boot/config-6.6.121.mshv1-1.azl3"
    assert module._validate_kernel_config(files.config, module.DEFAULT_CONFIG_REQUIREMENTS) == []


def test_kernel_config_validation_rejects_invalid_utf8() -> None:
    module = _lcow_rpm

    with pytest.raises(RuntimeError, match="kernel configuration is not valid UTF-8"):
        module._validate_kernel_config(b"CONFIG_VSOCKETS=y\n\xff", ("CONFIG_VSOCKETS=y",))
    with pytest.raises(RuntimeError, match="kernel configuration is not valid UTF-8"):
        module._kernel_config_warnings(b"CONFIG_HYPERV_VSM=y\n\xff")


def test_package_kernel_writes_hcsshim_boot_file_shape(tmp_path: Path) -> None:
    module = _lcow_rpm
    rpm = tmp_path / "kernel-mshv.rpm"
    out = tmp_path / "LinuxBootFiles"
    rpm.write_bytes(fake_rpm(cpio_payload()))

    kernel = module.package_kernel(
        rpm_path=rpm,
        output_dir=out,
        package=None,
        force=False,
        requirements=module.DEFAULT_CONFIG_REQUIREMENTS,
    )

    assert kernel == out / "kernel"
    assert (out / "kernel").read_bytes() == b"kernel-bytes"
    assert b"CONFIG_HYPERV_VSOCKETS=y" in (out / "kernel.config").read_bytes()
    module_archive = out / "kernel-modules.cpio.gz"
    assert module_archive.is_file()
    module_names = [
        name
        for name, _data in module._iter_cpio_newc(gzip.decompress(module_archive.read_bytes()))
    ]
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/9p/9p.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/netfs/netfs.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet_fd.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet_virtio.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/netfilter/nf_tables.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/netfilter/nft_ct.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/smb/client/cifs.ko" in module_names
    # This dependency is outside the configured selection patterns and proves
    # that the packager follows modules.dep rather than relying on a brittle list.
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/nls/nls_ucs2_utils.ko" in module_names
    assert "lib/modules/6.6.121.mshv1-1.azl3/kernel/crypto/cmac.ko" in module_names
    manifest = json.loads((out / "lcow-kernel-manifest.json").read_text(encoding="utf-8"))
    assert manifest["output_files"]["initrd_modules"] == "kernel-modules.cpio.gz"
    assert manifest["warnings"] == [
        (
            "This Azure Linux kernel enables Hyper-V VSM. Use a kernel compatible with your LCOW host."
        )
    ]
