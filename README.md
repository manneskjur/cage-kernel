# Cage Kernel

Cage Kernel packages Linux guest kernels for macOS on Apple silicon. It builds the guest kernel of Apple's [Containerization](https://github.com/apple/containerization) framework (0.38.0) in three profiles:

| Profile | Kernel |
|---------|--------|
| `hotplug` | The Containerization guest kernel plus SCSI disk, XHCI, USB mass storage and UAS support, so that a direct ext4 volume can be hot-plugged into a running virtual machine. This is the default profile. |
| `nbd` | `hotplug` plus the guest-side NBD client. |
| `nbd-cifs` | `nbd` plus SMB/CIFS guest mounts. |

Every profile also requires `CONFIG_VSOCKETS_LOOPBACK` to be unset. Containerization 0.38.0 already carries the CIFS options, so the CIFS patch is skipped at the pinned revision.

## Download and verify

Each release carries `hotplug-vmlinux.zst`, `nbd-vmlinux.zst`, `nbd-cifs-vmlinux.zst`, `vmlinux.zst` (the same file as `hotplug-vmlinux.zst`), `manifest.json` and `SHA256SUMS`. Download the assets you need and `SHA256SUMS` from the same [release](https://github.com/manneskjur/cage-kernel/releases); for example, check the NBD kernel in the download directory:

```sh
grep '  nbd-vmlinux.zst$' SHA256SUMS | shasum -a 256 -c -
```

`manifest.json` records the Containerization revision, the Linux source URL, each profile's required kernel options and the digest of every asset.

## Build from source

Building requires macOS on Apple silicon, the Xcode command line tools, Apple's `container` CLI with its service running, `zstd`, uv, and network access to `github.com` and `kernel.org`. To rebuild a release, set `KERNEL_SOURCE_URL` to `kernel_source_url` from that release's `manifest.json`; without it the newest non-EOL `6.18` source is resolved from kernel.org.

```sh
uv run --script scripts/cage_kernel.py create --kernel-source-url "$KERNEL_SOURCE_URL"
uv run --script scripts/cage_kernel.py package-release --kernel-source-url "$KERNEL_SOURCE_URL"
uv run --script scripts/cage_kernel.py test
```

`create` builds every profile; pass `--profile <name>` (repeatable) to build fewer. For each profile it checks out Containerization at the pinned revision `d9868bb657fac3b55ed5dcec97c8eb8a08e78bf5`, applies that profile's `kernel/config-arm64` patches from `patches/` (skipping a patch whose options upstream already carries), builds ``kernel/vmlinux-arm64`` with the upstream build container, and verifies the kernel's embedded configuration. `package-release` compresses the profile kernels and writes the release assets.

The script keeps its managed checkout, downloaded sources, profile kernels and packaged release under `.local/cage-kernel/` in the directory three levels above this checkout, so check the repository out at least three directories deep in a workspace you own, for example `~/src/cage-kernel-build/checkouts/v0.3.4/cage-kernel`.

Other commands: `prepare`, `build` and `verify` operate on one `--profile`; `list-profiles` describes the profiles; `diagnose-dns` compares the build container's default DNS with the host's, and `build --dns <ip>` passes explicit DNS servers; `publish --repo <owner>/<name>` packages and uploads a release with the GitHub CLI.

## Release source archive

The [corresponding source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.3.4-source.tar) contains the Linux source, the effective configuration of every profile, Apple's kernel build files, these build scripts and patches, license texts and build instructions. Verify it against [`SOURCE-SHA256SUMS`](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## Licensing

See [LICENSE](LICENSE). Linux, Apple's Containerization build files and the upstream portions of the patches keep their own licenses; the source archive carries their notices.
