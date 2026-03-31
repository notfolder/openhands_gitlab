"""
OpenHands Webhook Receiver for self-hosted GitLab
--------------------------------------------------
GitLab の Issue / MR / Note イベントを受け取り、
トリガー条件に一致したら OpenHands Resolver を Docker コンテナとして起動する。

DooD (Docker outside of Docker) 構成:
  - このコンテナは /var/run/docker.sock をマウントしている
  - Resolver 起動時に /tmp/openhands-resolver-workspace をホスト共有パスとして使う
"""

import hmac
import logging
import os
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

# ─── ログ設定 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── 環境変数 ─────────────────────────────────────────────────────────────────
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
GITLAB_USERNAME = os.environ.get("GITLAB_USERNAME", "openhands")
GITLAB_BASE_URL = os.environ.get("GITLAB_BASE_URL", "http://host.docker.internal:8080")
GIT_BASE_DOMAIN = os.environ.get("GIT_BASE_DOMAIN", "host.docker.internal:8080")
# ローカル GitLab 用の自己署名証明書パス（ホスト上のパス）
# setup.sh がローカル GitLab を検出した場合に自動設定する。
# 外部 HTTPS GitLab の場合は空のままでよい。
GITLAB_SSL_CERT = os.environ.get("GITLAB_SSL_CERT", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4.1")
# OpenAI 互換 API (LiteLLM proxy 等) を使う場合に設定。空なら OpenAI 直接接続。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TRIGGER_LABEL = os.environ.get("TRIGGER_LABEL", "openhands")
TRIGGER_MENTION = os.environ.get("TRIGGER_MENTION", "@openhands")
RESOLVER_NETWORK = os.environ.get("RESOLVER_NETWORK", "openhands-net")
OPENHANDS_IMAGE = os.environ.get(
    "OPENHANDS_IMAGE", "docker.openhands.dev/openhands/openhands:1.5"
)
AGENT_SERVER_IMAGE_TAG = os.environ.get("AGENT_SERVER_IMAGE_TAG", "1.12.0-python")
RESOLVER_WORKSPACE_HOST_PATH = os.environ.get(
    "RESOLVER_WORKSPACE_HOST_PATH", "/tmp/openhands-resolver-workspace"
)


# ─── 認証 ──────────────────────────────────────────────────────────────────────
def verify_gitlab_token(request_token: str) -> bool:
    """GitLab Webhook の X-Gitlab-Token ヘッダーを検証する。"""
    if not WEBHOOK_SECRET:
        return True  # シークレット未設定の場合はスキップ
    return hmac.compare_digest(request_token, WEBHOOK_SECRET)


# ─── Resolver 起動 ───────────────────────────────────────────────────────────
def run_resolver(repo_path: str, issue_number: int, issue_type: str = "issue") -> None:
    """
    OpenHands Resolver を Docker コンテナとして起動する。

    DooD 構成のため、Workspace は /tmp/openhands-resolver-workspace/<uuid> を使う。
    このパスはホストの Docker デーモンから見たパスと一致している必要がある。
    """
    run_id = uuid.uuid4().hex[:8]
    workspace_path = Path(RESOLVER_WORKSPACE_HOST_PATH) / f"{issue_number}-{run_id}"
    workspace_path.mkdir(parents=True, exist_ok=True)

    container_name = f"openhands-resolver-{issue_number}-{run_id}"
    logger.info(
        "Starting resolver: repo=%s issue=%s type=%s workspace=%s",
        repo_path,
        issue_number,
        issue_type,
        workspace_path,
    )

    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        # Agent Server
        "-e", "AGENT_SERVER_IMAGE_REPOSITORY=ghcr.io/openhands/agent-server",
        "-e", f"AGENT_SERVER_IMAGE_TAG={AGENT_SERVER_IMAGE_TAG}",
        # Workspace (DooD: ホストパスを指定)
        "-e", f"WORKSPACE_BASE={workspace_path}",
        "-e", "WORKSPACE_MOUNT_PATH=/workspace",
        # Docker socket (サンドボックスコンテナ起動のため)
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        # Workspace マウント (ホストパス = コンテナ内パスで揃えている)
        "-v", f"{workspace_path}:{workspace_path}",
        # ローカル GitLab 用の自己署名証明書（設定されている場合のみ）
        # SSL_CERT_FILE: Python httpx が信頼する CA 証明書
        # GIT_SSL_CAINFO: git clone/push が信頼する CA 証明書
        *((["-v", f"{GITLAB_SSL_CERT}:{GITLAB_SSL_CERT}:ro",
            "-e", f"SSL_CERT_FILE={GITLAB_SSL_CERT}",
            "-e", f"GIT_SSL_CAINFO={GITLAB_SSL_CERT}"]
           ) if GITLAB_SSL_CERT else []),
        # ネットワーク (GitLab と同一ネットワークで名前解決)
        "--network", RESOLVER_NETWORK,
        "--add-host", "host.docker.internal:host-gateway",
        # DooD: Resolver コンテナ内の localhost は Docker ホストではないため、
        # OpenHands が Runtime コンテナに接続する URL を host.docker.internal に変更する。
        # Runtime のポートはホストにパブリッシュされるので、host.docker.internal 経由でアクセスできる。
        "-e", "SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal",
        OPENHANDS_IMAGE,
        "python", "-m", "openhands.resolver.resolve_issue",
        "--selected-repo", repo_path,
        "--issue-number", str(issue_number),
        "--issue-type", issue_type,
        "--output-dir", str(workspace_path),
        "--token", GITLAB_TOKEN,
        "--username", GITLAB_USERNAME,
        "--base-domain", GIT_BASE_DOMAIN,
        "--llm-model", LLM_MODEL,
        "--llm-api-key", LLM_API_KEY,
        # LiteLLM proxy 等 OpenAI 互換 API の場合のみ設定（空なら OpenAI 直接）
        *(["--llm-base-url", LLM_BASE_URL] if LLM_BASE_URL else []),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 最大1時間
        )
        if result.returncode == 0:
            logger.info("Resolver finished successfully: container=%s", container_name)
        else:
            logger.error(
                "Resolver failed: container=%s returncode=%s\nstdout=%s\nstderr=%s",
                container_name,
                result.returncode,
                result.stdout[-2000:],
                result.stderr[-2000:],
            )
    except subprocess.TimeoutExpired:
        logger.error("Resolver timed out: container=%s", container_name)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    except Exception:
        logger.exception("Unexpected error running resolver: container=%s", container_name)


