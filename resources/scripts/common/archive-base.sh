#!/usr/bin/env bash
# common/archive-base.sh — release/backup 搬移邏輯共通

source "$(dirname "${BASH_SOURCE[0]}")/error-handler.sh"

ARTIFACTS_ROOT="${ARTIFACTS_ROOT:-/var/jenkins_home/artifacts}"
MAX_BACKUPS=10

# 將產出物放入 release/，並將舊版移入 backup/（最多保留 MAX_BACKUPS 份）
archive_artifact() {
    local app_name="${1}"
    local artifact_path="${2}"     # 完整路徑
    local artifact_filename
    artifact_filename="$(basename "${artifact_path}")"

    local release_dir="${ARTIFACTS_ROOT}/${app_name}/release"
    local backup_dir="${ARTIFACTS_ROOT}/${app_name}/backup"

    mkdir -p "${release_dir}" "${backup_dir}"

    # 將現有 release 移至 backup
    if [[ -n "$(ls -A "${release_dir}" 2>/dev/null)" ]]; then
        for old_file in "${release_dir}"/*; do
            mv "${old_file}" "${backup_dir}/"
            echo "[archive] Moved to backup: $(basename "${old_file}")"
        done
    fi

    # 清理超出上限的 backup（保留最新 MAX_BACKUPS 份）
    local backup_count
    backup_count="$(ls "${backup_dir}" | wc -l)"
    if (( backup_count > MAX_BACKUPS )); then
        local excess=$(( backup_count - MAX_BACKUPS ))
        ls -t "${backup_dir}" | tail -n "${excess}" | while read -r old; do
            rm -f "${backup_dir}/${old}"
            echo "[archive] Removed old backup: ${old}"
        done
    fi

    # 放入新版
    cp "${artifact_path}" "${release_dir}/${artifact_filename}"
    echo "[archive] Released: ${release_dir}/${artifact_filename}"
}
