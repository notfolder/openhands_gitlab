#!/bin/sh
set -e

CERT_DIR=/tmp/openhands-gitlab-ssl

mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_DIR/server.crt" ]; then
    echo "[ssl-proxy] Generating self-signed certificate for gitlab-ssl-proxy..."
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$CERT_DIR/server.key" \
        -out    "$CERT_DIR/server.crt" \
        -days 3650 \
        -subj "/CN=gitlab-ssl-proxy"
    echo "[ssl-proxy] Certificate generated: $CERT_DIR/server.crt"
else
    echo "[ssl-proxy] Using existing certificate: $CERT_DIR/server.crt"
fi

# テンプレートから nginx 設定を生成（GITLAB_UPSTREAM_URL を展開）
if [ -z "${GITLAB_UPSTREAM_URL:-}" ]; then
    echo "[ssl-proxy] ERROR: GITLAB_UPSTREAM_URL is not set" >&2
    exit 1
fi
echo "[ssl-proxy] Upstream: ${GITLAB_UPSTREAM_URL}"
envsubst '${GITLAB_UPSTREAM_URL}' \
    < /etc/nginx/conf.d/ssl-proxy.conf.template \
    > /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
