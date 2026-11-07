FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && mkdir /data \
 && chown 65532:65532 /data
COPY server.py ./
USER 65532:65532
EXPOSE 18445
VOLUME ["/data"]
ENTRYPOINT ["python", "server.py", "--host", "0.0.0.0", "--key-file", "/data/stt.key", "--download-root", "/data/models"]
CMD ["--model", "base"]
