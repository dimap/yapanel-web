#!/bin/sh
# Official Zig archive, pinned hashes from ziglang.org/download/index.json.
set -eu
DEST=${1:?destination directory}
case "$(uname -m)" in
 x86_64) ARCH=x86_64; SHA=24aeeec8af16c381934a6cd7d95c807a8cb2cf7df9fa40d359aa884195c4716c ;;
 aarch64|arm64) ARCH=aarch64; SHA=f7a654acc967864f7a050ddacfaa778c7504a0eca8d2b678839c21eea47c992b ;;
 *) echo 'Unsupported build host for Zig' >&2;exit 1 ;;
esac
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL --retry 3 "https://ziglang.org/download/0.14.1/zig-$ARCH-linux-0.14.1.tar.xz" -o "$TMP/zig.tar.xz"
printf '%s  %s\n' "$SHA" "$TMP/zig.tar.xz" | sha256sum -c -
mkdir -p "$DEST"
tar -xJf "$TMP/zig.tar.xz" -C "$DEST" --strip-components=1
