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

# ── 把發現「說出來」──────────────────────────────────────────────────────────
# 為何需要：main 的 failBuildOnCVSS=11（＝官方預設，CVSS 上限只有 10 ⇒ 永不 fail）。
# 於是掃描跑了、報告出了、build 全綠，**除非有人主動點開 HTML 報告，否則沒人知道掃到什麼**。
# 實例：claude-project main #14 掃出 9 個有 CVE 的依賴、其中 6 個 CRITICAL（CVSS 9.8，
# 經版本區間核對為真陽性非 CPE 誤判），而 build 是綠的、無人察覺（2026-07-20 稽核）。
# 「有價值的資訊不能只躺在報告裡等人去點」——本段只負責讓它出現在 console，
# 不改變任何閘門行為（門檻仍由 DEP_SCAN_CVSS 決定）。
# 遙測性質：解析失敗不得反噬掃描結果，故整段 `|| true`。
summarize_dep_report() {
    local json="${WORKSPACE}/target/dependency-check-report.json"
    [[ -f "${json}" ]] || { echo "[dep-check] （無 JSON 報告可摘要）"; return 0; }
    python3 - "${json}" "${CVSS}" <<'PY' || true
import json, sys, collections
try:
    data = json.load(open(sys.argv[1]))
except Exception as e:
    print("[dep-check] 報告解析失敗（不影響掃描結果）：%s" % e); sys.exit(0)
threshold = float(sys.argv[2])
rows = []
for dep in data.get("dependencies", []):
    vulns = dep.get("vulnerabilities") or []
    if not vulns:
        continue
    top, sev = 0.0, "UNKNOWN"
    for v in vulns:
        s = (v.get("cvssv3") or {}).get("baseScore") or (v.get("cvssv2") or {}).get("score") or 0
        try: s = float(s)
        except (TypeError, ValueError): s = 0.0
        if s > top:
            top, sev = s, (v.get("severity") or "UNKNOWN").upper()
    rows.append((top, sev, dep.get("fileName", "?"), len(vulns)))
if not rows:
    print("[dep-check] ✅ 未發現含已知 CVE 的依賴")
    sys.exit(0)
by_sev = collections.Counter(r[1] for r in rows)
total_cve = sum(r[3] for r in rows)
over = [r for r in rows if r[0] >= threshold]
print("")
print("╔══════════════════════════════════════════════════════════╗")
print("║  DEPENDENCY CVE SUMMARY                                  ║")
print("╠══════════════════════════════════════════════════════════╣")
print("  含已知 CVE 的依賴：%d 個（合計 %d 筆 CVE）" % (len(rows), total_cve))
print("  最高嚴重度分布：%s" % (", ".join("%s=%d" % (k, by_sev[k])
      for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN") if by_sev.get(k))))
print("  ── 依最高 CVSS 排序（前 10）──")
for top, sev, name, n in sorted(rows, reverse=True)[:10]:
    print("   CVSS %4.1f  %-8s %-42s (%d 筆)" % (top, sev, name[:42], n))
if threshold > 10:
    print("  ⚠ 本分支 failBuildOnCVSS=%g（>10 ＝永不阻斷，僅出報告）" % threshold)
    print("    ⇒ 上列發現**不會**讓 build 變紅。要讓它成為閘門，改 branch-policy.sh 的 DEP_SCAN_CVSS。")
else:
    print("  本分支門檻 failBuildOnCVSS=%g ⇒ %d 個依賴超標" % (threshold, len(over)))
print("  完整報告：Build 頁的「OWASP Dependency-Check Report」")
print("╚══════════════════════════════════════════════════════════╝")
PY
}
summarize_dep_report || true
