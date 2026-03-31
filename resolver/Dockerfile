FROM docker.openhands.dev/openhands/openhands:1.5

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# DooD 構成用設定: local_runtime_url を host.docker.internal に変更
COPY config.toml /app/config.toml

# SSL_CERT_FILE(GitLab 自己署名証明書)とシステム CA バンドルを結合して
# OpenAI 等の外部 HTTPS 接続を壊さないようにするラッパーエントリーポイント
COPY setup-certs.sh /app/setup-certs.sh
RUN chmod +x /app/setup-certs.sh
ENTRYPOINT ["/app/setup-certs.sh"]
