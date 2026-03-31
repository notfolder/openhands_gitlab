FROM python:3.12-slim

# docker CLI をインストール（DooD: ホストのDockerデーモンにコマンドを発行するため）
# docker.io は Debian パッケージの競合でバイナリが配置されないケースがあるため
# Docker 公式の static バイナリを直接取得する
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && DOCKER_VERSION=$(curl -fsSL https://download.docker.com/linux/static/stable/x86_64/ \
        | grep -oP 'docker-\K[0-9]+\.[0-9]+\.[0-9]+' | sort -V | tail -1) \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
        | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "300", "app:app"]
