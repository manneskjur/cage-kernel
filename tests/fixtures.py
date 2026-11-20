"""Synthetic inputs shared by the cage-kernel tests."""

from __future__ import annotations

import gzip
import lzma
import struct

def align(payload: bytes, boundary: int = 4) -> bytes:
    return payload + (b"\0" * ((-len(payload)) % boundary))


def newc_entry(name: str, *, mode: int = 0o100644, data: bytes = b"") -> bytes:
    encoded_name = name.encode("utf-8") + b"\0"
    fields = [
        1,
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
    return align(header.encode("ascii") + encoded_name) + align(data)


def cpio_payload() -> bytes:
    return b"".join(
        [
            newc_entry("boot/vmlinuz-6.6.121.mshv1-1.azl3", data=b"kernel-bytes"),
            newc_entry(
                "boot/config-6.6.121.mshv1-1.azl3",
                data=b"CONFIG_VSOCKETS=y\nCONFIG_HYPERV_VSOCKETS=y\nCONFIG_HYPERV_VSM=y\n",
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/9p/9p.ko.xz",
                data=lzma.compress(b"9p-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/netfs/netfs.ko.xz",
                data=lzma.compress(b"netfs-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet.ko.xz",
                data=lzma.compress(b"9pnet-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet_fd.ko.xz",
                data=lzma.compress(b"9pnet-fd-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/9p/9pnet_virtio.ko.xz",
                data=lzma.compress(b"9pnet-virtio-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/netfilter/nf_tables.ko.xz",
                data=lzma.compress(b"nf-tables-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/net/netfilter/nft_ct.ko.xz",
                data=lzma.compress(b"nft-ct-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/smb/client/cifs.ko.xz",
                data=lzma.compress(b"cifs-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/fs/nls/nls_ucs2_utils.ko.xz",
                data=lzma.compress(b"nls-ucs2-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/kernel/crypto/cmac.ko.xz",
                data=lzma.compress(b"cmac-module"),
            ),
            newc_entry(
                "lib/modules/6.6.121.mshv1-1.azl3/modules.dep",
                data=(
                    b"kernel/fs/9p/9p.ko: kernel/net/9p/9pnet.ko kernel/fs/netfs/netfs.ko\n"
                    b"kernel/fs/smb/client/cifs.ko: kernel/fs/nls/nls_ucs2_utils.ko "
                    b"kernel/fs/netfs/netfs.ko\n"
                ),
            ),
            newc_entry("TRAILER!!!", mode=0, data=b""),
        ]
    )


def rpm_header(strings: dict[int, str]) -> bytes:
    entries: list[tuple[int, int]] = []
    store = b""
    for tag, value in strings.items():
        entries.append((tag, len(store)))
        store += value.encode("utf-8") + b"\0"
    header = b"\x8e\xad\xe8\x01" + b"\0" * 4
    header += struct.pack(">II", len(entries), len(store))
    for tag, offset in entries:
        header += struct.pack(">IIII", tag, 6, offset, 1)
    return header + store


def fake_rpm(payload: bytes) -> bytes:
    lead = b"\xed\xab\xee\xdb" + (b"\0" * 92)
    signature = align(rpm_header({}), 8)
    header = rpm_header({1124: "cpio", 1125: "gzip"})
    return lead + signature + header + gzip.compress(payload)