def trigger_resolver_async(repo_path: str, issue_number: int, issue_type: str) -> None:
    """バックグラウンドスレッドで Resolver を起動する。"""
    thread = threading.Thread(
        target=run_resolver,
        args=(repo_path, issue_number, issue_type),
        daemon=True,
    )
    thread.start()


# ─── Webhook エンドポイント ───────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    # 認証チェック
    gitlab_token = request.headers.get("X-Gitlab-Token", "")
    if not verify_gitlab_token(gitlab_token):
        logger.warning("Webhook authentication failed")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    event_type = request.headers.get("X-Gitlab-Event", "")
    repo_path = data.get("project", {}).get("path_with_namespace", "")

    if not repo_path:
        return jsonify({"status": "ignored", "reason": "no repo path"}), 200

    # ─── Issue Hook ─────────────────────────────────────────────────────────
    if event_type == "Issue Hook":
        obj = data.get("object_attributes", {})
        action = obj.get("action", "")
        issue_number = obj.get("iid")
        current_labels = [label["title"] for label in data.get("labels", [])]

        if issue_number and TRIGGER_LABEL in current_labels and action in (
            "open", "reopen", "update"
        ):
            logger.info(
                "Issue trigger: repo=%s issue=%s labels=%s",
                repo_path,
                issue_number,
                current_labels,
            )
            trigger_resolver_async(repo_path, issue_number, "issue")
            return jsonify({"status": "triggered", "issue": issue_number})

    # ─── Note Hook (Issue / MR コメント) ────────────────────────────────────
    elif event_type == "Note Hook":
        obj = data.get("object_attributes", {})
        note_body = obj.get("note", "")
        noteable_type = obj.get("noteable_type", "")

        if TRIGGER_MENTION in note_body:
            if noteable_type == "Issue":
                issue_number = data.get("issue", {}).get("iid")
                if issue_number:
                    logger.info(
                        "Issue comment trigger: repo=%s issue=%s",
                        repo_path,
                        issue_number,
                    )
                    trigger_resolver_async(repo_path, issue_number, "issue")
                    return jsonify({"status": "triggered", "issue": issue_number})

            elif noteable_type == "MergeRequest":
                mr_number = data.get("merge_request", {}).get("iid")
                if mr_number:
                    logger.info(
                        "MR comment trigger: repo=%s mr=%s",
                        repo_path,
                        mr_number,
                    )
                    trigger_resolver_async(repo_path, mr_number, "pr")
                    return jsonify({"status": "triggered", "mr": mr_number})

    # ─── Merge Request Hook ──────────────────────────────────────────────────
    elif event_type == "Merge Request Hook":
        obj = data.get("object_attributes", {})
        action = obj.get("action", "")
        mr_number = obj.get("iid")
        current_labels = [label["title"] for label in data.get("labels", [])]

        if mr_number and TRIGGER_LABEL in current_labels and action in (
            "open", "reopen", "update"
        ):
            logger.info(
                "MR label trigger: repo=%s mr=%s", repo_path, mr_number
            )
            trigger_resolver_async(repo_path, mr_number, "pr")
            return jsonify({"status": "triggered", "mr": mr_number})

    return jsonify({"status": "ignored"})


# ─── ヘルスチェック ───────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    missing = []
    if not GITLAB_TOKEN:
        missing.append("GITLAB_TOKEN")
    if not LLM_API_KEY:
        missing.append("LLM_API_KEY")

    if missing:
        return jsonify({"status": "warning", "missing_env": missing}), 200

    return jsonify({
        "status": "ok",
        "trigger_label": TRIGGER_LABEL,
        "trigger_mention": TRIGGER_MENTION,
        "llm_model": LLM_MODEL,
        "llm_base_url": LLM_BASE_URL or "(OpenAI direct)",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
