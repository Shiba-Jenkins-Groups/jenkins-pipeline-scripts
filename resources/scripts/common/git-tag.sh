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

_git_with_askpass() {
    local askpass
    askpass="$(_make_git_askpass)"
    # 展開當下的隨機路徑，避免 RETURN 時 local 變數已離開作用域。
    # shellcheck disable=SC2064
    trap "rm -f '${askpass}'" RETURN
    GIT_ASKPASS="${askpass}" \
    GIT_ASKPASS_USER="${GITHUB_CREDENTIALS_USR}" \
    GIT_ASKPASS_PASS="${GITHUB_CREDENTIALS_PSW}" \
    GIT_TERMINAL_PROMPT=0 "$@"
}

remote_tag_commit() {
    local tag="${1:?tag is required}" remote_url output
    remote_url="$(git remote get-url origin)"
    output="$(_git_with_askpass git ls-remote --tags "${remote_url}" \
        "refs/tags/${tag}" "refs/tags/${tag}^{}")"
    awk -v peeled="refs/tags/${tag}^{}" -v direct="refs/tags/${tag}" \
        '$2 == peeled { print $1; found=1; exit } $2 == direct { fallback=$1 } END { if (!found && fallback) print fallback }' \
        <<<"${output}"
}

# Branch → tag prefix 對應
# develop  → ci-dev-{BUILD_NUMBER}（專案可透過 gitTagEnabled:false 關閉）
# main     → ci-main-{BUILD_NUMBER}
# prod     → 由已通過 strict verification 的 release finalization 建立 v{APP_VERSION}
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
            local version="${RELEASE_VERSION:-${APP_VERSION:-}}"
            version="${version#v}"
            version="${version%-SNAPSHOT}"
            version="${version%-RC}"
            if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                echo "[ERROR] prod release requires an exact SemVer APP_VERSION (got: ${version:-empty})." >&2
                exit 1
            fi
            echo "v${version}"
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

    local head remote_commit local_commit
    head="$(git rev-parse HEAD)"
    remote_commit="$(remote_tag_commit "${tag}")"
    if [[ -n "${remote_commit}" ]]; then
        if [[ "${remote_commit}" == "${head}" ]]; then
            echo "[git-tag] Tag already exists on this commit: ${tag}"
            return 0
        fi
        report_error "GIT_TAG" "002" "Immutable tag ${tag} already points to ${remote_commit}; refusing to move it to ${head}."
        return 1
    fi

    if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null; then
        local_commit="$(git rev-list -n 1 "${tag}")"
        if [[ "${local_commit}" != "${head}" ]]; then
            report_error "GIT_TAG" "003" "Local tag ${tag} points to ${local_commit}; refusing to move it to ${head}."
            return 1
        fi
    else
        # 動態 Jenkins agent 不保證有 global/repository committer identity。
        # Annotated tag 必須自行帶 identity，不能讓已完成的 PROD 驗證在最後一步依賴 agent 狀態。
        git -c user.name="${GIT_TAG_USER_NAME:-Jenkins Release}" \
            -c user.email="${GIT_TAG_USER_EMAIL:-jenkins-release@localhost}" \
            tag -a "${tag}" -m "Release ${tag}"
    fi

    # 憑證以 GIT_ASKPASS 提供（走 env，不塞進 URL）——避免 token 洩漏於
    # `ps` 的 command args（世界可讀，Jenkins console mask 不涵蓋）與 git push 失敗訊息。
    _git_with_askpass git push "${remote_url}" "refs/tags/${tag}"

    echo "[git-tag] Pushed tag: ${tag}"
}
