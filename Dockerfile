# Веб-панель для Яндекс Станций: Python (только стандартная библиотека) + adb + ffmpeg + openssl.

# Помощник для колонки (device/assistant) — статическая сборка под linux/arm64; Go в итоговом образе не нужен.
FROM golang:1.26-alpine AS assistant
WORKDIR /src
COPY device/assistant/ ./
COPY VERSION /VERSION
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w -X main.version=$(cat /VERSION)" -o /out/assistantd . \
 && cp /VERSION /out/VERSION
# агент панели (device/agent) — так же
WORKDIR /agent
COPY device/agent/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w -X main.version=$(cat /VERSION)" -o /out-agent/yapanel-agent . \
  && cp /VERSION /out-agent/VERSION
# живой спектр (device/viz) — так же
WORKDIR /viz
COPY device/viz/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w -X main.version=$(cat /VERSION)" -o /out-viz/vizd . \
  && cp /VERSION /out-viz/VERSION

# ARM32 userspace library for the microphone tap (the daemon itself is ARM64).
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS microphone
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY .github/setup-zig.sh /tmp/setup-zig.sh
RUN sh /tmp/setup-zig.sh /opt/zig
ENV PATH="/opt/zig:${PATH}"
COPY device/mic-tap /src/device/mic-tap
COPY VERSION /src/VERSION
RUN sh /src/device/mic-tap/build.sh

FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb ffmpeg openssl ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py access.py agent.py glagol.py profiles.py midi.py managed_yam.py model_colors.py zvuk.py zvuk_queries.py zvuk_player.py multi.py pyadb.py speakers.example.json VERSION ./
COPY static ./static
COPY device ./device
COPY --from=assistant /out/assistantd /out/VERSION ./device/assistant/build/
COPY --from=assistant /out-agent/yapanel-agent /out-agent/VERSION ./device/agent/build/
COPY --from=assistant /out-viz/vizd /out-viz/VERSION ./device/viz/build/
COPY --from=microphone /src/device/mic-tap/build/mic-tap.so /src/device/mic-tap/build/VERSION ./device/mic-tap/build/
COPY LICENSE ./

# /data — токен Яндекса, пароль панели, сертификаты, история графиков и ключ adb (HOME).
# Свой порт adb-сервера, чтобы при network_mode: host не мешать adb на самой машине.
ENV PANEL_HOST=0.0.0.0 \
    PANEL_INSTALL=docker \
    PANEL_DATA_DIR=/data \
    HOME=/data \
    ANDROID_ADB_SERVER_PORT=5038 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765 8766 8767

CMD ["python", "server.py"]
