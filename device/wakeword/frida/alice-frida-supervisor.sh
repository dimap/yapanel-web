#!/system/bin/sh
# Keep the Frida trigger agent alive in com.yandex.io.sdk.
# Managed by yapanel: scripts live in /data/wakeword/frida.
ALIVE=/data/data/com.yandex.io.sdk/files/alice_frida_alive
TRIGGER=/data/data/com.yandex.io.sdk/files/alice_trigger
INJECT=/data/local/tmp/frida-inject
JS=/data/wakeword/frida/alice_trigger.js

mkdir -p /data/data/com.yandex.io.sdk/files
[ -f "$TRIGGER" ] || echo 0 > "$TRIGGER"
chmod 666 "$TRIGGER" 2>/dev/null

while true; do
  pid=$(pidof com.yandex.io.sdk)
  if [ -n "$pid" ]; then
    now=$(date +%s)
    m=$(stat -c %Y "$ALIVE" 2>/dev/null || echo 0)
    if [ "$m" = "0" ] || [ $((now - m)) -gt 20 ]; then
      "$INJECT" -p "$pid" -s "$JS" -e >> /tmp/alice_frida.log 2>&1
    fi
  fi
  sleep 10
done
