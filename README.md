# Cage Kernel

Cage Kernel packages a Linux guest kernel for macOS on Apple silicon. It builds the guest kernel of Apple's [Containerization](https://github.com/apple/containerization) framework with SCSI disk, XHCI, USB mass storage and UAS support, so that a direct ext4 volume can be hot-plugged into a running virtual machine.

## Download and verify

Download `vmlinux.zst`, `manifest.json` and `SHA256SUMS` from the same [release](https://github.com/manneskjur/cage-kernel/releases) and check them in the download directory:

```sh
shasum -a 256 -c SHA256SUMS
```

`manifest.json` records the Containerization revision, the Linux source and the digest of every asset. Decompress `vmlinux.zst` and give the resulting file to Containerization as the guest kernel.

## Build from source

Building requires macOS on Apple silicon, the Xcode command line tools, Apple's `container` CLI with its service running, `zstd`, uv, and network access to `github.com` and `cdn.kernel.org`.

```sh
uv run --script scripts/cage_kernel.py build
uv run --script scripts/cage_kernel.py verify
uv run --script scripts/cage_kernel.py package
```

`build` clones Containerization at the pinned revision `25558e6b85251104b13d9ae91b5721c071052047` into `.local/cage-kernel/containerization`, applies `patches/containerization-hotplug-guest.patch` to `kernel/config-arm64`, and builds `kernel/vmlinux` with the upstream build container. `verify` checks the kernel's embedded configuration for the hotplug options. `package` writes `vmlinux.zst`, `manifest.json` and `SHA256SUMS` to `dist/`.

If the build container cannot resolve package mirrors, pass explicit DNS servers with `build --dns <ip>`, or run `diagnose-dns` to compare the container's default DNS with the host's.

Stable releases are promoted from a release candidate's assets by the `Bless Release` workflow, without rebuilding.

## Release source archive

The [corresponding source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.1.0-source.tar) contains the Linux source, the effective kernel configuration, Apple's kernel build files, these build scripts and patches, license texts and build instructions. Verify it against [`SOURCE-SHA256SUMS`](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## Licensing

See [LICENSE](LICENSE). Linux, Apple's Containerization build files and the upstream portions of the patches keep their own licenses; the source archive carries their notices.
