#!/usr/bin/env bash
# =============================================================================
# OpenHands + GitLab セットアップスクリプト (Mac / Linux 対応)
# =============================================================================
# 事前準備:
#   1. GitLab に openhands ユーザーを用意する
#      - ローカル GitLab: UI (localhost:8080) でユーザー作成
#      - 外部 GitLab:    自己登録 or 管理者に依頼
#   2. openhands ユーザーで GitLab にログインし PAT を作成
#      Settings → Access Tokens
#      スコープ: api, read_repository, write_repository
#   3. .env を設定（cp .env.example .env）
#      GITLAB_EXTERNAL_URL=http://<GitLabのURL>
#      GITLAB_TOKEN=<作成した PAT>
#      WEBHOOK_URL=<GitLab から Webhook Receiver に到達できる URL>
#        ローカル GitLab（同一 Docker ネットワーク）の場合:
#          WEBHOOK_URL=http://openhands-webhook:5000/webhook
#        外部 GitLab の場合:
#          WEBHOOK_URL=http://<このホストのIP>:<WEBHOOK_PORT>/webhook
#
# 実行順序:
#   （ローカル GitLab 使用時）
#     docker compose --profile local up -d gitlab
#     → GitLab が起動したら UI で openhands ユーザーを作成・PAT を取得
#   1. ./scripts/setup.sh
#   2. docker compose up -d          （外部 GitLab）
#      docker compose --profile local up -d  （ローカル GitLab）
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

# ─── OS 判定 & Docker ホストIP 検出 ──────────────────────────────────────────
detect_docker_host() {
    local os
    os="$(uname -s)"
    if [[ "$os" == "Darwin" ]]; then
        echo "host.docker.internal"
    else
        local gateway
        gateway=$(docker network inspect bridge \
            --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null \
            | head -n 1 || true)
        if [[ -n "$gateway" ]]; then
            echo "$gateway"
        else
            warn "Docker bridge gateway を自動検出できませんでした。172.17.0.1 を使います"
            echo "172.17.0.1"
        fi
    fi
}

# ─── .env の指定キーを更新（または追記） ────────────────────────────────────
update_env_key() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        local tmpfile
        tmpfile="$(mktemp)"
        sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmpfile"
        mv "$tmpfile" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# ─── .env 読み込み ─────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    die ".env が見つかりません。先に: cp .env.example .env して値を設定してください"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${GITLAB_EXTERNAL_URL:?GITLAB_EXTERNAL_URL が設定されていません}"
: "${GITLAB_TOKEN:?GITLAB_TOKEN（openhands ユーザーの PAT）が設定されていません}"

GITLAB_URL="${GITLAB_EXTERNAL_URL%/}"
GITLAB_GROUP="${GITLAB_GROUP:-openhands}"
TRIGGER_LABEL="${TRIGGER_LABEL:-openhands}"
WEBHOOK_PORT="${WEBHOOK_PORT:-5000}"
# WEBHOOK_URL 未設定の場合は Docker 内部ネットワーク経由のデフォルトを使う
# （ローカル GitLab が同一 Docker ネットワーク内にある場合はこのままで動作する）
WEBHOOK_URL="${WEBHOOK_URL:-http://openhands-webhook:${WEBHOOK_PORT}/webhook}"

# ─── GitLab 接続確認 & ユーザー検証 ─────────────────────────────────────────
verify_token() {
    info "GitLab への接続を確認しています: ${GITLAB_URL}"

    local me
    me=$(curl -sf \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        "${GITLAB_URL}/api/v4/user" 2>/dev/null || echo "")

    if [ -z "$me" ]; then
        die "GitLab に接続できません。GITLAB_EXTERNAL_URL と GITLAB_TOKEN を確認してください"
    fi

    local username
    username=$(echo "$me" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('username',''))" 2>/dev/null || echo "")

    if [ -z "$username" ]; then
        die "GITLAB_TOKEN が無効です"
    fi

    info "接続 OK: ユーザー '${username}' でセットアップを実行します"
    echo "$username"
}

