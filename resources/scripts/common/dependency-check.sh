#!/usr/bin/env bash
# common/dependency-check.sh — 第三方依賴 CVE 掃描（OWASP Dependency-Check；Security Phase 2 v1.7.1）
#
# 比對專案依賴 vs NVD（National Vulnerability Database）已知 CVE。
# 政策 DO_DEP_SCAN / DEP_SCAN_CVSS 由 branch-policy.sh 決定：
#   main  → failBuildOnCVSS=11（＝官方預設，永不 fail＝warn only，只出報告）
#   prod  → failBuildOnCVSS=7（依賴含 CVSS≥7 的 CVE 即 build FAILURE）
# NVD DB 快取於 Maven localRepository（/home/jenkins/.cache/m2，jenkins-agent-cache volume）自動持久化，
#   首次同步較慢（下載/建庫），之後跨動態 agent 命中快取。
# NVD_API_KEY 由 Jenkins credential（nvd-api-key）綁定注入 env——加速 NVD 更新、避免 rate limit 403。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"

WORKSPACE="${WORKSPACE:-$(pwd)}"
LANGUAGE="${LANGUAGE:-}"
BUILD_TOOL="${BUILD_TOOL:-}"
CVSS="${DEP_SCAN_CVSS:-11}"
DC_VERSION="12.2.2"

cd "${WORKSPACE}"

# 本階段 OWASP 走 dependency-check-maven plugin，僅適用 Java/Maven。
# 其他語言（Node/Go）的依賴 CVE 掃描屬另一分析器，暫不在本項範圍——非 java/maven 直接跳過。
if [[ "${LANGUAGE}" != "java" || "${BUILD_TOOL}" != "maven" ]]; then
    echo "[dep-check] LANGUAGE=${LANGUAGE:-?}/BUILD_TOOL=${BUILD_TOOL:-?} 非 java/maven，跳過 OWASP 依賴掃描。"
    exit 0
fi

if [[ -z "${NVD_API_KEY:-}" ]]; then
    echo "[dep-check] [WARN] NVD_API_KEY 未設定——NVD 更新將極慢且易撞 rate limit。" >&2
fi

echo "[dep-check] OWASP dependency-check ${DC_VERSION}（failBuildOnCVSS=${CVSS}；NVD DB 快取於 m2）..."

# NVD API key：user property 是 nvd.api.key（非 nvdApiKey——後者是 pom 設定元素名，命令列會被靜默忽略
#   → 退回 keyless 慢速同步）。key 由 Jenkins credential 綁定注入 NVD_API_KEY（console 自動 mask）。
# report HTML+JSON 落 target/ → ciPipeline 已 publishHTML target/dependency-check-report.html
./mvnw -B org.owasp:dependency-check-maven:"${DC_VERSION}":check \
    -Dnvd.api.key="${NVD_API_KEY:-}" \
    -DfailBuildOnCVSS="${CVSS}" \
    -Dformats=HTML,JSON

echo "[dep-check] 依賴掃描完成（report: target/dependency-check-report.html）。"
