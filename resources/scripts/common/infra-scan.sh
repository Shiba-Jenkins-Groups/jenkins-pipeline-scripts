#!/usr/bin/env bash
# infra-scan.sh — 基礎設施 image 弱點掃描（Trivy）
#
# 用途：掃描「不屬於任何專案交付物、故不被 cd.sh 覆蓋」的 image。
#   - agent image：所有 build 碼的執行環境，且掛 docker.sock 並以 root 執行
#   - Harbor 各組件：LAN 可達且位於 jenkins-network 內（攻擊鏈上最短的跳板）
#
# 掃描分工（三者互補、不重疊）：
#   cd.sh          → 各專案 app image，build 當下掃一次
#   Harbor auto_scan → Harbor 內既有 app image，持續重掃（新 CVE 出現時才會發現）
#   本腳本          → 基礎設施 image，週期性掃描
#
# 政策：**純 warn**（一律 exit 0）。掃的是基礎設施而非交付物，
#       fail 沒有「這次 build 該擋下來」的意義，只會製造噪音。
#       趨勢由 Jenkins 的 junit 報告呈現。
#
# 用法：infra-scan.sh <output_dir> [image ...]
#       未指定 image 時，掃描預設清單（agent + Harbor 全組件）

set -uo pipefail   # 不用 -e：單一 image 掃描失敗不應中斷其餘掃描

OUTPUT_DIR="${1:?usage: infra-scan.sh <output_dir> [image ...]}"
shift || true

TRIVY_CACHE="${TRIVY_CACHE_DIR:-/home/jenkins/.cache/trivy}"
TRIVY_TEMPLATE="/usr/local/share/trivy/templates/junit.tpl"

# ── 預設掃描清單 ─────────────────────────────────────────────────────────────
# agent image 名稱依 APP_ENV 動態組成，與 jenkins.yaml 的 Docker Cloud template 一致
AGENT_IMAGE="shiba-docker-jenkins-agent-${APP_ENV:-dev}"

default_targets() {
    echo "${AGENT_IMAGE}"
    # Harbor 組件：由運行中的容器實際取得，避免版本寫死後與現況脫節
    docker ps --format '{{.Image}}' 2>/dev/null | grep '^goharbor/' | sort -u || true
}

TARGETS=("$@")
if [[ ${#TARGETS[@]} -eq 0 ]]; then
    # shellcheck disable=SC2207
    TARGETS=($(default_targets))
fi

mkdir -p "${OUTPUT_DIR}"

echo "[infra-scan] 掃描對象：${#TARGETS[@]} 個"
printf '[infra-scan]   - %s\n' "${TARGETS[@]}"
echo ""

FAILED=0
SUMMARY=""

for image in "${TARGETS[@]}"; do
    # 檔名安全化：goharbor/harbor-core:v2.11.2 → goharbor_harbor-core_v2.11.2
    safe_name="$(echo "${image}" | tr '/:' '__')"
    report="${OUTPUT_DIR}/trivy-${safe_name}.xml"

    echo "[infra-scan] 掃描 ${image} ..."
    if ! trivy image \
        --exit-code 0 \
        --severity HIGH,CRITICAL \
        --scanners vuln \
        --cache-dir "${TRIVY_CACHE}" \
        --format template \
        --template "@${TRIVY_TEMPLATE}" \
        --output "${report}" \
        --skip-version-check \
        --quiet \
        "${image}" 2>&1; then
        echo "[infra-scan] ⚠️  ${image} 掃描失敗（不中斷其餘掃描）"
        rm -f "${report}"
        FAILED=$((FAILED + 1))
        continue
    fi

    # 由 junit 報告計數（testcase 內含 failure 者＝有弱點）
    count=$(grep -c "<failure" "${report}" 2>/dev/null || echo 0)
    SUMMARY="${SUMMARY}\n  ${image}: ${count} 項 HIGH/CRITICAL"
    echo "[infra-scan] ${image} → ${count} 項 HIGH/CRITICAL"
done

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  INFRA SCAN SUMMARY                          ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${SUMMARY}"
[[ "${FAILED}" -gt 0 ]] && echo "  ⚠️  ${FAILED} 個 image 掃描失敗"
echo ""
echo "[infra-scan] 報告輸出至 ${OUTPUT_DIR}（warn 檔位，一律 exit 0）"

exit 0
