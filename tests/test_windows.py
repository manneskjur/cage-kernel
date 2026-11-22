from __future__ import annotations

import argparse
import gzip
import json
import shutil
from pathlib import Path

import _common
import _windows
import pytest
from fixtures import cpio_payload, fake_rpm

PINS = """schema = 1

[kernel]
name = "kernel"
version = "6.6.121.1"
release = "1.azl3"
arch = "x86_64"
url = "https://packages.example/kernel-6.6.121.1-1.azl3.x86_64.rpm"
sha256 = "{sha256}"
build_time = 1
[sources]
rpm_url = "https://packages.example/kernel.src.rpm"
rpm_sha256 = "{sha256}"
packaging_revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `.local/` root, pins and a fake kernel RPM the download step returns."""
    root = tmp_path / ".local"
    monkeypatch.setattr(_common, "LOCAL_ROOT", root)
    pins = tmp_path / "lcow-kernel.toml"
    pins.write_text(PINS.format(sha256="0" * 64), encoding="utf-8")
    monkeypatch.setattr(_windows, "PINS_PATH", pins)
    # The synthetic kernel is a few bytes; the size floor guards real builds only.
    monkeypatch.setattr(_windows, "MINIMUM_KERNEL_SIZE", 1)
    rpm = tmp_path / "kernel.rpm"
    rpm.write_bytes(fake_rpm(cpio_payload()))
    monkeypatch.setattr(_windows, "_download", lambda url, sha256, destination: rpm)
    return root


def paths(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        work_dir=root / "cage-kernel" / "windows", artifact_dir=root / "cage-kernel" / "lcow"
    )


def test_create_packages_a_payload_that_verifies(local: Path) -> None:
    args = paths(local)
    assert _windows.create(args) == 0
    assert {path.name for path in args.artifact_dir.iterdir()} == _windows.PAYLOAD_FILES
    assert _windows.payload_problems(args.artifact_dir, pins=_windows.load_pins()) == []
    assert _windows.verify(args) == 0


def test_a_payload_from_other_pins_is_refused(local: Path) -> None:
    args = paths(local)
    _windows.create(args)
    other = _windows.load_pins() | {"kernel": {**_windows.load_pins()["kernel"], "version": "x"}}
    assert any("other pins" in error for error in _windows.payload_problems(args.artifact_dir, pins=other))


def test_a_payload_with_an_extra_or_missing_file_is_refused(local: Path) -> None:
    args = paths(local)
    _windows.create(args)
    (args.artifact_dir / "stray").write_bytes(b"")
    assert _windows.payload_problems(args.artifact_dir, pins=_windows.load_pins())
    (args.artifact_dir / "stray").unlink()
    (args.artifact_dir / _windows.MODULES_FILE).unlink()
    assert _windows.payload_problems(args.artifact_dir, pins=_windows.load_pins())


def test_a_modular_feature_without_its_modules_is_refused(local: Path) -> None:
    args = paths(local)
    _windows.create(args)
    manifest_path = args.artifact_dir / _windows.KERNEL_MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["initrd_module_files"] = [
        name for name in manifest["initrd_module_files"] if "/9p/" not in name
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (args.artifact_dir / _windows.CONFIG_FILE).write_text(
        "CONFIG_VSOCKETS=y\nCONFIG_HYPERV_VSOCKETS=y\nCONFIG_9P_FS=m\n", encoding="utf-8"
    )
    errors = _windows.payload_problems(args.artifact_dir, pins=_windows.load_pins())
    assert errors == ["9p is modular but its kernel modules are missing"]


def test_the_kernel_must_have_hyper_v_vsock_built_in(local: Path) -> None:
    args = paths(local)
    _windows.create(args)
    (args.artifact_dir / _windows.CONFIG_FILE).write_text("CONFIG_VSOCKETS=y\n", encoding="utf-8")
    errors = _windows.payload_problems(args.artifact_dir, pins=_windows.load_pins())
    assert errors == ["kernel lacks CONFIG_HYPERV_VSOCKETS=y"]


def test_create_refuses_a_directory_outside_local(local: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"outside \.local"):
        _windows.create(argparse.Namespace(work_dir=tmp_path / "w", artifact_dir=local / "a"))


@pytest.mark.parametrize(
    "replace",
    [
        ("schema = 1", "schema = 2"),
        ('sha256 = "' + "0" * 64 + '"', 'sha256 = ""'),
        ("build_time = 1", 'build_time = "1"'),
    ],
)
def test_pins_without_a_schema_digest_or_typed_field_are_refused(
    local: Path, replace: tuple[str, str]
) -> None:
    text = _windows.PINS_PATH.read_text(encoding="utf-8")
    _windows.PINS_PATH.write_text(text.replace(*replace), encoding="utf-8")
    with pytest.raises(SystemExit):
        _windows.load_pins()


def test_the_checked_in_pins_are_content_addressed() -> None:
    _windows.load_pins()


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd CLI is not installed")
def test_package_release_records_every_asset_digest(local: Path, tmp_path: Path) -> None:
    args = paths(local)
    _windows.create(args)
    release = tmp_path / "release"
    release.mkdir()
    entry = _windows.package_release(args.artifact_dir, release)
    assert {path.name for path in release.iterdir()} == set(_windows.release_asset_names())
    for key in ("kernel", "modules", "config"):
        asset = release / entry[key]["name"]
        assert entry[key]["sha256"] == _common.file_sha256(asset)
    assert entry["content"]["sha256"] == _common.file_sha256(args.artifact_dir / "kernel")
    archived = gzip.decompress((release / entry["modules"]["name"]).read_bytes())
    assert all(name.encode() in archived for name in entry["modules"]["files"])
    assert entry["modular_features"] == []
