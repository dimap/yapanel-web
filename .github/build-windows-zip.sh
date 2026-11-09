#!/bin/sh
# Готовый ZIP для Windows: панель + портативный Python (python.org, embeddable).
# adb и по желанию ffmpeg start.bat скачает сам при первом запуске (platform-tools Google распространять нельзя).
#   .github/build-windows-zip.sh <версия> [amd64|arm64] [папка для результата]
set -eu
VER="${1:?версия, например 1.0.0}"
ARCH="${2:-amd64}"
OUT="${3:-dist}"
PYVER="3.14.8"   # 3.14+: в стандартной библиотеке есть zstd (анимации часов, заставка)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="yapanel-web-${VER}-windows-$( [ "$ARCH" = amd64 ] && echo x64 || echo arm64 )"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/$NAME/runtime/python" "$OUT"
cd "$ROOT"
cp -r server.py access.py agent.py glagol.py profiles.py max_profile.py max_privacy.py assistant_max.py config.py multi.py pyadb.py dsp_max.py party.py party_store.py party_audio.py speakers.example.json VERSION api static device start.bat install_to_max.bat install_to_max.ps1 README_WINDOWS.txt LICENSE README.md CONTRIBUTION.md "$WORK/$NAME/"
# помощник для колонки (linux/arm64): на Windows Go обычно нет, поэтому кладём готовую сборку
rm -rf "$WORK/$NAME/device/assistant/build"
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go -C device/assistant build -trimpath -ldflags "-s -w -X main.version=$VER" \
  -o "$WORK/$NAME/device/assistant/build/assistantd" .
echo "$VER" > "$WORK/$NAME/device/assistant/build/VERSION"
rm -rf "$WORK/$NAME/device/agent/build"
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go -C device/agent build -trimpath -ldflags "-s -w -X main.version=$VER" \
  -o "$WORK/$NAME/device/agent/build/yapanel-agent" .
echo "$VER" > "$WORK/$NAME/device/agent/build/VERSION"
rm -rf "$WORK/$NAME/device/viz/build"
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go -C device/viz build -trimpath -ldflags "-s -w -X main.version=$VER" \
  -o "$WORK/$NAME/device/viz/build/vizd" .
echo "$VER" > "$WORK/$NAME/device/viz/build/VERSION"
curl -fsSL -o "$WORK/python.zip" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-$ARCH.zip"
unzip -q "$WORK/python.zip" -d "$WORK/$NAME/runtime/python"
# портативный Python не видит папку скрипта — добавляем корень панели (путь относительно ._pth)
for pth in "$WORK/$NAME/runtime/python"/python3*._pth; do printf '..\\..\r\n' >> "$pth"; done
cat > "$WORK/$NAME/runtime/README.txt" <<TXT
runtime — всё, что нужно панели, без установки в систему.
python\  — Python $PYVER (embeddable) с python.org, лицензия PSF: python\LICENSE.txt
platform-tools\, ffmpeg\ — start.bat скачает при первом запуске, если их нет в системе.
TXT
find "$WORK/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} +
(cd "$WORK" && zip -qr9 "$NAME.zip" "$NAME")
mv "$WORK/$NAME.zip" "$OUT/"
echo "$OUT/$NAME.zip"
