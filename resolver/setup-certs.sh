#!/bin/bash
set -e

# SSL_CERT_FILE に GitLab 自己署名証明書が設定されている場合、
# certifi の CA バンドルと結合して外部 HTTPS 接続(OpenAI 等)を壊さないようにする
if [ -n "$SSL_CERT_FILE" ] && [ -f "$SSL_CERT_FILE" ]; then
    SYS_CA=$(python3 -c "import certifi; print(certifi.where())" 2>/dev/null \
             || echo "/etc/ssl/certs/ca-certificates.crt")
    if [ -f "$SYS_CA" ]; then
        COMBINED=/tmp/combined-ca-bundle.crt
        cat "$SYS_CA" "$SSL_CERT_FILE" > "$COMBINED"
        export SSL_CERT_FILE="$COMBINED"
        export REQUESTS_CA_BUNDLE="$COMBINED"
    fi
fi

exec /app/entrypoint.sh "$@"
