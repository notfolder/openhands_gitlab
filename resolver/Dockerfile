FROM docker.openhands.dev/openhands/openhands:1.5

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# DooD 構成用設定: local_runtime_url を host.docker.internal に変更
COPY config.toml /app/config.toml
