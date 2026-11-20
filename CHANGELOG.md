# Release notes

## v0.6.0

Cage guest kernels for every platform.

- macOS ContainerKit arm64: the hotplug, nbd and nbd-cifs profiles of the apple/containerization guest kernel.
- Windows LCOW amd64: the Azure Linux kernel, its configuration and the modules the LCOW guest loads.

`manifest.json` names every asset's digest and the exact sources each platform was built from: the apple/containerization revision and Linux source tarball, and the Azure Linux kernel RPM.

## v0.3.4

Cage ContainerKit guest kernels rebased on Apple Containerization 0.38.0. All profiles retain hotplug support, NBD/CIFS profiles remain available, and CONFIG_VSOCKETS_LOOPBACK is disabled.

Corresponding source, kernel configuration, build scripts, and license texts are available in the [source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.3.4-source.tar). [Source checksums](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## v0.3.2

Cage ContainerKit guest kernels: hotplug, nbd, and nbd-cifs profiles.

Corresponding source, kernel configuration, build scripts, and license texts are available in the [source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.3.2-source.tar). [Source checksums](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## v0.1.0

macOS arm64 guest kernel with direct-volume hotplug support.

Corresponding source, kernel configuration, build scripts, and license texts are available in the [source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.1.0-source.tar). [Source checksums](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## v0.1.0-rc.2

Release candidate for the macOS arm64 guest kernel with direct-volume hotplug support.

Corresponding source, kernel configuration, build scripts, and license texts are available in the [source archive](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/cage-kernel-v0.1.0-source.tar). [Source checksums](https://github.com/manneskjur/cage-kernel/releases/download/sources-2026-11-21/SOURCE-SHA256SUMS).

## v0.1.0-rc.1

Release candidate for the macOS arm64 guest kernel with direct-volume hotplug support.
