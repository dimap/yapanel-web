#!/bin/sh
# Temporary launch.json bind; the read-only firmware is never modified.
set -eu
D=/data/quasar/webpanel/assistant/microphone
TARGET=/system/vendor/quasar/launch.json
M=/tmp/webpanel-mic
mounted() { grep -q " $TARGET " /proc/mounts; }
stop_audio() {
 p=$(pidof quasar_launcher2 || true); [ -z "$p" ] || kill -STOP $p
 p=$(pidof maind || true); [ -z "$p" ] || kill -KILL $p
 p=$(pidof quasar_launcher2 || true); [ -z "$p" ] || kill -KILL $p
 sleep 2
}
start_audio() { /system/vendor/quasar/run.sh; }
restore() {
 rm -f "$M/exclusive" "$D/enabled"
 if mounted && [ -f "$M/mounted" ]; then
  stop_audio
  umount "$TARGET" || { start_audio; return 1; }
  rm -f "$M/mounted"
  start_audio
 fi
}
case "${1:-}" in
 enable|boot)
  # An unrelated bind mount (e.g. a user customization) must never be unmounted.
  if mounted; then [ -f "$M/mounted" ] && exit 0; echo 'launch.json already mounted' >&2;exit 1;fi
  [ -f "$D/mic-tap.so" ] && [ -f "$D/launch.json" ]
  mkdir -p "$M";chmod 700 "$M"
  rm -f "$M/confirmed" "$M/exclusive" "$M/ready"
  mount --bind "$D/launch.json" "$TARGET"
  touch "$M/mounted"
  nohup sh "$D/manage.sh" watchdog >"$D/watchdog.log" 2>&1 </dev/null &
  echo $! >"$M/watchdog.pid"
  stop_audio;start_audio
  ;;
 confirm)
  [ -f "$M/mounted" ] || exit 1
  touch "$M/confirmed" "$D/enabled"
  ;;
 watchdog)
  # Confirmation only follows verified capture. On boot a running receiver
  # refreshes ready, allowing automatic confirmation after the restart.
  sleep 5
  n=0
  while [ "$n" -lt 45 ]; do
   [ -f "$M/confirmed" ] && exit 0
   if [ -f "$M/ready" ] && [ -S "$M/audio.sock" ]; then
    now=$(date +%s);stamp=$(cat "$M/ready" 2>/dev/null || echo 0)
    if [ "$stamp" -le "$now" ] && [ $((now-stamp)) -le 2 ];then touch "$M/confirmed" "$D/enabled";exit 0;fi
   fi
   sleep 1;n=$((n+1))
  done
  restore
  ;;
 disable)
  if [ -f "$M/watchdog.pid" ]; then kill "$(cat "$M/watchdog.pid")" 2>/dev/null || true; fi
  restore
  ;;
 *) echo 'usage: manage.sh enable|boot|confirm|disable' >&2;exit 1 ;;
esac
