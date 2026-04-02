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
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
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
OPENHANDS_LOG_DIR = os.environ.get("OPENHANDS_LOG_DIR", "/tmp/openhands-logs")
MAX_ITERATIONS = os.environ.get("MAX_ITERATIONS", "30")
PROMPT_FILE = os.environ.get("PROMPT_FILE", "") or "/app/openhands/resolver/prompts/resolve/basic.jinja"


# ─── 認証 ──────────────────────────────────────────────────────────────────────
def verify_gitlab_token(request_token: str) -> bool:
    """GitLab Webhook の X-Gitlab-Token ヘッダーを検証する。"""
    if not WEBHOOK_SECRET:
        return True  # シークレット未設定の場合はスキップ
    return hmac.compare_digest(request_token, WEBHOOK_SECRET)


# ─── クリーンアップヘルパー ────────────────────────────────────────────────────
def _save_log(container_name: str, lines: list[str]) -> Path | None:
    """コンテナのログをファイルに保存する。失敗してもエラーはログのみ。"""
    try:
        log_dir = Path(OPENHANDS_LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{container_name}.log"
        log_file.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Log saved: %s", log_file)
        return log_file
    except Exception:
        logger.warning("Failed to save log: container=%s", container_name, exc_info=True)
        return None


def _format_log_detail(lines: list[str], n: int = 200) -> str:
    """ログの末尾 n 行を GitLab Markdown の <details> ブロックとして返す。"""
    tail = lines[-n:] if len(lines) > n else lines
    summary = "\n".join(tail)
    return (
        f"\n\n<details><summary>ログ末尾（最大{n}行）</summary>\n\n"
        f"```\n{summary}\n```\n\n</details>"
    )


def _get_runtime_containers() -> set:
    """現在存在する openhands-runtime-* コンテナ名を取得する。"""
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=openhands-runtime-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return set(result.stdout.strip().splitlines()) if result.returncode == 0 else set()


def _cleanup(workspace_path: Path, runtime_containers_before: set) -> None:
    """Resolver 実行後に Runtime コンテナとワークスペースを削除する。"""
    # Resolver が起動した Runtime コンテナを削除
    after = _get_runtime_containers()
    for name in after - runtime_containers_before:
        logger.info("Removing runtime container: %s", name)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    # ワークスペースディレクトリを削除
    shutil.rmtree(workspace_path, ignore_errors=True)
    logger.info("Cleanup done: workspace=%s", workspace_path)


def _run_docker_streaming(
    cmd: list,
    prefix: str,
    timeout: int,
    line_callback=None,
) -> tuple[int, list[str]]:
    """docker run をストリーミング実行し、各行を webhook ログにリアルタイム転送する。

    line_callback: 各行を引数に呼び出されるオプションのコールバック。
                   リーダースレッド内で呼ばれるため、スレッドセーフに実装すること。
    Returns: (returncode, output_lines)
      returncode == -1 はタイムアウトまたは起動失敗を示す。
    """
    output_lines: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception:
        logger.exception("Failed to start docker process: prefix=%s", prefix)
        return -1, []

    def _reader() -> None:
        try:
            for line in proc.stdout:
                stripped = line.rstrip()
                output_lines.append(stripped)
                logger.info("[%s] %s", prefix, stripped)
                if line_callback:
                    try:
                        line_callback(stripped)
                    except Exception:
                        logger.debug("line_callback error", exc_info=True)
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        reader.join(timeout=5)
        return -1, output_lines

    reader.join(timeout=10)
    return proc.returncode, output_lines


def _make_ssl_ctx() -> ssl.SSLContext:
    """GitLab 向け SSL コンテキストを生成する。"""
    ctx = ssl.create_default_context()
    if GITLAB_SSL_CERT and os.path.exists(GITLAB_SSL_CERT):
        ctx.load_verify_locations(cafile=GITLAB_SSL_CERT)
    return ctx


def _gitlab_notes_url(repo_path: str, issue_number: int, issue_type: str) -> str:
    """GitLab Notes エンドポイント URL を返す。"""
    endpoint = (
        f"merge_requests/{issue_number}/notes"
        if issue_type == "pr"
        else f"issues/{issue_number}/notes"
    )
    encoded_repo = urllib.parse.quote(repo_path, safe="")
    return f"{GITLAB_BASE_URL}/api/v4/projects/{encoded_repo}/{endpoint}"


def _post_gitlab_comment(
    repo_path: str, issue_number: int, issue_type: str, body: str
) -> int | None:
    """GitLab イシュー / MR にコメントを新規投稿する。

    Returns: 作成された note の id。失敗時は None。
    issue_type == "issue" → /issues/{N}/notes
    issue_type == "pr"    → /merge_requests/{N}/notes
    """
    url = _gitlab_notes_url(repo_path, issue_number, issue_type)
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {GITLAB_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as resp:
            note = json.loads(resp.read())
            note_id = note.get("id")
            logger.info(
                "Posted GitLab comment: %s #%s note_id=%s",
                issue_type, issue_number, note_id,
            )
            return note_id
    except Exception:
        logger.warning(
            "Failed to post GitLab comment: %s #%s",
            issue_type, issue_number, exc_info=True,
        )
        return None


def _update_gitlab_comment(
    repo_path: str, issue_number: int, issue_type: str, note_id: int, body: str
) -> None:
    """GitLab の既存 note を PUT で更新する。失敗してもエラーはログのみ。"""
    base_url = _gitlab_notes_url(repo_path, issue_number, issue_type)
    url = f"{base_url}/{note_id}"
    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {GITLAB_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as resp:
            logger.info(
                "Updated GitLab comment: %s #%s note_id=%s status=%s",
                issue_type, issue_number, note_id, resp.status,
            )
    except Exception:
        logger.warning(
            "Failed to update GitLab comment: %s #%s note_id=%s",
            issue_type, issue_number, note_id, exc_info=True,
        )


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
    runtime_containers_before = _get_runtime_containers()

    try:
        container_name = f"openhands-resolver-{issue_number}-{run_id}"
        logger.info(
            "Starting resolver: repo=%s issue=%s type=%s workspace=%s",
            repo_path,
            issue_number,
            issue_type,
            workspace_path,
        )

        # 処理開始を GitLab にコメントで通知（note_id を保持して後で更新する）
        _start_body = "🤖 **OpenHands** がこのイシューに取り組んでいます。\n\n完了したら結果をお知らせします。"
        progress_state = {
            "note_id": _post_gitlab_comment(repo_path, issue_number, issue_type, _start_body),
            "last_update": 0.0,  # epoch seconds
        }

        def _upsert_comment(body: str) -> None:
            """note_id があれば更新、なければ新規投稿してIDを保存する。"""
            nid = progress_state["note_id"]
            if nid is not None:
                _update_gitlab_comment(repo_path, issue_number, issue_type, nid, body)
            else:
                progress_state["note_id"] = _post_gitlab_comment(
                    repo_path, issue_number, issue_type, body
                )

        _ITER_RE = re.compile(r'Iteration\s+(\d+)(?:\s*/\s*(\d+))?', re.IGNORECASE)
        _THROTTLE_SECS = 60
        _recent_lines: deque = deque(maxlen=20)

        def _progress_callback(line: str) -> None:
            """リーダースレッドから呼ばれる進捗コールバック。

            全行を rolling 窓に蓄積し、60秒ごとに GitLab コメントを更新する。
            直近20行を <details> で折りたたみ表示し、Iteration があれば先頭に表示する。
            """
            _recent_lines.append(line)
            now = time.monotonic()
            if now - progress_state["last_update"] < _THROTTLE_SECS:
                return
            progress_state["last_update"] = now

            # 直近20行から最新の Iteration を逆順サーチ
            iter_str = None
            for l in reversed(_recent_lines):
                m = _ITER_RE.search(l)
                if m:
                    cur, max_ = m.group(1), m.group(2)
                    iter_str = f"Iteration {cur}" + (f" / {max_}" if max_ else "")
                    break

            summary = "\n".join(_recent_lines)
            body = (
                "🔄 **OpenHands** 処理中...\n\n"
                + (f"- {iter_str}\n\n" if iter_str else "")
                + "<details><summary>直近ログ（最大20行）</summary>\n\n"
                + f"```\n{summary}\n```\n\n</details>\n\n"
                + "完了したら結果をお知らせします。"
            )
            _upsert_comment(body)

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
            # DooD: Runtime コンテナへの接続先を host.docker.internal に強制設定
            # docker_runtime.py の __init__ で local_runtime_url を上書きする
            "-e", "DOCKER_HOST_ADDR=host.docker.internal",
            # LLM 設定 (--llm-model 等の CLI 引数は v1.5 で無視される。環境変数で渡す)
            "-e", f"LLM_MODEL={LLM_MODEL}",
            "-e", f"LLM_API_KEY={LLM_API_KEY}",
            *((["-e", f"LLM_BASE_URL={LLM_BASE_URL}"]) if LLM_BASE_URL else []),
            # ネットワーク (GitLab と同一ネットワークで名前解決)
            "--network", RESOLVER_NETWORK,
            "--add-host", "host.docker.internal:host-gateway",
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
            "--max-iterations", MAX_ITERATIONS,
            *(["--prompt-file", PROMPT_FILE] if PROMPT_FILE else []),
        ]

        # ─── Resolver 実行（ストリーミング） ─────────────────────────────────
        returncode, resolver_output = _run_docker_streaming(cmd, container_name, 3600, line_callback=_progress_callback)
        _save_log(container_name, resolver_output)
        if returncode == -1:
            logger.error("Resolver timed out: container=%s", container_name)
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            _upsert_comment(
                "⏱️ **OpenHands** の処理がタイムアウトしました（上限 1 時間）。"
                + _format_log_detail(resolver_output)
            )
            return
        elif returncode != 0:
            logger.error("Resolver failed: container=%s returncode=%s", container_name, returncode)
            _upsert_comment(
                "❌ **OpenHands** の処理が失敗しました。"
                + _format_log_detail(resolver_output)
            )
            return

        logger.info("Resolver finished successfully: container=%s", container_name)

        # ─── output.jsonl で git_patch の有無を確認 ───────────────────────────
        # OpenHands がコードを変更しなかった場合は MR 作成できないためスキップする。
        output_jsonl_path = workspace_path / "output.jsonl"
        git_patch: str | None = None
        oh_success: bool = False
        try:
            with open(output_jsonl_path, encoding="utf-8") as _f:
                for _line in _f:
                    _d = json.loads(_line)
                    if str(_d.get("issue_number")) == str(issue_number):
                        oh_success = bool(_d.get("success", False))
                        git_patch = _d.get("git_patch") or None
                        break
        except Exception:
            logger.warning("Failed to read output.jsonl: path=%s", output_jsonl_path, exc_info=True)

        if not git_patch:
            logger.warning(
                "No git patch found, skipping MR: issue=%s oh_success=%s",
                issue_number, oh_success,
            )
            _upsert_comment(
                "⚠️ **OpenHands** は処理を完了しましたが、コードの変更を生成できませんでした。\n\n"
                "課題の内容を見直すか、手動での対応をご検討ください。"
                + _format_log_detail(resolver_output)
            )
            return

        # ─── MR 作成（ストリーミング） ───────────────────────────────────────
        # resolve_issue.py はコード変更と output.jsonl の生成のみ行う。
        # send_pull_request.py が branch push + GitLab MR 作成を担当する。
        mr_container_name = f"openhands-mr-{issue_number}-{run_id}"
        logger.info("Creating MR: repo=%s issue=%s", repo_path, issue_number)

        mr_cmd = [
            "docker", "run", "--rm",
            "--name", mr_container_name,
            # SSL 証明書（設定されている場合のみ）
            *((["-v", f"{GITLAB_SSL_CERT}:{GITLAB_SSL_CERT}:ro",
                "-e", f"SSL_CERT_FILE={GITLAB_SSL_CERT}",
                "-e", f"GIT_SSL_CAINFO={GITLAB_SSL_CERT}"]
               ) if GITLAB_SSL_CERT else []),
            # LLM (MR タイトル・説明の生成に使用)
            "-e", f"LLM_MODEL={LLM_MODEL}",
            "-e", f"LLM_API_KEY={LLM_API_KEY}",
            *((["-e", f"LLM_BASE_URL={LLM_BASE_URL}"]) if LLM_BASE_URL else []),
            # Workspace マウント (output.jsonl の読み込みのため)
            "-v", f"{workspace_path}:{workspace_path}",
            # ネットワーク
            "--network", RESOLVER_NETWORK,
            "--add-host", "host.docker.internal:host-gateway",
            OPENHANDS_IMAGE,
            "python", "-m", "openhands.resolver.send_pull_request",
            "--selected-repo", repo_path,
            "--issue-number", str(issue_number),
            "--output-dir", str(workspace_path),
            "--token", GITLAB_TOKEN,
            "--username", GITLAB_USERNAME,
            "--base-domain", GIT_BASE_DOMAIN,
            "--pr-type", "ready",
            "--send-on-failure",
            "--llm-model", LLM_MODEL,
            "--llm-api-key", LLM_API_KEY,
            "--git-user-name", GITLAB_USERNAME,
            "--git-user-email", f"{GITLAB_USERNAME}@localhost.local",
            *(["--llm-base-url", LLM_BASE_URL] if LLM_BASE_URL else []),
        ]

        mr_returncode, mr_output = _run_docker_streaming(mr_cmd, mr_container_name, 300)
        _save_log(mr_container_name, mr_output)
        if mr_returncode == -1:
            logger.error("MR creation timed out: repo=%s issue=%s", repo_path, issue_number)
            subprocess.run(["docker", "rm", "-f", mr_container_name], capture_output=True)
            _upsert_comment(
                "⚠️ **OpenHands** はコードを生成しましたが、MR 作成がタイムアウトしました。"
                + _format_log_detail(mr_output)
            )
        elif mr_returncode != 0:
            logger.error(
                "MR creation failed: repo=%s issue=%s returncode=%s",
                repo_path, issue_number, mr_returncode,
            )
            _upsert_comment(
                "⚠️ **OpenHands** はコードを生成しましたが、MR の作成に失敗しました。"
                + _format_log_detail(mr_output)
            )
        else:
            # ログ出力から MR URL を抽出（例: "ready created: https://..."）
            mr_url = next(
                (
                    line.split("created:", 1)[1].strip().split()[0]
                    for line in mr_output
                    if "created:" in line and "http" in line
                ),
                None,
            )
            msg = "✅ **OpenHands** による修正が完了しました。"
            if mr_url:
                msg += f"\n\nMR: {mr_url}"
            _upsert_comment(msg)
            logger.info(
                "MR created successfully: repo=%s issue=%s url=%s",
                repo_path, issue_number, mr_url,
            )

    finally:
        # ─── クリーンアップ ──────────────────────────────────────────────────────
        # 成功・失敗に関わらず Runtime コンテナとワークスペースを削除する。
        _cleanup(workspace_path, runtime_containers_before)


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
