#!/usr/bin/env bash
# cd.sh — CD 入口
# 用法：cd.sh [docker-build | image-scan | harbor-push | deploy | all]
#   無參數或 all：依序執行全部
#   指定參數：只執行該 stage（供 ciPipeline.groovy 拆分 stage 使用）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/common/error-handler.sh"
source "${SCRIPT_DIR}/common/docker.sh"
source "${SCRIPT_DIR}/common/nexus-upload.sh"

STAGE="${1:-all}"

# ── 讀取 Archive 階段寫入的 build.env ────────────────────────────────────────
BUILD_ENV="${WORKSPACE:-$(pwd)}/.pipeline/build.env"
if [[ -f "${BUILD_ENV}" ]]; then
    # shellcheck source=/dev/null
    source "${BUILD_ENV}"
else
    # build.env 由 Archive stage 寫入，找不到代表上游 stage 失敗或未執行
    # WARNING 改為 ERROR：避免以空值繼續執行，造成下游難以追查的假性失敗
    report_error "CD" "001" "build.env not found at ${BUILD_ENV}. Did Archive stage succeed?"
    exit 1
fi

BRANCH="${BRANCH:-${GIT_BRANCH:-unknown}}"
BRANCH="${BRANCH#origin/}"

# ── branch 政策旗標 ───────────────────────────────────────────────────────────
# Pipeline 情境：Detect stage 已推導並注入 env（DO_* / SCAN_EXIT_CODE / ...）
# standalone / all 情境：旗標未注入時自行推導（單一真相仍在 branch-policy.sh）
if [[ -z "${DO_DOCKER_BUILD:-}" ]]; then
    # shellcheck source=common/branch-policy.sh
    source "${SCRIPT_DIR}/common/branch-policy.sh"
    derive_branch_policy "${BRANCH}"
fi

APP_NAME="${APP_NAME:?APP_NAME is required}"
APP_VERSION="${APP_VERSION:?APP_VERSION is required}"
BUILD_NUMBER="${BUILD_NUMBER:?BUILD_NUMBER is required}"
ARTIFACT_NAME="${ARTIFACT_NAME:-}"
RUNTIME_VERSION="${RUNTIME_VERSION:-17}"
LANGUAGE="${LANGUAGE:-java}"

# build.env is sourced into this shell, but deploy/cleanup are delegated to a
# child Bash process. Export the resolved build identity so the child receives
# the same application/version context instead of failing under `set -u`.
export APP_NAME APP_VERSION BUILD_NUMBER BRANCH ARTIFACT_NAME RUNTIME_VERSION LANGUAGE

IMAGE_TAG="${APP_NAME}:${APP_VERSION}-${BUILD_NUMBER}"

echo "[cd] Stage: ${STAGE}"
echo "[cd] Branch: ${BRANCH}"
echo "[cd] Image: ${IMAGE_TAG}"

