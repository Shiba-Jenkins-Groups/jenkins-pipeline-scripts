#!/usr/bin/env bash
# common/docker.test.sh — harbor_image_ref 的迴歸測試
#
# 為何需要：這個函數是**四個專案共用的 image 命名單一真相**（push／deploy／smoke 三處都呼叫它）。
# 它一旦產出不合法或不一致的參照，症狀會出現在很後面的 stage（push 失敗、k8s ImagePullBackOff、
# smoke 拉不到），而且每個專案的版號格式不同，壞掉的往往只有其中一個專案。
#
# 用法：bash resources/scripts/common/docker.test.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=docker.sh
source "${SCRIPT_DIR}/docker.sh"

fail=0
assert_ref() {
    local desc="$1" want="$2"; shift 2
    local got; got="$(harbor_image_ref "$@")"
    if [[ "${got}" == "${want}" ]]; then
        echo "PASS: ${desc} → ${got}"
    else
        echo "FAIL: ${desc}"
        echo "        got : ${got}"
        echo "        want: ${want}"
        fail=$((fail + 1))
    fi
}

assert_file_kv() {
    local desc="$1" file="$2" key="$3" want="$4"
    local got; got="$(grep -E "^${key}=" "${file}" | head -1 | cut -d= -f2-)"
    if [[ "${got}" == "${want}" ]]; then
        echo "PASS: ${desc}（${key}=${got}）"
    else
        echo "FAIL: ${desc}"
        echo "        got : ${key}=${got}"
        echo "        want: ${key}=${want}"
        fail=$((fail + 1))
    fi
}

echo "── 各專案的實際版號格式（跨專案一致性）──────────────────────────────"
assert_ref "go-ditch prod" \
    "localhost:9290/shiba-go-ditch-api-project/prod/0.67.2:12" \
    localhost:9290 shiba-go-ditch-api-project prod 0.67.2 12
assert_ref "go-ditch develop" \
    "localhost:9290/shiba-go-ditch-api-project/develop/0.67.0:21" \
    localhost:9290 shiba-go-ditch-api-project develop 0.67.0 21
assert_ref "woof main（RC 後綴需轉小寫）" \
    "localhost:9290/woof-woof-project/main/1.2.0-rc:11" \
    localhost:9290 woof-woof-project main 1.2.0-RC 11

echo "── ⚠ 大寫版號：OCI 規範要求 repository 全小寫，不轉就是非法參照 ────────"
# claude-project 的版號是 0.0.1-SNAPSHOT。舊格式把版本放 tag（容許大寫）所以沒事；
# 移進 repository 路徑後若不轉小寫，docker 會直接 invalid reference format。
# 這條是「其他專案比照此機制」時最容易炸的一點，故獨立列出。
assert_ref "claude develop（SNAPSHOT 轉小寫）" \
    "localhost:9290/claude-project/develop/0.0.1-snapshot:82" \
    localhost:9290 claude-project develop 0.0.1-SNAPSHOT 82

echo "── branch 含斜線：不得多切出一層路徑（會破壞「第二層＝環境」語意）────"
assert_ref "feature/new-ui → feature-new-ui" \
    "localhost:9290/app/feature-new-ui/0.1.0:3" \
    localhost:9290 app feature/new-ui 0.1.0 3

echo "── registry 變體：k3s 走 host.docker.internal，格式須與 push 完全一致 ──"
assert_ref "k3s registry" \
    "host.docker.internal:9290/shiba-go-ditch-api-project/prod/0.67.2:12" \
    host.docker.internal:9290 shiba-go-ditch-api-project prod 0.67.2 12

