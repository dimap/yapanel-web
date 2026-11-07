#!/bin/sh
# Zig 0.14.1+ supplies an ARM32 glibc sysroot; no firmware SDK required.
set -eu
cd "$(dirname "$0")"
mkdir -p build
zig cc -target arm-linux-gnueabihf.2.17 -shared -fPIC -O2 -Wall -Wextra -Werror \
    -Wl,-z,relro,-z,now,--strip-all -o build/mic-tap.so tap.c -ldl

cat ../../VERSION > build/VERSION