# ─── グループ作成 ─────────────────────────────────────────────────────────────
# openhands ユーザー自身のトークンで作成するため、
# 作成者が自動的に Owner になりグループ Webhook の登録も可能。
create_group() {
    info "グループ '${GITLAB_GROUP}' を確認しています..."

    local existing
    existing=$(curl -sf \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        "${GITLAB_URL}/api/v4/groups?search=${GITLAB_GROUP}" 2>/dev/null || echo "[]")

    local group_id
    group_id=$(echo "$existing" | python3 -c "
import sys, json
for g in json.load(sys.stdin):
    if g.get('path') == '${GITLAB_GROUP}':
        print(g['id']); break
" 2>/dev/null || echo "")

    if [ -z "$group_id" ]; then
        local response
        response=$(curl -sf \
            --request POST \
            --header "Authorization: Bearer ${GITLAB_TOKEN}" \
            --header "Content-Type: application/json" \
            --data "{
                \"name\": \"${GITLAB_GROUP}\",
                \"path\": \"${GITLAB_GROUP}\",
                \"description\": \"OpenHands 連携グループ\",
                \"visibility\": \"private\"
            }" \
            "${GITLAB_URL}/api/v4/groups" 2>/dev/null || echo "")

        group_id=$(echo "$response" | python3 -c \
            "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

        if [ -z "$group_id" ]; then
            warn "グループの作成に失敗しました"
            warn "GitLab インスタンスで can_create_group が無効の場合は管理者に依頼してください"
            warn "既存グループを使う場合は .env の GITLAB_GROUP にそのグループのパスを設定してください"
            return 1
        fi
        info "グループ作成完了 (ID: ${group_id})"
    else
        # 既存グループへの Owner 権限確認
        local my_access
        my_access=$(curl -sf \
            --header "Authorization: Bearer ${GITLAB_TOKEN}" \
            "${GITLAB_URL}/api/v4/groups/${group_id}/members/all" 2>/dev/null \
            | python3 -c "
import sys, json
for m in json.load(sys.stdin):
    if m.get('username') == '${OPENHANDS_USER:-}':
        print(m.get('access_level', 0)); break
else:
    print(50)  # 自分が Owner として作成した場合はデフォルト50
" 2>/dev/null || echo "50")

        if [[ "$my_access" -lt 50 ]]; then
            warn "グループ '${GITLAB_GROUP}' への Owner 権限が確認できません（access_level: ${my_access}）"
            warn "Webhook 登録に失敗する場合は管理者に Owner 権限の付与を依頼してください"
        fi
        info "グループ確認済み (ID: ${group_id})"
    fi

    echo "$group_id"
}

# ─── グループレベルラベル作成 ─────────────────────────────────────────────────
create_group_label() {
    local group_id="$1"

    info "グループレベルラベル '${TRIGGER_LABEL}' を作成しています..."

    curl -sf \
        --request POST \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        --header "Content-Type: application/json" \
        --data "{
            \"name\": \"${TRIGGER_LABEL}\",
            \"color\": \"#0075ca\",
            \"description\": \"OpenHands に処理を依頼するラベル（グループ共通）\"
        }" \
        "${GITLAB_URL}/api/v4/groups/${group_id}/labels" > /dev/null 2>&1 || true

    info "グループレベルラベル '${TRIGGER_LABEL}' を作成しました"
}

# ─── グループ Webhook 登録 ────────────────────────────────────────────────────
register_group_webhook() {
    local group_id="$1"

    info "グループ Webhook を登録しています: ${WEBHOOK_URL}"

    # 既存の openhands webhook を削除
    local existing_hooks
    existing_hooks=$(curl -sf \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        "${GITLAB_URL}/api/v4/groups/${group_id}/hooks" 2>/dev/null || echo "[]")

    echo "$existing_hooks" | python3 -c "
import sys, json
for h in json.load(sys.stdin):
    if 'openhands-webhook' in h.get('url', ''):
        print(h['id'])
" 2>/dev/null | while read -r hook_id; do
        curl -sf --request DELETE \
            --header "Authorization: Bearer ${GITLAB_TOKEN}" \
            "${GITLAB_URL}/api/v4/groups/${group_id}/hooks/${hook_id}" > /dev/null 2>&1 || true
        info "既存のグループ Webhook (ID: ${hook_id}) を削除しました"
    done

    local hook_response
    hook_response=$(curl -sf \
        --request POST \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        --header "Content-Type: application/json" \
        --data "{
            \"url\": \"${WEBHOOK_URL}\",
            \"token\": \"${WEBHOOK_SECRET:-}\",
            \"issues_events\": true,
            \"note_events\": true,
            \"merge_requests_events\": true,
            \"push_events\": false,
            \"member_events\": false,
            \"enable_ssl_verification\": false
        }" \
        "${GITLAB_URL}/api/v4/groups/${group_id}/hooks" 2>&1 || echo "")

    local hook_id
    hook_id=$(echo "$hook_response" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

    if [ -n "$hook_id" ]; then
        info "グループ Webhook 登録完了 (ID: ${hook_id})"
    else
        warn "グループ Webhook の登録に失敗しました"
        warn "グループの Owner 権限があるか確認してください"
        warn "再実行: ./scripts/setup.sh"
    fi
}

# ─── テストプロジェクト作成 ───────────────────────────────────────────────────
create_test_project() {
    local group_id="$1"

    info "テストプロジェクト '${GITLAB_GROUP}/openhands-test' を作成しています..."

    local response
    response=$(curl -sf \
        --request POST \
        --header "Authorization: Bearer ${GITLAB_TOKEN}" \
        --header "Content-Type: application/json" \
        --data "{
            \"name\": \"openhands-test\",
            \"namespace_id\": ${group_id},
            \"description\": \"OpenHands 連携テスト用プロジェクト\",
            \"visibility\": \"private\",
            \"initialize_with_readme\": true,
            \"default_branch\": \"main\"
        }" \
        "${GITLAB_URL}/api/v4/projects" 2>/dev/null || echo "")

    local project_id
    project_id=$(echo "$response" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

    if [ -n "$project_id" ]; then
        info "テストプロジェクト作成完了 (ID: ${project_id})"
    else
        warn "テストプロジェクトはすでに存在するか作成に失敗しました（スキップ）"
    fi
}

# ─── メイン処理 ───────────────────────────────────────────────────────────────
main() {
    echo ""
    echo "=============================================="
    echo " OpenHands + GitLab セットアップ"
    echo " GitLab: ${GITLAB_URL}"
    echo "=============================================="
    echo ""

    # GitLab 接続確認
    local openhands_user
    openhands_user=$(verify_token)

    # Webhook / GitLab の接続情報を .env に保存
    # （Webhook コンテナが Resolver 起動時に使う）
    local git_domain
    git_domain=$(echo "${GITLAB_URL}" | python3 -c \
        "import sys; from urllib.parse import urlparse; u=urlparse(sys.stdin.read().strip()); print(u.netloc)")
    update_env_key "GITLAB_BASE_URL" "${GITLAB_URL}"
    update_env_key "GIT_BASE_DOMAIN" "${git_domain}"
    info ".env に GITLAB_BASE_URL / GIT_BASE_DOMAIN を保存しました"

    # OS 判定（DOCKER_HOST_INTERNAL を .env に保存）
    local docker_host
    docker_host=$(detect_docker_host)
    update_env_key "DOCKER_HOST_INTERNAL" "${docker_host}"

    # グループ作成（openhands ユーザーが Owner として作成）
    local group_id
    group_id=$(create_group) || { warn "グループ作成をスキップしました"; exit 0; }

    if [ -n "$group_id" ]; then
        create_group_label "$group_id"
        register_group_webhook "$group_id"
        create_test_project "$group_id"
    fi

    echo ""
    echo "=============================================="
    info "セットアップ完了！"
    echo ""
    echo "次のコマンドで OpenHands と Webhook を起動してください:"
    echo ""
    echo "  # 外部 GitLab の場合:"
    echo "  docker compose up -d"
    echo ""
    echo "  # ローカル GitLab の場合:"
    echo "  docker compose --profile local up -d"
    echo ""
    echo "アクセス先:"
    echo "  GitLab:    ${GITLAB_URL}"
    echo "  OpenHands: http://localhost:${OPENHANDS_PORT:-3000}"
    echo "  Webhook:   http://localhost:${WEBHOOK_PORT:-5000}/health"
    echo ""
    echo "グループ: ${GITLAB_URL}/${GITLAB_GROUP}"
    echo "  実行ユーザー:     ${openhands_user}"
    echo "  グループ Webhook: グループ配下の全プロジェクトで有効"
    echo "  共通ラベル:       ${TRIGGER_LABEL}"
    echo "=============================================="
}

main "$@"