echo "── 產出必須是 docker 認得的合法參照（拿 docker 自己驗，不靠人眼）──────"
# 用 docker tag 對一個已知存在的 image 試貼標籤：參照非法時 docker 會拒絕。
# 沒有 docker 或沒有測試用 image 時跳過（測試不該因環境缺件而假失敗）。
if command -v docker >/dev/null 2>&1 && docker image inspect alpine >/dev/null 2>&1; then
    for ref in \
        "$(harbor_image_ref localhost:9290 claude-project develop 0.0.1-SNAPSHOT 82)" \
        "$(harbor_image_ref localhost:9290 shiba-go-ditch-api-project prod 0.67.2 12)" ; do
        if docker image tag alpine "${ref}" 2>/dev/null; then
            echo "PASS: docker 接受參照 ${ref}"
            docker rmi "${ref}" >/dev/null 2>&1
        else
            echo "FAIL: docker 拒絕參照 ${ref}"
            fail=$((fail + 1))
        fi
    done
    # 反向確認：未轉小寫的版本確實會被 docker 拒絕（證明上面那條斷言不是裝飾）
    if docker image tag alpine "localhost:9290/claude-project/develop/0.0.1-SNAPSHOT:82" 2>/dev/null; then
        echo "FAIL: docker 竟接受大寫 repository——本測試的小寫斷言失去意義，請重新確認前提"
        docker rmi "localhost:9290/claude-project/develop/0.0.1-SNAPSHOT:82" >/dev/null 2>&1
        fail=$((fail + 1))
    else
        echo "PASS: docker 如預期拒絕大寫 repository（小寫轉換確有必要）"
    fi
else
    echo "SKIP: 無 docker 或無 alpine image，略過合法性實測"
fi

echo
echo "── write_image_ref_file：產出物內容與韌性 ────────────────────────────"
# 為何測：這個檔是「pipeline 產出哪一顆 image」的唯一機讀來源（收尾摘要與 archiveArtifacts
# 都吃它）。寫錯 key 名不會讓 build 變紅，只會讓下游安靜地拿不到值。
TMPWS="$(mktemp -d)"
(
    export WORKSPACE="${TMPWS}" APP_NAME=demo-app BRANCH=prod APP_VERSION=1.2.3 BUILD_NUMBER=42
    write_image_ref_file "localhost:9290/demo-app/prod/1.2.3:42" >/dev/null
)
REF_FILE="${TMPWS}/image-ref.txt"
if [[ -f "${REF_FILE}" ]]; then
    assert_file_kv "IMAGE_REF 為 push 側（host 可用）參照" "${REF_FILE}" IMAGE_REF "localhost:9290/demo-app/prod/1.2.3:42"
    assert_file_kv "帶出 APP_NAME"     "${REF_FILE}" APP_NAME     "demo-app"
    assert_file_kv "帶出 BRANCH"       "${REF_FILE}" BRANCH       "prod"
    assert_file_kv "帶出 APP_VERSION"  "${REF_FILE}" APP_VERSION  "1.2.3"
    assert_file_kv "帶出 BUILD_NUMBER" "${REF_FILE}" BUILD_NUMBER "42"
    # 不存在的 image → digest 取不到，但不得讓函數失敗（產出物是便利性，不是交付條件）
    if grep -qE '^IMAGE_DIGEST=' "${REF_FILE}"; then
        echo "PASS: image 不存在時 IMAGE_DIGEST 仍留空鍵、未讓函數失敗"
    else
        echo "FAIL: 缺 IMAGE_DIGEST 鍵"; fail=$((fail + 1))
    fi
    # SRP 守門：本 library 跨四專案共用，不得混入任何專案專屬的部署指令
    if grep -qiE 'prod_app\.sh|kubectl|helm' "${REF_FILE}"; then
        echo "FAIL: 產出物混入專案專屬部署指令（違反跨專案 SRP）"; fail=$((fail + 1))
    else
        echo "PASS: 產出物只含事實、無專案專屬指令"
    fi
else
    echo "FAIL: 未產出 image-ref.txt"; fail=$((fail + 1))
fi
rm -rf "${TMPWS}"

echo "────────────────────────────────────────────────────────────"
if [[ ${fail} -eq 0 ]]; then
    echo "✅ docker.sh 全數通過"
else
    echo "❌ docker.sh 有 ${fail} 項失敗"
fi
exit ${fail}
