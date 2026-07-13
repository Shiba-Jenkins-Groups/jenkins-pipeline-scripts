#!/usr/bin/env bash
# common/nexus-upload.sh — Artifact 上傳/下載 Nexus raw hosted（改善計畫 #4a）
#
# 路徑契約：{app}/{branch}/{version}-{build}-{sha7}/{filename}
#   sha7＝GIT_COMMIT 前 7 碼：#6 Multibranch 遷移後 BUILD_NUMBER 歸零也不撞路徑，
#   撞上時由 repo 的 Disable redeploy 政策 loud fail，不靜默覆蓋
#
# 憑證紀律：一律經 netrc 暫存檔（0600＋用畢即刪）傳遞，不進 argv/URL——
#   避免 ps 與 curl 錯誤訊息露密（git-tag.sh 憑證拼 URL 為反面教材，見改善計畫 #8）

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

# Nexus 位址與 repo 名可由環境覆蓋（雲端/異環境用）；預設走 jenkins-network 內部 DNS
NEXUS_BASE_URL="${NEXUS_BASE_URL:-http://shiba-docker-nexus-dev:8081}"
NEXUS_RAW_REPO="${NEXUS_RAW_REPO:-raw-artifacts}"

# 取 commit 短碼：Jenkins GIT_COMMIT 優先，standalone 情境退回 git rev-parse，再退 nosha
_nexus_sha7() {
    local sha="${GIT_COMMIT:-}"
    if [[ -z "${sha}" ]]; then
        sha="$(git rev-parse HEAD 2>/dev/null || true)"
    fi
    if [[ -z "${sha}" ]]; then
        echo "nosha"
    else
        echo "${sha:0:7}"
    fi
}

# 由 NEXUS_BASE_URL 取出 host（去 scheme／port／path），供 netrc machine 欄位使用
_nexus_host() {
    local host="${NEXUS_BASE_URL#*://}"
    host="${host%%/*}"
    echo "${host%%:*}"
}

# 產生 netrc 暫存檔（mktemp 預設 0600），呼叫端負責用畢即刪
_nexus_netrc() {
    local netrc_file
    netrc_file="$(mktemp)"
    printf 'machine %s login %s password %s\n' \
        "$(_nexus_host)" "${NEXUS_CRED_USR}" "${NEXUS_CRED_PSW}" > "${netrc_file}"
    echo "${netrc_file}"
}

# 路徑契約單一實作（測試與上傳共用）
nexus_artifact_path() {
    local app="$1" branch="$2" version="$3" build="$4" filename="$5"
    echo "${app}/${branch}/${version}-${build}-$(_nexus_sha7)/${filename}"
}

# 上傳產出物；stdout 回傳完整 URL（供 build.env 記錄），過程訊息一律走 stderr
# 用法：NEXUS_ARTIFACT_URL="$(nexus_upload_artifact <app> <branch> <version> <build> <artifact_path>)"
nexus_upload_artifact() {
    local app="$1" branch="$2" version="$3" build="$4" artifact_path="$5"

    if [[ -z "${NEXUS_CRED_USR:-}" ]] || [[ -z "${NEXUS_CRED_PSW:-}" ]]; then
        report_error "NEXUS" "001" "NEXUS_CRED_USR/NEXUS_CRED_PSW not set. Check Jenkins credential 'nexus-ci-deploy' binding in ciPipeline.groovy."
        exit 1
    fi
    if [[ ! -f "${artifact_path}" ]]; then
        report_error "NEXUS" "003" "Artifact not found for upload: ${artifact_path}"
        exit 1
    fi

    local dest_path url netrc_file
    dest_path="$(nexus_artifact_path "${app}" "${branch}" "${version}" "${build}" "$(basename "${artifact_path}")")"
    url="${NEXUS_BASE_URL}/repository/${NEXUS_RAW_REPO}/${dest_path}"

    echo "[nexus] Uploading: $(basename "${artifact_path}") → ${url}" >&2
    netrc_file="$(_nexus_netrc)"
    if ! curl -sSf --netrc-file "${netrc_file}" -T "${artifact_path}" "${url}" >&2; then
        rm -f "${netrc_file}"
        # Disable redeploy 的 repo 對同路徑重傳會 4xx——屬設計內 loud fail（防覆蓋防競態）
        report_error "NEXUS" "002" "Upload failed: ${url}. Check repo '${NEXUS_RAW_REPO}' exists (raw hosted, Disable redeploy), credential permission (add+read), or path collision."
        exit 1
    fi
    rm -f "${netrc_file}"
    echo "[nexus] Upload completed." >&2

    echo "${url}"
}

# 下載產出物（cd.sh 取檔第二優先序用）；有憑證走 netrc，無憑證嘗試匿名讀
nexus_download_artifact() {
    local url="$1" dest="$2"

    if [[ -n "${NEXUS_CRED_USR:-}" ]] && [[ -n "${NEXUS_CRED_PSW:-}" ]]; then
        local netrc_file
        netrc_file="$(_nexus_netrc)"
        if ! curl -sSf --netrc-file "${netrc_file}" -o "${dest}" "${url}"; then
            rm -f "${netrc_file}"
            report_error "NEXUS" "004" "Download failed: ${url}"
            exit 1
        fi
        rm -f "${netrc_file}"
    else
        curl -sSf -o "${dest}" "${url}" \
            || { report_error "NEXUS" "004" "Download failed (anonymous): ${url}"; exit 1; }
    fi
    echo "[nexus] Downloaded: ${url} → ${dest}" >&2
}
