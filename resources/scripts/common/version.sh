#!/usr/bin/env bash
# common/version.sh — 跨語言統一的「應用程式版本號」解析（單一版本契約）
#
# 設計目的（解決版本號痛點）：
#   原本 Java 讀 pom.xml、Node 讀 package.json、Go 讀 VERSION/CHANGELOG，
#   三套 inline 邏輯散落各 {lang}-archive.sh，加語言／改規則要巡多處。
#   本檔收斂為「單一解析函數 + 單一優先序」，各語言只負責提供自己的原生版本號，
#   其餘（VERSION 檔、CHANGELOG、git、保底）由此處統一處理。
#
# 解析優先序（resolve_app_version）：
#   1. VERSION 檔        ← 跨語言統一契約（opt-in，最高優先；專案根目錄放一行版本號即生效）
#   2. 語言原生來源      ← 由呼叫端傳入（Java: mvnw project.version；Node: package.json version；Go: 無，傳空）
#   3. CHANGELOG.md      ← 首個 `## [x.y.z]`（Keep a Changelog 慣例）
#   4. git describe      ← 最近的 annotated tag
#   5. 0.0.0             ← 保底，永不讓版本號為空
#
# 用法：
#   source "${SCRIPT_DIR}/common/version.sh"
#   APP_VERSION="$(resolve_app_version "${LANGUAGE}" "${NATIVE_VERSION}")"
#
# ⚠ Java + VERSION 檔注意事項（SRP：命名歸命名，建置歸建置）：
#   若採 VERSION 檔作為 Java 專案版本來源，pom.xml 需設 <version>${revision}</version>
#   並以 `-Drevision=$(cat VERSION)` 建置，否則 artifact 命名（來自 VERSION）會與
#   mvn 產出的 jar 內部版本分歧。未放 VERSION 檔時 pom.xml 為權威，行為與舊版完全一致。

# ── VERSION 檔（跨語言統一契約）──────────────────────────────────────────────
# 有值 → echo 並 return 0；無檔或空 → return 1（供 `&&` 短路）
version_from_file() {
    local workspace="${WORKSPACE:-$(pwd)}"
    local file="${workspace}/VERSION"
    [[ -f "${file}" ]] || return 1
    local v
    # 去除前導 v、空白、Tab、CR；只取第一行
    v="$(head -1 "${file}" | tr -d ' \t\r' | sed 's/^v//')"
    [[ -n "${v}" ]] || return 1
    echo "${v}"
}

# ── CHANGELOG.md 首個 SemVer 標題 ────────────────────────────────────────────
version_from_changelog() {
    local workspace="${WORKSPACE:-$(pwd)}"
    local file="${workspace}/CHANGELOG.md"
    [[ -f "${file}" ]] || return 1
    local v
    v="$(grep -m1 -oE '^## \[[0-9]+\.[0-9]+\.[0-9]+[^]]*\]' "${file}" 2>/dev/null \
        | sed -E 's/^## \[([^]]+)\]/\1/')"
    [[ -n "${v}" ]] || return 1
    echo "${v}"
}

# ── git 最近 tag ─────────────────────────────────────────────────────────────
version_from_git() {
    local workspace="${WORKSPACE:-$(pwd)}"
    local v
    v="$(git -C "${workspace}" describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')"
    [[ -n "${v}" ]] || return 1
    echo "${v}"
}

# ── 統一解析（優先序 1→5）────────────────────────────────────────────────────
# 參數：
#   $1 language        — 語言（保留供未來語言特例；目前僅記錄用途）
#   $2 native_version  — 呼叫端已解析的語言原生版本號（無則傳空字串）
resolve_app_version() {
    local language="${1:-}"
    local native_version="${2:-}"
    local v

    # 1. VERSION 檔（統一契約，opt-in 覆蓋一切）
    if v="$(version_from_file)"; then echo "${v}"; return 0; fi
    # 2. 語言原生（呼叫端傳入）
    if [[ -n "${native_version}" ]]; then echo "${native_version}"; return 0; fi
    # 3. CHANGELOG.md
    if v="$(version_from_changelog)"; then echo "${v}"; return 0; fi
    # 4. git describe
    if v="$(version_from_git)"; then echo "${v}"; return 0; fi
    # 5. 保底
    echo "0.0.0"
}
