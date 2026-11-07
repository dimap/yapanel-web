#!/system/bin/sh
# Stage wakeword runtime files from /data/yapanel/device/wakeword.
# Runs as root from install_to_max.ps1 after the panel archive is unpacked.
set -e
SRC="${1:-/data/yapanel/device/wakeword}"
DATA=/data/wakeword

mkdir -p "$DATA/lib" "$DATA/frida" /data/local/tmp

if [ -f "$SRC/build/wakeword" ]; then
  cp -f "$SRC/build/wakeword" "$DATA/wakeword"
  chmod 755 "$DATA/wakeword"
fi
if [ -f "$SRC/negatives.bin" ]; then
  cp -f "$SRC/negatives.bin" "$DATA/negatives.bin"
fi
if [ -f "$SRC/lib/audiocapture.jar" ]; then
  cp -f "$SRC/lib/audiocapture.jar" "$DATA/lib/audiocapture.jar"
fi
if [ -f "$SRC/frida/alice_trigger.js" ]; then
  cp -f "$SRC/frida/alice_trigger.js" "$DATA/frida/alice_trigger.js"
  chmod 644 "$DATA/frida/alice_trigger.js"
fi
if [ -f "$SRC/frida/alice-frida-supervisor.sh" ]; then
  cp -f "$SRC/frida/alice-frida-supervisor.sh" "$DATA/frida/alice-frida-supervisor.sh"
  chmod 755 "$DATA/frida/alice-frida-supervisor.sh"
fi
if [ -f "$SRC/bin/frida-inject" ]; then
  cp -f "$SRC/bin/frida-inject" /data/local/tmp/frida-inject
  chmod 755 /data/local/tmp/frida-inject
fi
if [ -f "$SRC/build/VERSION" ]; then
  cp -f "$SRC/build/VERSION" "$DATA/VERSION"
fi
[ -f "$DATA/VERSION" ] || echo 0.3.0-maxpkg > "$DATA/VERSION"

# Trigger file must be writable by the SDK app: the Frida agent resets 1 -> 0.
T=/data/data/com.yandex.io.sdk/files/alice_trigger
mkdir -p /data/data/com.yandex.io.sdk/files
[ -f "$T" ] || echo 0 > "$T"
chmod 666 "$T" 2>/dev/null || true

exit 0
