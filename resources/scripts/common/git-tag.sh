#!/usr/bin/env bash
# common/git-tag.sh — Git Tag 共通

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

# 建一支臨時 GIT_ASKPASS 腳本並印出路徑（呼叫端負責在 GIT_ASKPASS_USER/PASS
# 設好後執行 git 指令、用完 rm -f）——push_git_tag／fetch_git_tags 共用，
# 避免憑證塞進 URL（世界可讀的 `ps`／git 錯誤訊息會外洩）。
_make_git_askpass() {
    local askpass
    askpass="$(mktemp)"
    cat > "${askpass}" <<'ASKPASS_EOF'
#!/usr/bin/env bash
# git 依 prompt 詢問 Username/Password；各自回對應 env（值不出現在 args）
case "${1}" in
    Username*) printf '%s' "${GIT_ASKPASS_USER}" ;;
    Password*) printf '%s' "${GIT_ASKPASS_PASS}" ;;
esac
ASKPASS_EOF
    chmod +x "${askpass}"
    echo "${askpass}"
}

# Jenkins checkout 預設 noTags（避免 clone 拖一堆 tag 拖慢），但 prod 分支
# 靠 `git describe --tags` 找手動打的 tag——describe 前得先把 tag 抓下來，
# 否則剛 push 的 tag 在本地 workspace 看不到（G: 2026-07-19 實測踩坑）。
fetch_git_tags() {
    local remote_url
    remote_url="$(git remote get-url origin)"

    local askpass
    askpass="$(_make_git_askpass)"
    trap "rm -f '${askpass}'" RETURN

    GIT_ASKPASS="${askpass}" \
    GIT_ASKPASS_USER="${GITHUB_CREDENTIALS_USR}" \
    GIT_ASKPASS_PASS="${GITHUB_CREDENTIALS_PSW}" \
    GIT_TERMINAL_PROMPT=0 \
        git fetch --tags --force "${remote_url}" 2>/dev/null || true
}

# Branch → tag prefix 對應
# develop  → ci-dev-{BUILD_NUMBER}
# main     → ci-main-{BUILD_NUMBER}
# prod     → 開發者手動打 tag，不自動建立
# 其他     → ci-{branch}-{BUILD_NUMBER}
resolve_git_tag() {
    local branch="${1}"
    local build_number="${2}"

    case "${branch}" in
        develop)
            echo "ci-dev-${build_number}"
            ;;
        main)
            echo "ci-main-${build_number}"
            ;;
        prod)
            # prod 使用手動打的 tag，從 git describe 取得（先 fetch --tags，見上）
            fetch_git_tags
            local tag
            tag="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
            if [[ -z "${tag}" ]]; then
                echo "[ERROR] prod branch requires a manual git tag. Please tag the commit before triggering pipeline." >&2
                exit 1
            fi
            echo "${tag}"
            ;;
        *)
            local safe_branch
            safe_branch="$(echo "${branch}" | tr '/' '-' | tr '_' '-')"
            echo "ci-${safe_branch}-${build_number}"
            ;;
    esac
}

push_git_tag() {
    local tag="${1}"
    local remote_url
    remote_url="$(git remote get-url origin)"

    # prod 路徑：resolve_git_tag 已透過 fetch_git_tags 把手動打的 tag 抓下來，
    # 本地已存在同名 tag ref，此時只需重推（no-op）；develop/main 才是全新建立。
    git rev-parse -q --verify "refs/tags/${tag}" >/dev/null || git tag "${tag}"

    # 憑證以 GIT_ASKPASS 提供（走 env，不塞進 URL）——避免 token 洩漏於
    # `ps` 的 command args（世界可讀，Jenkins console mask 不涵蓋）與 git push 失敗訊息。
    local askpass
    askpass="$(_make_git_askpass)"
    # 函數返回時清掉臨時 askpass（含失敗路徑）
    # shellcheck disable=SC2064
    trap "rm -f '${askpass}'" RETURN

    GIT_ASKPASS="${askpass}" \
    GIT_ASKPASS_USER="${GITHUB_CREDENTIALS_USR}" \
    GIT_ASKPASS_PASS="${GITHUB_CREDENTIALS_PSW}" \
    GIT_TERMINAL_PROMPT=0 \
        git push "${remote_url}" "${tag}"

    echo "[git-tag] Pushed tag: ${tag}"
}