# ── Docker Build ──────────────────────────────────────────────────────────────
docker_build_if_needed() {
    # 政策旗標由 branch-policy.sh 單一真相表決定，本函數不自帶 branch case
    if [[ "${DO_DOCKER_BUILD}" != "true" ]]; then
        echo "[cd] Branch '${BRANCH}' — skipping Docker build (DO_DOCKER_BUILD=${DO_DOCKER_BUILD})."
        return 0
    fi

    # 產出物需複製至 .pipeline/（Docker build context 內）
    # 才能被 Dockerfile 的 COPY ${JAR_FILE} app.jar 正確引用
    #
    # 取檔優先序（#4b 起 release/ 共享單槽已退役，僅兩來源，皆無＝loud fail）：
    #   ① ARTIFACT_LOCAL     — 本 build 於 agent 內的 staging 檔（零網路，同 run 不可能被他 job 動到）
    #   ② NEXUS_ARTIFACT_URL — 權威保管庫（raw-artifacts）版本化路徑下載
    local jar_dest="${WORKSPACE}/.pipeline/${ARTIFACT_NAME}"

    if [[ -n "${ARTIFACT_LOCAL:-}" ]] && [[ -f "${ARTIFACT_LOCAL}" ]]; then
        echo "[cd] Using local staged artifact: ${ARTIFACT_LOCAL}"
        cp "${ARTIFACT_LOCAL}" "${jar_dest}"
    elif [[ -n "${NEXUS_ARTIFACT_URL:-}" ]]; then
        echo "[cd] Downloading artifact from Nexus: ${NEXUS_ARTIFACT_URL}"
        nexus_download_artifact "${NEXUS_ARTIFACT_URL}" "${jar_dest}"
    else
        report_error "DOCKER" "001" "No artifact source: ARTIFACT_LOCAL and NEXUS_ARTIFACT_URL both unavailable. Check Archive stage output (build.env)."
        exit 1
    fi

    local build_args="--build-arg APP_NAME=${APP_NAME} \
                      --build-arg APP_VERSION=${APP_VERSION} \
                      --build-arg BUILD_NUMBER=${BUILD_NUMBER} \
                      --build-arg BRANCH=${BRANCH} \
                      --build-arg RUNTIME_VERSION=${RUNTIME_VERSION} \
                      --build-arg REGISTRY_PREFIX=${REGISTRY_PREFIX:-} \
                      --build-arg JAR_FILE=.pipeline/${ARTIFACT_NAME} \
                      --build-arg ARTIFACT_FILE=.pipeline/${ARTIFACT_NAME}"
    # Product-scoped source identity for the controlled runtime verifier.
    if [[ "${APP_NAME}" == shiba-go-ditch-api-project ]]; then
        [[ "${GIT_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || { echo '[cd] Missing product commit identity' >&2; return 1; }
        local artifact_sha
        artifact_sha="$(sha256sum "${jar_dest}" | awk '{print $1}')"
        [[ "${artifact_sha}" =~ ^[0-9a-f]{64}$ ]] || return 1
        build_args="${build_args} --build-arg GIT_COMMIT=${GIT_COMMIT} --build-arg ARTIFACT_SHA256=${artifact_sha}"
    fi
    # ARTIFACT_FILE：語言中立的通用名（Dockerfile-go 使用）；JAR_FILE 保留 Java 向下相容
    # REGISTRY_PREFIX：base image 來源前綴（agent env 提供；空＝Docker Hub 直抓）
    docker_build "${IMAGE_TAG}" "${LANGUAGE}" "${build_args}"

    # build context 用完後清理臨時 JAR
    rm -f "${jar_dest}"
}

# ── Harbor Push ───────────────────────────────────────────────────────────────
harbor_push_if_needed() {
    if [[ "${DO_PUSH}" != "true" ]]; then
        echo "[cd] Branch '${BRANCH}' — skipping Harbor push (DO_PUSH=${DO_PUSH})."
        return 0
    fi

    local registry="${HARBOR_REGISTRY:-localhost:9290}"
    # 參照格式的單一產生點（見 common/docker.sh 的 harbor_image_ref）
    local harbor_image
    harbor_image="$(harbor_image_ref "${registry}" "${APP_NAME}" "${BRANCH}" "${APP_VERSION}" "${BUILD_NUMBER}")"

    # Harbor credentials 由 ciPipeline.groovy withCredentials 注入
    # docker login 失敗時包裝業務層說明，避免只看到 docker daemon 原始訊息
    echo "${HARBOR_PASS}" | docker login "${registry}" \
        --username "${HARBOR_USER}" \
        --password-stdin \
        || { report_error "HARBOR" "001" "docker login failed for ${registry}. Check harbor credentials in Jenkins (ID: harbor-robot-*)."; exit 1; }

    echo "[cd] Tagging: ${IMAGE_TAG} → ${harbor_image}"
    docker tag "${IMAGE_TAG}" "${harbor_image}"

    echo "[cd] Pushing: ${harbor_image}"
    docker push "${harbor_image}"

    docker logout "${registry}"
    echo "[cd] Harbor push completed: ${harbor_image}"

    # 把「這次產出哪一顆」寫成產出物，讓收尾摘要與 archiveArtifacts 有東西可交（見 docker.sh）。
    # 寫檔失敗不得讓已成功的 push 變成紅的——產出物是便利性，不是交付條件。
    write_image_ref_file "${harbor_image}" || echo "[cd] WARNING: image-ref.txt 寫入失敗（不影響 push 結果）"
}

# ── Image Scan（Trivy）────────────────────────────────────────────────────────
image_scan_if_needed() {
    if [[ "${DO_SCAN}" != "true" ]]; then
        echo "[cd] Branch '${BRANCH}' — skipping image scan (DO_SCAN=${DO_SCAN})."
        return 0
    fi

    # SCAN_EXIT_CODE 由政策表決定：0＝僅警告不阻斷（main）；1＝HIGH/CRITICAL 即 fail（prod）
    local trivy_exit_code="${SCAN_EXIT_CODE}"

    # trivy-results.xml 輸出至 WORKSPACE 根目錄，供 ciPipeline.groovy junit step 收集
    # trivy-cache 存於 WORKSPACE 下，隨 cleanWs 清理（避免額外 volume 掛載）
    local trivy_report="${WORKSPACE:-$(pwd)}/trivy-results.xml"
    local trivy_raw_report="${WORKSPACE:-$(pwd)}/trivy-results-raw.json"
    local trivy_cache="${WORKSPACE:-$(pwd)}/.trivy-cache"

    # Trivy 不支援 --format junit，需透過 template 輸出 JUnit XML
    # template 路徑為 Trivy 安裝時內建，固定於 /usr/local/share/trivy/templates/junit.tpl
    local trivy_template="/usr/local/share/trivy/templates/junit.tpl"

    # 專案可選擇性放 WORKSPACE 根目錄 .trivyignore（逐條記錄「目前無可用修補」理由＋
    # 到期日）——不存在則不加 --ignorefile，其餘專案零行為變更。
    local ignore_args=()
    local trivy_ignorefile="${WORKSPACE:-$(pwd)}/.trivyignore"
    if [[ -f "${trivy_ignorefile}" ]]; then
        echo "[cd] 套用 .trivyignore（${trivy_ignorefile}）"
        ignore_args=(--ignorefile "${trivy_ignorefile}")
    fi

    # Raw evidence 與 gate 分離：raw 永不套 ignore、永不直接改變 build result；filtered
    # scan 才依 branch policy 與專案例外決定 gate。只對明確 opt-in 專案產生，避免所有
    # 既有 pipeline 無條件增加一次 registry/database scan。
    if [[ "${TRIVY_RAW_REPORT_ENABLED:-false}" == "true" ]]; then
        echo "[cd] Writing unfiltered Trivy evidence: ${trivy_raw_report}"
        trivy image \
            --exit-code 0 \
            --severity HIGH,CRITICAL \
            --scanners vuln \
            --cache-dir "${trivy_cache}" \
            --format json \
            --output "${trivy_raw_report}" \
            "${IMAGE_TAG}"
    fi

    echo "[cd] Running Trivy image scan: ${IMAGE_TAG} (branch: ${BRANCH}, exit-code: ${trivy_exit_code})"
    trivy image \
        --exit-code "${trivy_exit_code}" \
        --severity HIGH,CRITICAL \
        --cache-dir "${trivy_cache}" \
        --format template \
        --template "@${trivy_template}" \
        --output "${trivy_report}" \
        "${ignore_args[@]}" \
        "${IMAGE_TAG}"
    echo "[cd] Image scan completed: ${trivy_report}"
}

# ── Deploy（kubectl apply to k3s）─────────────────────────────────────────────
deploy_if_needed() {
    if [[ "${DO_DEPLOY}" != "true" ]]; then
        echo "[cd] Branch '${BRANCH}' — skipping deploy (DO_DEPLOY=${DO_DEPLOY})."
        return 0
    fi
    bash "${SCRIPT_DIR}/common/k3d-verify.sh" deploy
}

# ── Stage 分派 ────────────────────────────────────────────────────────────────
case "${STAGE}" in
    docker-build)  docker_build_if_needed ;;
    image-scan)    image_scan_if_needed ;;
    harbor-push)   harbor_push_if_needed ;;
    deploy)        deploy_if_needed ;;
    cleanup)       bash "${SCRIPT_DIR}/common/k3d-verify.sh" cleanup ;;
    all)           docker_build_if_needed; image_scan_if_needed; harbor_push_if_needed; deploy_if_needed ;;
    *)
        echo "[ERROR] Unknown stage: ${STAGE}. Use: docker-build | image-scan | harbor-push | deploy | cleanup | all" >&2
        exit 1
        ;;
esac

echo "[cd] ${STAGE} completed."
