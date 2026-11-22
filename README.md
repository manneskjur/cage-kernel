![Cage Kernel](cage-kernel.jpg)

# Cage Kernel

Cage Kernel packages Linux guest kernels for macOS on Apple silicon and Windows LCOW on amd64. Choose the release that matches your guest filesystem. macOS assets provide hotplug, NBD, and NBD with CIFS profiles. Windows assets include the kernel, its configuration, and its loadable modules.

## Download and verify

Download your platform assets, `manifest.json`, and `SHA256SUMS` from the same [release](https://github.com/manneskjur/cage-kernel/releases). For example, on macOS check the hotplug kernel from v0.6.0 in the download directory:

```sh
grep '  macos-arm64-hotplug-vmlinux.zst$' SHA256SUMS | shasum -a 256 -c -
```

Verify the checksum file against your application's trusted release pin when installing a managed guest.

## Build from source

Install uv. Building macOS kernels also requires Apple silicon, Apple's container CLI with its service running, and network access. Check out the release tag. Set `KERNEL_SOURCE_URL` to `platforms.macos.kernel_source_url` from that release’s manifest.

```sh
uv run --script scripts/cage_kernel.py macos create --kernel-source-url "$KERNEL_SOURCE_URL"
uv run --script scripts/cage_kernel.py windows create
uv run --script scripts/cage_kernel.py test
```

The Windows command extracts the content-pinned Azure Linux RPM. Use the modules from that release when assembling a Windows initrd.

## Release source archive

Extract `source-and-notices.tar` to get the inputs used for that release. For a macOS build, unpack `containerization/<profile>.tar`, copy `linux/source.tar.xz` to its `kernel/` directory and run `make -C kernel GIT_VERSION=<revision>` on Apple silicon. Use the first 12 characters of the manifest’s Containerization revision. The profile archive contains the prepared build configuration. The final embedded configurations are in `configs/`.

For Windows, `azure-linux/` contains the source RPM, effective configuration and Azure Linux’s signed-package recipe. The source RPM contains the upstream source, distribution patches and RPM build specification. Use the Azure Linux 3.0 RPM build environment.

## Licensing and contributions

Independently authored build tooling and documentation are [MIT licensed](LICENSE). Upstream components and their patches retain their applicable licenses. See the release notes for corresponding source and notices.

Public pull requests are welcome. For security reports, follow the [private reporting instructions](SECURITY.md).
