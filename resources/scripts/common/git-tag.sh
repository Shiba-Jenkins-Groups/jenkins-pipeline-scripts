#!/usr/bin/env bash
# common/git-tag.sh — Git Tag 共通

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

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
            # prod 使用手動打的 tag，從 git describe 取得
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
    local credentials_usr="${GITHUB_CREDENTIALS_USR}"
    local credentials_psw="${GITHUB_CREDENTIALS_PSW}"
    local remote_url
    remote_url="$(git remote get-url origin)"

    # 注入 credentials 到 remote URL
    local auth_url
    auth_url="$(echo "${remote_url}" | sed "s|https://|https://${credentials_usr}:${credentials_psw}@|")"

    git tag "${tag}"
    git push "${auth_url}" "${tag}"
    echo "[git-tag] Pushed tag: ${tag}"
}
