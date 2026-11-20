# Cage Kernel

Cage Kernel packages Linux guest kernels for macOS on Apple silicon and for Windows LCOW on amd64.

| Platform | Kernel |
|---|---|
| macOS arm64 | The guest kernel of Apple's [Containerization](https://github.com/apple/containerization) framework in three profiles: `hotplug` (SCSI disk, XHCI, USB mass storage and UAS, so a direct ext4 volume can be hot-plugged into a running virtual machine), `nbd` (`hotplug` plus the guest-side NBD client) and `nbd-cifs` (`nbd` plus SMB/CIFS guest mounts). Every profile requires `CONFIG_VSOCKETS_LOOPBACK` to be unset. |
| Windows amd64 | Microsoft's Azure Linux kernel RPM, pinned by content in `windows/lcow-kernel.toml`, with `CONFIG_VSOCKETS` and `CONFIG_HYPERV_VSOCKETS` built in and the loadable modules an LCOW guest needs (Plan9, nftables, CIFS) with their `modules.dep` closure. |

## Download and verify

Download your platform's assets, `manifest.json` and `SHA256SUMS` from the same [release](https://github.com/manneskjur/cage-kernel/releases). Asset names start with `<platform>-<arch>-`: `macos-arm64-hotplug-vmlinux.zst`, `macos-arm64-nbd-vmlinux.zst`, `macos-arm64-nbd-cifs-vmlinux.zst`, `windows-amd64-lcow-kernel.zst`, `windows-amd64-lcow-modules.cpio.gz` and `windows-amd64-lcow-kernel.config`. For example, check the macOS hotplug kernel in the download directory:

```sh
grep '  macos-arm64-hotplug-vmlinux.zst$' SHA256SUMS | shasum -a 256 -c -
```

`manifest.json` (schema 2) records, for each platform, every asset's digest and the exact sources it was built from: the Containerization revision and Linux source tarball for macOS, and the Azure Linux kernel RPM for Windows.

## Build from source

Install uv. Building the macOS kernels also requires Apple silicon, the Xcode command line tools, Apple's `container` CLI with its service running (`container system start`), and network access to `github.com` and `kernel.org`. The Windows kernel is unpacked from the pinned RPM rather than built, so it can be produced on any host with access to `packages.microsoft.com`. Packaging a release also requires `zstd`.

To rebuild a release, check out its tag and set `KERNEL_SOURCE_URL` to `platforms.macos.kernel_source_url` from that release's `manifest.json`:

```sh
uv run --script scripts/cage_kernel.py macos create --kernel-source-url "$KERNEL_SOURCE_URL"
uv run --script scripts/cage_kernel.py windows create
uv run --script scripts/cage_kernel.py package-release
uv run --script scripts/cage_kernel.py test
```

`macos create` builds every profile; pass `--profile <name>` (repeatable) to build fewer. For each profile it checks out Containerization at the pinned revision `d9868bb657fac3b55ed5dcec97c8eb8a08e78bf5`, applies that profile's `kernel/config-arm64` patches from `macos/patches/`, builds the kernel with the upstream build container and verifies its embedded configuration. `windows create` downloads the RPM pinned in `windows/lcow-kernel.toml`, refuses it unless its SHA-256 matches, and extracts the kernel, its configuration and the module closure. `package-release` packages both platforms into `.local/cage-kernel/release/`. Generated files are written under `.local/`, which git ignores.

Other commands: `macos prepare`, `macos build` and `macos verify` operate on one `--profile`; `macos list-profiles` describes the profiles; `macos diagnose-dns` compares the build container's default DNS with the host's, and `--dns <ip>` passes explicit DNS servers to the build; `windows verify` re-checks a Windows payload; `windows latest` prints the newest Azure Linux kernel RPM's pin values; `release-assets` lists a release's asset names.

## Licensing

See [LICENSE](LICENSE). Linux, Apple's Containerization build files, the Azure Linux kernel package and the upstream portions of the patches keep their own licenses.
