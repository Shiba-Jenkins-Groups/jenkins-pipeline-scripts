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

    # namespace / NodePort 由政策表決定；DO_DEPLOY=true 時兩者必為非空（表內不變量）
    local namespace="${DEPLOY_NAMESPACE}"
    if [[ -z "${namespace}" ]] || [[ -z "${NODE_PORT:-}" ]]; then
        report_error "DEPLOY" "004" "DO_DEPLOY=true but DEPLOY_NAMESPACE/NODE_PORT empty. Check branch-policy.sh table."
        exit 1
    fi

    # k8s/ 目錄由各專案提供，包含 deployment.yaml / service.yaml（含 envsubst 佔位符）
    if [[ ! -d "${WORKSPACE}/k8s" ]]; then
        report_error "DEPLOY" "001" "k8s/ directory not found in workspace. Please add k8s/ manifests to the project."
        exit 1
    fi

    # k3s pod 位於 jenkins-network，直接使用 Harbor 內部地址（不繞 localhost）
    # k3s pull image 地址：預設 host.docker.internal:9290（Docker Desktop 本地環境）
    # 雲端環境請透過 HARBOR_K3S_REGISTRY env var 覆蓋為真實 Harbor address
    local k3s_registry="${HARBOR_K3S_REGISTRY:-host.docker.internal:9290}"

    # envsubst 只替換已 export 的環境變數
    # APP_NAME 由 build.env source 取得，需明確 export 才能被 envsubst 看到
    export APP_NAME
    # HARBOR_IMAGE 走與 push 相同的產生器，避免兩處字串各自漂移（見 common/docker.sh）
    export HARBOR_IMAGE
    HARBOR_IMAGE="$(harbor_image_ref "${k3s_registry}" "${APP_NAME}" "${BRANCH}" "${APP_VERSION}" "${BUILD_NUMBER}")"
    export NAMESPACE="${namespace}"
    # NODE_PORT 由呼叫端（ciPipeline.groovy 的 devNodePort/prodNodePort 參數）提供，此處僅確保 export 供 envsubst 使用
    export NODE_PORT
    # TTL 回收依據：ttl-janitor CronJob 讀此 annotation 判斷 Deployment 存活時間（見 k8s-ops/ttl-janitor）
    export DEPLOY_TIMESTAMP
    DEPLOY_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    echo "[cd] Deploying to namespace: ${namespace}"
    echo "[cd] Image: ${HARBOR_IMAGE}"
    echo "[cd] Deploy timestamp: ${DEPLOY_TIMESTAMP}"

    # envsubst 替換 manifest 佔位符（${APP_NAME} / ${HARBOR_IMAGE} / ${NAMESPACE} / ${NODE_PORT} / ${DEPLOY_TIMESTAMP}）
    # 產生渲染後的臨時 manifest，避免污染原始 k8s/ 目錄
    local rendered="${WORKSPACE}/.pipeline/k8s-rendered"
    mkdir -p "${rendered}"
    for f in "${WORKSPACE}/k8s/"*.yaml; do
        envsubst < "${f}" > "${rendered}/$(basename "${f}")"
    done

    # per-namespace overlay（選配）：專案可放 k8s/<namespace>/*.yaml 蓋掉同檔名 base manifest
    # 或新增檔案——只在該 namespace 部署時生效，其餘專案未建此目錄即無影響（零行為變更）。
    if [[ -d "${WORKSPACE}/k8s/${namespace}" ]]; then
        for f in "${WORKSPACE}/k8s/${namespace}/"*.yaml; do
            [[ -e "${f}" ]] || continue
            envsubst < "${f}" > "${rendered}/$(basename "${f}")"
        done
    fi

    # 【S9 換 pod 前 DB 快照已於 2026-07-19 移除】
    # 原本在此對 prod PVC 內的 SQLite 做 VACUUM INTO，補「image 可回滾但 DB 不可逆」的缺口。
    # 移除原因有二：(1) 架構回退後真資料不再進 pod（pod＝驗證閘、跑空庫），這裡無資料可快照；
    # (2) 它寫死了某個專案的 DB 檔名與路徑，本就不該長在跨專案的共用 library 裡（SRP）。
    # 該責任已轉生到專案端「換版的原子單元」：shiba-go-ditch-api-project 的
    # scripts/prod_app.sh deploy（快照→停舊→起新→身分斷言→失敗自動回滾）。
    # ⚠ 未來若有專案要讓 pipeline 直接部署「有狀態」的生產 pod，快照這件事必須先補回來。

    # KUBECONFIG 由 ciPipeline.groovy withCredentials(file) 注入至環境變數
    kubectl apply -f "${rendered}/" -n "${namespace}" \
        || { report_error "DEPLOY" "002" "kubectl apply failed for namespace ${namespace}."; exit 1; }

    # 等待 Deployment rollout 完成（120 秒逾時）
    kubectl rollout status deployment/"${APP_NAME}" -n "${namespace}" --timeout=120s \
        || {
            report_error "DEPLOY" "003" "Rollout timeout for ${APP_NAME} in ${namespace}."
            echo "[cd] === Pod Status ===" >&2
            kubectl get pods -n "${namespace}" -l "app=${APP_NAME}" >&2 || true
            echo "[cd] === Recent Pod Logs ===" >&2
            kubectl logs -n "${namespace}" -l "app=${APP_NAME}" --tail=50 >&2 || true
            exit 1
        }

    # ── 驗證閘：NodePort 上的 health 端點必須回 200 ────────────────────────────
    # rollout status 只證明 readinessProbe 過（叢集內部視角）；這一步多驗 Service→Pod 的
    # 對外接線。對「部署後即撤」的專案而言這是最後一次能驗證的機會，故不可省。
    # health 路徑沿用專案既有的 smoke-test.env 契約（SMOKE_HEALTH_PATH），不另立新設定。
    local health_path=""
    if [[ -f "${WORKSPACE}/smoke-test.env" ]]; then
        # ⚠ 結尾 `|| true` 是必要的：本檔在 set -euo pipefail 下執行，檔案存在但**沒有這個 key**
        #   時 grep 回 1 ⇒ pipefail 讓 pipeline 回 1 ⇒ 賦值回 1 ⇒ set -e 靜默殺掉整個 deploy。
        #   下面的 else 分支（「未宣告 SMOKE_HEALTH_PATH ⇒ 僅以 rollout status 為準」）明白表示
        #   作者本就打算讓缺 key 是合法情形——pipefail 卻讓那條路不可達。
        #   實例：claude-project 的 smoke-test.env 只有 SPRING_AUTOCONFIGURE_EXCLUDE，
        #   於是 rollout 成功後整個 build 無聲 FAILURE（2026-07-20 build #86）。
        health_path="$(grep -E '^SMOKE_HEALTH_PATH=' "${WORKSPACE}/smoke-test.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'' || true)"
    fi
    if [[ -n "${health_path}" ]]; then
        # agent 在 jenkins-network 上，經 host.docker.internal 打 k3d 發佈到 host 的 NodePort
        local probe_url="http://host.docker.internal:${NODE_PORT}${health_path}"
        echo "[cd] 驗證部署後對外可用性：${probe_url}"
        local ok=0
        for _ in $(seq 1 10); do
            if curl -sf -m 5 -o /dev/null "${probe_url}"; then ok=1; break; fi
            sleep 3
        done
        if [[ "${ok}" != "1" ]]; then
            report_error "DEPLOY" "006" "部署後 ${probe_url} 未回 200：pod 起來了但 Service 對外接線不通。資源保留供查錯（不執行 teardown）。"
            kubectl get svc,pods -n "${namespace}" -l "app=${APP_NAME}" >&2 || true
            exit 1
        fi
        echo "[cd] ✅ 對外 health 檢查通過"
    else
        echo "[cd] 專案未宣告 smoke-test.env 的 SMOKE_HEALTH_PATH ⇒ 僅以 rollout status（readinessProbe）為準"
    fi

    echo "[cd] Deploy complete: http://localhost:${NODE_PORT}"

    # ── 驗證後即撤（DEPLOY_TEARDOWN=true 的專案）──────────────────────────────
    # 適用於「k3d pod 只是 CI/CD 驗證閘、不是執行體」的專案：image 能起來、對外接線通，
    # 這個 pod 的任務就結束了。續留只是佔資源並讓 NodePort 一直對區網開著。
    #
    # ⚠ 只在**驗證成功**時撤：失敗路徑上面已 exit，資源刻意留著供查錯——
    #   把失敗現場一起刪掉等於銷毀唯一的證據。
    # ⚠ ttl-janitor 不因此退場，它變成**失敗／中斷路徑的兜底**：那些情況不會走到這裡，
    #   留下的資源仍需要有人回收（見 k8s-ops/ttl-janitor）。兩者互補不重疊。
    # ⚠ 預設關閉：其他專案的 k3d deployment 可能是常駐開發環境而非驗證閘
    #   （實測 claude-project 的 dev deployment 已存活 108 天且無 last-deploy annotation），
    #   一律撤除會直接毀掉別人的環境。故由各專案 Jenkinsfile 明確 opt-in。
    if [[ "${DEPLOY_TEARDOWN:-false}" == "true" ]]; then
        echo "[cd] DEPLOY_TEARDOWN=true ⇒ 驗證已通過，回收本次驗證用資源"
        # 先 Service 後 Deployment：與 ttl-janitor 同序——先斷流量入口，
        # Deployment 留到最後當「刪除未完成」的重試錨點（Service 刪失敗就不動 Deployment）。
        if kubectl delete service "${APP_NAME}" -n "${namespace}" --ignore-not-found; then
            kubectl delete deployment "${APP_NAME}" -n "${namespace}" --ignore-not-found \
                || echo "[cd] WARN: deployment 刪除失敗，ttl-janitor 會在 TTL 到期後接手"
        else
            echo "[cd] WARN: service 刪除失敗，保留 deployment 當重試錨點；ttl-janitor 會接手"
        fi
        echo "[cd] ✅ 驗證閘生命週期結束（CI/CD 完成）"
    fi
}

# ── Stage 分派 ────────────────────────────────────────────────────────────────
case "${STAGE}" in
    docker-build)  docker_build_if_needed ;;
    image-scan)    image_scan_if_needed ;;
    harbor-push)   harbor_push_if_needed ;;
    deploy)        deploy_if_needed ;;
    all)           docker_build_if_needed; image_scan_if_needed; harbor_push_if_needed; deploy_if_needed ;;
    *)
        echo "[ERROR] Unknown stage: ${STAGE}. Use: docker-build | image-scan | harbor-push | deploy | all" >&2
        exit 1
        ;;
esac

echo "[cd] ${STAGE} completed."
