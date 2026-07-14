# Changelog

本檔案依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.0.0/) 格式，
版號遵循 [Semantic Versioning（語意化版本）](https://semver.org/lang/zh-TW/)。

## [Unreleased]

---

## [1.18.0] - 2026-07-14

### Added
- **OWASP Dependency-Check 第三方依賴 CVE 掃描（Security Phase 2 v1.7.1）**
  - 新增 CI 階段「Dependency Scan」（Test 後、Archive 前）：`common/dependency-check.sh` 用 `dependency-check-maven` 12.2.2
  - `branch-policy.sh` 新增 `DO_DEP_SCAN` / `DEP_SCAN_CVSS`：main **warn**（`failBuildOnCVSS=11` 只報告）／prod **fail**（CVSS≥7）；develop/feature 不掃
  - 僅 Java/Maven（腳本內 guard）；Node/Go 跳過（依賴 CVE 掃描屬另一分析器，暫不做）
  - NVD API key 由 Jenkins credential `nvd-api-key` 綁定注入（`-DnvdApiKey`；console 自動 mask）
  - NVD DB 自動持久化於 Maven localRepository（`jenkins-agent-cache` volume，沿用 #5，免建新 volume）；首次同步較慢
  - 報告 `target/dependency-check-report.html`（ciPipeline 已 publishHTML）

---

## [1.17.0] - 2026-07-14

### Added
- **秘密掃描 gitleaks（Security Phase 2 v1.7.0）**
  - 新增 CI 階段「Secret Scan」（置 Build 前 fail fast）：`common/secret-scan.sh` 用 `gitleaks git` 掃 git 歷史
  - `branch-policy.sh` 新增旗標 `DO_SECRET_SCAN` / `SECRET_SCAN_EXIT_CODE`（全 branch true/1——秘密洩漏處處 critical，feature 也擋；獨立於 Trivy 的 DO_SCAN）
  - `--redact`：報告與 log 遮罩秘密實際值（找到秘密的報告不二次曝露）；產出 `gitleaks-report.json`（ciPipeline 已 archive）
  - 專案可放 `.gitleaks.toml`（自訂規則）／`.gitleaksignore`（豁免已知誤判），gitleaks 自動採用
  - 前置：agent `gitleaks` 8.30.1 加 SHA256 checksum 驗證（jenkins-infra；供應鏈硬化）
  - 本地實測（agent image）：含秘密 repo→exit 1（private-key/github-pat）、報告 redact 無明文、clean repo→exit 0

---

## [1.16.0] - 2026-07-14

### Changed
- **報告路徑契約化（語言中立，改善計畫 #2）**：`ciPipeline.groovy` 不再硬編 Maven 路徑
  - Test stage `junit` 由 `**/target/surefire-reports/*.xml` 改讀 `reports/junit/*.xml`
  - Coverage `publishHTML` 由 `target/site/jacoco` 改讀 `reports/coverage/index.html`（reportName 中立化）
  - 各語言 test 腳本統一把產物搬到契約路徑：
    - Java：`collect_reports` 搬 surefire/test-results → `reports/junit`、JaCoCo → `reports/coverage`
    - Go：改用 `gotestsum` 產 JUnit XML → `reports/junit`（測試失敗仍寫檔）、`go tool cover -html` → `reports/coverage`
    - Node：best-effort 蒐集常見 junit/coverage 產物（reporter 依專案，`allowEmptyResults` 容錯）
  - 前置：agent image 裝 `gotestsum` 1.13.0（jenkins-infra；pinned 比照 Trivy/gitleaks）

### Security
- **`git-tag.sh` 憑證改走 `GIT_ASKPASS`（改善計畫 #8）**：`push_git_tag` 不再把 `user:token` 拼進 remote URL
  - 避免 token 洩漏於 `ps` 命令列 args（Jenkins console mask 不涵蓋）與 git push 失敗訊息
  - 憑證以 env 傳給臨時 askpass script，URL 為純網址；函數返回時清理臨時檔

---

## [1.15.0] - 2026-07-14

### Removed
- **本地 release/backup 輪替退役——4b 收斂階段（改善計畫 #4，單一真相在 Nexus）**
  - 刪除 `common/archive-base.sh`（release→backup 輪替、MAX_BACKUPS 手工保留）；
    三語言 `{lang}-archive.sh` 移除 `archive_artifact` 雙寫，只上傳 Nexus
  - `cd.sh` 取檔鏈移除 ③ release/ 共享單槽 fallback：僅
    `ARTIFACT_LOCAL`（同 run staging）→ `NEXUS_ARTIFACT_URL`（權威下載），
    兩者皆無＝loud fail（DOCKER-001）；`ARTIFACTS_ROOT` 變數一併移除
  - `ciPipeline.groovy` Load Scripts 清單移除 `archive-base.sh`
  - 宿主機 `builds/` 目錄不再被寫入（實體目錄清除由 Shiba 另行決定）

### Changed
- Nexus retention 定案：cleanup policy `lastBlobUpdated > 90d`（D2 修訂，60d → 90d），
  全 repo 單一政策起步；main/prod per-path 永久保留待 UI 確認 regex 支援度

---

## [1.14.0] - 2026-07-14

### Added
- **Artifact 歸家 Nexus——4a 雙寫階段（改善計畫 #4）**
  - 新增 `common/nexus-upload.sh`：raw hosted repo（`raw-artifacts`）上傳/下載；
    路徑契約 `{app}/{branch}/{version}-{build}-{sha7}/{filename}`（sha7 抗 #6
    Multibranch buildNumber 歸零撞路徑）；憑證一律經 netrc 暫存檔（0600＋用畢即刪），
    不進 argv/URL（不重蹈 git-tag.sh 憑證拼 URL，見 #8）
  - 三語言 `{lang}-archive.sh`：本地 release/backup 輪替照舊＋上傳 Nexus（雙寫）；
    staging 檔不再 rm（拋棄式 agent 隨容器回收），`build.env` 新增
    `ARTIFACT_LOCAL`／`NEXUS_ARTIFACT_URL` 兩欄
  - `ciPipeline.groovy`：新增選用參數 `nexusCredentials`（預設 `nexus-ci-deploy`，
    全專案共用部署帳號，各專案 Jenkinsfile 零改動）；environment 注入
    `NEXUS_CRED_USR/PSW`；Load Scripts 清單納入 `nexus-upload.sh`

### Changed
- **`cd.sh` Docker Build 取檔改精確取「本 build」產出物**（競態根治）：
  優先序 `ARTIFACT_LOCAL`（agent 內 staging）→ `NEXUS_ARTIFACT_URL`（權威庫下載）→
  `release/` 共享單槽（過渡 fallback，帶 WARN；4b 退役）——
  同 app 多 job 併行不再互相擊落（develop #61 vs main #5 實證之根治）

### 前置需求（Shiba 手動閘，commit 前完成）
- Nexus：raw hosted repo `raw-artifacts`（Disable redeploy）＋部署帳號 privileges
  **browse/read/add/edit**（無 delete；edit 為 HTTP PUT 上傳必需——add 只覆蓋 POST，
  防覆蓋由 Disable redeploy 政策把關）✅ 已完成並實證（PUT=201／重傳=400）
- Jenkins：usernamePassword credential `nexus-ci-deploy` ⏳

---

## [1.13.0] - 2026-07-13

### Added
- **base image 經 registry 快取（改善計畫 #9，build-arg 注入式）**
  - `Dockerfile-go` / `Dockerfile-java`：新增 `ARG REGISTRY_PREFIX=""`，
    `FROM ${REGISTRY_PREFIX}<image>`——**預設空＝Docker Hub 直抓，行為不變**；
    本地環境由 agent env 提供 Harbor proxy-cache 前綴（如 `localhost:9290/dockerhub-proxy/library/`）
  - `cd.sh`：docker build 增傳 `--build-arg REGISTRY_PREFIX=${REGISTRY_PREFIX:-}`
  - node / python Dockerfile 為 TODO 佔位，未納入（實作時比照）

### Fixed
- `docker.sh` `tag_base_image()`：`shiba/base/` 別名改以「邏輯名」（去除來源前綴）組成——
  原邏輯以 `cut -d:` 切 tag，registry 帶 port（`localhost:9290/...`）時會把 port 冒號
  誤判為 tag 分隔；別名維持 `shiba/base/{image}:{tag}` 不隨來源改變，tag 來源用實際名

### 啟用條件（agent env `REGISTRY_PREFIX` 尚未設定，現階段 no-op）
- 待 Harbor `dockerhub-proxy` proxy-cache project 驗證可用後，
  於 agent Dockerfile 加 `ENV REGISTRY_PREFIX=...` 並 rebuild（jenkins-infra 側）

---

## [1.12.0] - 2026-07-13

### Changed
- **語言擴充點收斂（OCP）**（改善計畫 #1）——新增語言由「改 5 處」降為
  「LANGUAGES 清單加一個字串＋依慣例放好檔案」
  - `ci.sh`：刪除 `detect_language()` / `detect_build_tool()` 重複實作
    （與 detect.sh 兩份邏輯遲早 drift），改委派 `detect.sh` 單一真相；
    語言分派 case 改為慣例迴圈 `{lang}/{lang}-{build,test,archive}.sh`
  - `ciPipeline.groovy` Load Scripts：26 行硬列清單改慣例載入——
    `LANGUAGES` × `{build,test,archive,smoke-test}.sh` ＋ `Dockerfile-{lang}`；
    語言特例檔（`go-env.sh` / `java-env.sh`）收斂至 `LANG_EXTRAS` map
  - 缺檔策略採 **fail**（`libraryResource` 直接拋錯）：缺檔＝結構錯誤，不靜默跳過

### 驗證
- 本地：慣例清單 ↔ 檔案樹雙向 1:1 比對（無缺檔、無漏載）、ci.sh 委派／順序／
  go.mod 優先權／偵測失敗非零退出（7 tests）；groovy 2.4.21 編譯通過

---

## [1.11.0] - 2026-07-13

### Added
- **`common/branch-policy.sh`：branch 政策單一真相表**（改善計畫 #3）
  - `derive_branch_policy <branch>` 一次推導全部政策旗標：
    `DO_DOCKER_BUILD / DO_SCAN / SCAN_EXIT_CODE / DO_PUSH / DO_DEPLOY /
     DEPLOY_NAMESPACE / NODE_PORT / DEPLOY_INPUT_GATE / TEST_LEVEL`
  - 雙模式：執行模式（Detect stage 印 KEY=VALUE 供 groovy 注入 env）＋
    source 模式（`cd.sh all`、`{lang}-test.sh` standalone fallback 自行推導）
  - branch 來源優先序：`GIT_BRANCH` > `BRANCH_NAME`（Multibranch 預留）> `BRANCH`；取不到即 fail
  - 表內不變量：`DO_PUSH ⇒ DO_DOCKER_BUILD`、`DO_DEPLOY ⇒ DO_PUSH`、`DO_DEPLOY ⇒ ns/port 非空`

### Changed
- **政策消費端全面改讀旗標，刪除自帶 branch case**（行為與 v1.10.0 完全一致，純集中化）
  - `ciPipeline.groovy`：`CD_ENABLED` 退役；Docker Build / Image Scan / Harbor Push /
    Smoke Test / Deploy 的 `when` 改讀 `DO_*` 旗標（profile flag 雙重把關不變）；
    Deploy 人工閘改由 `DEPLOY_INPUT_GATE` 控制（groovy 不再出現 branch 字面值）
  - `cd.sh`：四個 `*_if_needed()` 改旗標閘；Trivy exit-code 改讀 `SCAN_EXIT_CODE`；
    deploy namespace / NodePort 改讀政策旗標；旗標未注入時 fallback 自行推導
  - `java-test.sh` / `go-test.sh`：branch case 改為 `TEST_LEVEL` 檔位（unit / coverage）
  - `java-test.sh`：移除 `run_security_scan()` 空殼（Security 由 Phase 2 v1.7.x 以獨立旗標實作）
  - Stage View 呈現變化：政策不啟用的 stage 由「執行後內部跳過」改為「stage 直接 skip（灰色）」，
    實際行為不變、視覺更真實
- README：CD 開關／測試範圍兩節改為政策單一真相表；目錄結構補 go / smoke-test / version.sh
- 註：roadmap「移除 develop 暫時開關」項——v1.8.0 後 develop 的 Harbor Push / Deploy
  已是 k3s dev namespace 正式交付鏈，本版**保留現行為**；若仍要關閉，
  現在只需改 branch-policy.sh 表中兩個值（此即本次集中化的價值）

---

## [1.10.0] - 2026-07-13

### Added
- **`common/version.sh`：跨語言統一的應用程式版本號解析（單一版本契約）**
  - `resolve_app_version <language> <native_version>`：優先序
    `VERSION 檔 > 語言原生（呼叫端傳入）> CHANGELOG.md > git describe > 0.0.0`
  - 專案根目錄放一行版本號的 `VERSION` 檔即為所有語言的統一權威來源（opt-in，最高優先）
  - 未放 `VERSION` 檔＝行為與舊版完全一致（Java 走 pom.xml、Node 走 package.json、Go 走 CHANGELOG/git）
  - 收斂原本散落 `java-archive.sh` / `node-archive.sh` / `go-archive.sh` 的三套 inline 版本邏輯（DRY / OCP）

### Changed
- `java-archive.sh` / `node-archive.sh` / `go-archive.sh`：APP_VERSION 改由 `resolve_app_version` 統一解析
  - `go-archive.sh` 移除 inline `read_app_version()`（優先序不變，改由 `common/version.sh` 維護）
- `ciPipeline.groovy`：Load Scripts 清單加入 `scripts/common/version.sh`
- **版本策略文件收斂為 `@main` 單一真相**：README / CLAUDE.md 移除過期版本表與 `@v1.2.x` 引用範例
  - 變更歷史的單一真相＝本 CHANGELOG；git tag `vX.Y.Z` 僅供歷史審計
  - `config/system/jenkins.yaml` `defaultVersion` 由 `v1.2.1` 更新為 `main`（fallback 對齊實際策略）

---

## [1.9.0] - 2026-07-13

### Added
- **Go 語言支援**（首個適用專案：shiba-go-ditch-api-project）
  - `detect.sh` / `ci.sh`：`go.mod` 偵測（優先於 package.json——Go 專案可能帶前端工具檔）
  - `scripts/go/go-env.sh`：讀取專案根目錄 `go-pipeline.env` 宣告
    （`GO_MAIN_PKG` 多 main 專案入口、`GO_TEST_PKGS` 測試範圍、`GO_LDFLAGS`）
  - `scripts/go/go-build.sh`：`go build ./...` 全量驗證 ＋ `CGO_ENABLED=0` 產出 artifact binary
  - `scripts/go/go-test.sh`：branch 分級——develop/其他＝`go vet`+unit test；main/prod＝＋coverage 總覽
    （`-race` 需 CGO+gcc，agent 未裝 C toolchain，race 檢測留在開發機）
  - `scripts/go/go-archive.sh`：版本來源 `VERSION` 檔 > `CHANGELOG.md` `## [x.y.z]` > `git describe`；
    命名同 Java 慣例（binary 無副檔名）；build.env / git tag 流程共用 common/
  - `scripts/go/go-smoke-test.sh`：`smoke-test.env` 的 `SMOKE_*` 鍵設定腳本層
    （health path / port / timeout），其他鍵以 `-e` 注入容器
  - `dockerfiles/Dockerfile-go`：base 用 `debian:bookworm-slim`（glibc）——
    Go 1.26 linux/arm64 ELF 帶 glibc loader（PIE 預設），即使 `CGO_ENABLED=0` 也無法跑 alpine（musl）
  - `ciPipeline.groovy`：Load Scripts 清單加入 go 腳本組與 Dockerfile-go

### Changed
- `cd.sh`：docker build 增傳 `--build-arg ARTIFACT_FILE=...`（語言中立通用名；
  `JAR_FILE` 保留 Java 向下相容）

### Infra（jenkins-infra 同步事項）
- Agent Dockerfile 加入 Go 1.26.5（pinned、arch 自動偵測；`GOTOOLCHAIN=auto`；
  `GOPROXY` 走官方 proxy——Nexus 尚無 go proxy repo，建立 go-group 後改指 Nexus）

---

## [1.8.2] - 2026-04-15

### Changed
- `ciPipeline.groovy`：`post.always` 加入 `docker image prune -f`
  - 每次 pipeline 結束（無論成功失敗）自動清除 dangling image（`<none>:<none>`）
  - 避免長期累積無 tag 殘留層佔用磁碟

---

## [1.8.1] - 2026-04-01

### Changed
- `cd.sh`：`HARBOR_K3S_REGISTRY` 預設值從 `172.20.0.4:8080` 改為 `host.docker.internal:9290`
  - 統一 Harbor push（`localhost:9290`）與 k3s pull 的底層服務端點，消除地址分裂
  - `host.docker.internal` 在 Mac Docker Desktop 環境中對 Agent container 與 k3d container 均可達
  - 雲端環境請透過 `HARBOR_K3S_REGISTRY` env var 覆蓋為真實 Harbor address

---

## [1.8.0] - 2026-04-01

### Added
- `cd.sh`：實作 `deploy_if_needed()`，透過 kubectl 部署至 k3s cluster
  - develop branch → namespace `dev`（NodePort 30090）
  - prod branch → namespace `prod`（NodePort 30091）
  - 使用 `envsubst` 替換 manifest 佔位符（`${APP_NAME}` / `${HARBOR_IMAGE}` / `${NAMESPACE}` / `${NODE_PORT}`）
  - 等待 `kubectl rollout status`（120 秒逾時），失敗時自動輸出 pod status 與 container log
  - `HARBOR_K3S_REGISTRY` 環境變數可覆蓋 Harbor 內部地址（預設 `172.20.0.4:8080`）
- `ciPipeline.groovy`：Deploy stage 加入 kubeconfig 注入與 prod 人工審核
  - `withCredentials([file(credentialsId: 'k3s-kubeconfig')])` 注入 KUBECONFIG
  - prod branch：`input` gate，需人工確認才執行部署

### Changed
- `cd.sh`：移除 `harbor_push_if_needed()` 中 `develop` 的 `TODO(暫時)` 註解（develop harbor push 為正式行為）
- `cd.sh`：移除 `deploy_if_needed()` 中 `develop` 的 `TODO(暫時)` 佔位，改為正式實作

---

## [1.6.2] - 2026-03-31

### Added
- `common/error-handler.sh`：新增 `report_error(stage, code, message)` 函數，統一業務層結構化錯誤輸出格式
  - 輸出含框線區塊，console log 可快速掃描定位問題
  - 所有 stage script 共用，錯誤碼格式：`{STAGE}-{CODE}`（例：`DOCKER-001`、`HARBOR-001`）
- `ciPipeline.groovy`：`post.always` 新增 BUILD SUMMARY 框線區塊
  - 輸出 Job 名稱、Build 號、Result（`currentBuild.currentResult`）、Duration、Build URL
- `ciPipeline.groovy`：`post.failure` 新增 FAILURE DIAGNOSIS 診斷提示區塊
- `ciPipeline.groovy`：`post.always` 新增 `archiveArtifacts gitleaks-report.json`（allowEmptyArchive，為 v1.7.0 gitleaks 預留）
- `java-smoke-test.sh`：失敗時自動輸出容器 log（`docker logs`），Spring Boot 啟動 stacktrace 直接可見

### Changed
- `common/error-handler.sh`：`trap_error()` 加入 script 名稱（`${BASH_SOURCE[1]}`），從「行號 + exit code」升級為「script 名稱 + 行號 + exit code」
- `cd.sh`：`.pipeline/build.env` 找不到時從 WARNING 升級為 ERROR（`report_error "CD" "001"`），避免以空值繼續執行產生假性失敗
- `cd.sh`：Docker Build 前加入 JAR 存在性前置檢查（`report_error "DOCKER" "001"`）
- `cd.sh`：Harbor `docker login` 失敗時加入業務層說明（`report_error "HARBOR" "001"`），提示檢查 Jenkins Credentials ID
- `ciPipeline.groovy`：Trivy JUnit XML 收集從 Image Scan stage 的 `post.always` 移至 pipeline 最外層 `post.always`，統一與其他報告同步收集

### Fixed
- `ciPipeline.groovy`：`post.always` 內 `archiveArtifacts` 置於 `cleanWs()` 之前，確保報告在 workspace 清理前已保存至 Jenkins

---

## [1.6.1] - 2026-03-30

### Fixed
- `cd.sh`：修正 Trivy `--format junit` 不支援問題（Trivy v0.69.3 不存在 `junit` 格式）
  - 改為 `--format template --template "@/usr/local/share/trivy/templates/junit.tpl"`（Trivy 安裝內建 template）
  - 輸出格式與 Jenkins junit step 相容，行為不變

---

## [1.6.0] - 2026-03-30

### Added
- `cd.sh`：新增 `image_scan_if_needed()` 與 `image-scan` stage 參數，整合 Trivy container image 掃描
  - main branch：warn only（`--exit-code 0`），不阻斷 build
  - prod branch：發現 HIGH/CRITICAL 時 fail build（`--exit-code 1`）
  - develop 及其他 branch：自動跳過
  - 輸出格式：JUnit XML（`trivy-results.xml`），供 Jenkins junit step 收集
  - cache 存於 `${WORKSPACE}/.trivy-cache`，隨 `cleanWs` 自動清理
- `ciPipeline.groovy`：新增 `Image Scan` stage（位於 Docker Build 之後、Harbor Push 之前）
  - 所有 profile 加入 `imageScan` flag（`full` / `ci-cd` / `smoke`：true；`ci-only`：false）
  - 依賴推導：`dockerBuild: false → imageScan: false`；`imageScan` 與 `harborPush` 為獨立 flag，互不阻斷
  - stage post block 收集 `trivy-results.xml`（allowEmptyResults: true）
- `ciPipeline.groovy`：`post { always }` 新增 `publishHTML` 發布兩份報告（`cleanWs` 之前執行）
  - JaCoCo Coverage Report：`target/site/jacoco/index.html`（main/prod branch 才產生）
  - OWASP Dependency-Check Report：`target/dependency-check-report.html`（Phase 2 預留，allowMissing: true）

### Changed
- `java/java-test.sh`：`run_coverage()` 實作完成，main/prod branch 執行 `mvnw verify`（包含 unit test + JaCoCo report），取代原先分別呼叫 `run_unit_test()` 再呼叫 `run_coverage()` 的設計，避免 Maven 重複執行
- `java/java-test.sh`：執行邏輯重構為 `case` 分流——main/prod 執行 `run_coverage()`；develop 及其他執行 `run_unit_test()`

---

## [1.5.0] - 2026-03-26

### Added
- `ciPipeline.groovy`：以 Sequential Stages 將 pipeline 劃分為三層群組：`Prepare（準備）` / `Continuous Integration（持續整合）` / `Continuous Delivery（持續交付）`，Stage View 可視化呈現各層職責邊界

### Changed
- `ciPipeline.groovy`：`dockerBuild` 從 `ciStages` 移至 `cdStages`（破壞性變更），語意上 Docker Build 為 CD 起點，負責將 CI artifact 容器化
- `ciPipeline.groovy`：依賴推導調整為 `archive: false → cdStages.dockerBuild = false`（跨群組推導）
- Profile 矩陣同步更新：`ciStages` 僅含 `build / test / archive`；`cdStages` 含 `dockerBuild / harborPush / smokeTest / deploy`

---

## [1.4.1] - 2026-03-26

### Added
- `ciPipeline.groovy`：新增 `profile` 參數，支援四種預定義 pipeline 規模（`full` / `ci-only` / `ci-cd` / `smoke`），統一由 Shared Library 維護，代表組織層策略選擇
- `ciPipeline.groovy`：新增 `ciStages` / `cdStages` 參數，各專案可在 profile 基礎上針對個別 stage 做開關微調
- `ciPipeline.groovy`：強制依賴推導——上游 stage 關閉時自動關閉所有下游 stage（build→test、archive→dockerBuild、dockerBuild→全 cdStages、harborPush→smokeTest）
- `ciPipeline.groovy`：Pipeline 啟動時輸出推導後的 profile / ciStages / cdStages，方便 debug

### Changed
- `ciPipeline.groovy`：CI stage（Build / Test / Archive / Docker Build）`when` 條件改為讀取 `ciStages` flag
- `ciPipeline.groovy`：CD stage（Harbor Push / Smoke Test / Deploy）`when` 條件改為 `CD_ENABLED`（branch）+ `cdStages` flag 雙重把關
- 向下相容：不傳任何 profile / stages 設定時，行為與 v1.3.0 完全一致（預設 `profile: 'full'`）

### Fixed
- `ciPipeline.groovy`：補齊 `build: false → archive` 自動 skip 的依賴推導；build 跳過時 target/ 目錄不存在，archive 繼續執行會 find 失敗
- `ciPipeline.groovy`：以 Sequential Stages 將 stage 分組為 `Continuous Integration（持續整合）` 與 `Continuous Delivery（持續交付）`，Stage View 可視化區分 CI / CD 邊界

---

## [1.3.0] - 2026-03-26

### Added
- `smoke-test.sh`：Smoke Test 入口，Harbor Push 後自動驗證 image 可正常啟動，依語言呼叫對應實作
- `java/java-smoke-test.sh`：Java Smoke Test 實作，啟動 Harbor image 臨時容器，輪詢 Spring Boot Actuator health，status=UP 才算通過，trap EXIT 確保容器自動清理
  - 選用 `smoke-test.env`：專案根目錄可放置此檔注入啟動所需最小設定（例如排除多餘 AutoConfiguration）
- `node/node-smoke-test.sh`：空殼佔位，尚未實作
- `python/python-smoke-test.sh`：空殼佔位，尚未實作
- `ciPipeline.groovy`：新增 `Smoke Test` stage（位於 Harbor Push 之後、Deploy 之前），CD_ENABLED=true 時觸發

---

## [1.2.7] - 2026-03-26

### Fixed
- `java/java-archive.sh`：修復 Docker Build 時 base image tag 找不到的問題
  - 根本原因：Maven `help:evaluate -Dexpression=java.version` 回傳 JVM 系統屬性（如 `21.0.10`）而非 pom.xml 定義的主版本號，導致 `eclipse-temurin:21.0.10-jre-jammy` 不存在
  - 修法：截取主版本號（`${raw_version%%.*}`），確保傳入 Dockerfile 的 `RUNTIME_VERSION` 為 `21` 而非 `21.0.10`

---

## [1.2.6] - 2026-03-26

### Fixed
- `cd.sh`：修復 Docker Build 產生的 image 內 `app.jar` 為目錄而非 JAR 檔的問題
  - 根本原因：`build_args` 未傳入 `--build-arg JAR_FILE`，Dockerfile `COPY ${JAR_FILE} app.jar` 接到空字串，將整個 workspace 複製為目錄
  - 修法：Docker Build 前將 JAR 從 `ARTIFACTS_ROOT` 複製至 `.pipeline/`（build context 內），並補傳 `--build-arg JAR_FILE` 與 `--build-arg RUNTIME_VERSION`；build 完成後清理臨時檔

---

## [1.2.5] - 2026-03-26

### Fixed
- `ciPipeline.groovy`：`CD_ENABLED` 移出 `environment {}` 區塊，改在 Checkout stage 完成後以 script 設定
  - 根本原因：`environment {}` 在 checkout 前評估，`GIT_BRANCH` 尚未設定，導致 regex 永遠不匹配，Harbor Push 被跳過
  - 修法：`checkout scm` 後取 `env.GIT_BRANCH`，去除 `origin/` 前綴後比對 `develop|main|prod`

---

## [1.2.4] - 2026-03-26

### Added
- `cd.sh`：實作 `harbor_push_if_needed()`，完成 Harbor image push 流程（docker login → tag → push → logout）
- `cd.sh`：image 命名規則 `{registry}/{app-name}/{app-name}:{branch}-{version}-{buildNumber}`，registry 預設 `localhost:9290`
- `ciPipeline.groovy`：新增必填參數 `harborCredentials`，各專案 Jenkinsfile 指定對應 Harbor Robot Account Credential ID
- `ciPipeline.groovy`：Harbor Push stage 改以 `withCredentials` 注入 `HARBOR_USER` / `HARBOR_PASS`，避免憑證明文外洩

### Changed
- `ciPipeline.groovy`：`CD_ENABLED` 由寫死 `false` 改為依 branch 自動判斷（`develop|main|prod` 為 `true`，其餘為 `false`）
- `cd.sh`：`harbor_push_if_needed()` 暫時開啟 develop branch（供 CI/CD 串接驗證，驗證完成後移除）
- `cd.sh`：`deploy_if_needed()` 暫時開啟 develop branch 佔位（供 CI/CD 串接驗證，驗證完成後移除）

---

## [1.2.3] - 2026-03-18

### Fixed
- `java/java-build.sh`：加入 `source java-env.sh`，確保 Build stage 執行前自動切換至正確 JDK 版本
- `java/java-test.sh`：加入 `source java-env.sh`，確保 Test stage 與 Build stage 使用相同 JDK
- `ciPipeline.groovy`：Load Scripts 補入遺漏的 `java-env.sh`，修復 java-build.sh source 時找不到檔案的問題

### Changed
- `ci.sh`：移除重複的 `source java-env.sh`（已改由 java-build.sh 自行 source，避免雙重載入）

---

## [1.2.2] - 2026-03-18

### Fixed
- `dockerfiles/Dockerfile-java`：base image 從 `eclipse-temurin:jre-alpine` 改為 `eclipse-temurin:jre-jammy`（Ubuntu 22.04），修復在 Apple Silicon（ARM64/M1）上 `no matching manifest for linux/arm64/v8` 導致 Docker Build 失敗的問題

---

## [1.2.1] - 2026-03-17

### Fixed
- `node/node-archive.sh`：修正 `package.json` 解析邏輯與 zip 排除路徑模式（`-x` pattern 格式錯誤）

---

## [1.2.0] - 2026-03-16

### Added
- `node/node-build.sh`：Node.js Build 實作，以 python3 解析 `engines.node` 透過 nvm 切換版本，執行 `npm ci` / `yarn install --frozen-lockfile`，偵測到 `scripts.build` 才執行（兼容前端框架）
- `node/node-test.sh`：Node.js Test 實作，重新 source nvm 確保版本一致，依 branch 決定測試範圍（develop: unit、main: +coverage/integration TODO、prod: +security TODO）
- `node/node-archive.sh`：Node.js Archive 實作，以 python3 讀取 appName/appVersion/nodeVersion，打包 zip（排除 node_modules/.git/.pipeline/logs），比照 Java 命名規則與 release/backup 目錄結構，寫入 build.env

---

## [1.1.0] - 2026-03-16

### Added
- `java/java-env.sh`：依 pom.xml `<java.version>` 或 build.gradle `sourceCompatibility` 自動切換 `JAVA_HOME`，支援 JDK 8 / 11 / 17 / 21

### Changed
- `ci.sh`：Java CI 流程執行前加入 `source java-env.sh`，確保使用正確 JDK 版本建置

---

## [1.0.0] - 2026-03-12

### Added
- jenkins-pipeline Shared Library 初始版本，統一管理所有專案 CI/CD 流程
- `ciPipeline.groovy`：Pipeline 統一入口，各專案 Jenkinsfile 僅需傳入 `githubCredentials`
- `detect.sh`：自動偵測語言（Java / Node / Python）與 Build Tool，輸出 KEY=VALUE 格式
- `ci.sh`：CI 流程入口（standalone 用途）
- `cd.sh`：CD 流程入口，支援 `docker-build | harbor-push | deploy | all` stage 參數
- `common/error-handler.sh`：共用錯誤處理（trap ERR）
- `common/docker.sh`：Docker Build 共用邏輯，支援三層 Dockerfile 查找優先序
- `common/git-tag.sh`：Git Tag 共用邏輯
- `common/archive-base.sh`：release/backup 搬移共用邏輯
- `java/java-build.sh`：Maven / Gradle 條件判斷與執行
- `java/java-test.sh`：測試執行，依 branch 決定範圍（Unit / Coverage / Integration / Security）
- `java/java-archive.sh`：JAR 版本命名管理，結果寫入 `.pipeline/build.env`
- `dockerfiles/Dockerfile-java`：Java 應用標準 Docker Image 定義
- Pipeline Stages：Checkout → Load Scripts → Detect → Build → Test → Archive → Docker Build
- Load Scripts 機制：透過 `libraryResource()` 將 scripts/dockerfiles 寫入 Agent `.pipeline/` 目錄
- 產出物命名規則：依 branch 自動加入對應後綴（SNAPSHOT / RC / 正式版）
- Git Tag 策略：develop/main/feature branch 由 Jenkins 自動打 tag，prod 由開發者手動標記

### Fixed
- 修正 `archive-base.sh` / `git-tag.sh` 被 source 時 `BASH_SOURCE` 路徑錯誤
- 修正 Agent 無法存取 Shared Library scripts 的問題（改以 `libraryResource()` 寫入 Agent workspace）
- 修正 artifact 命名出現重複 SNAPSHOT 後綴的問題

### Docs
- 新增 README：使用方式、目錄結構、語言偵測邏輯、版本管理說明

[Unreleased]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.8.0...HEAD
[1.8.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.6.2...v1.8.0
[1.6.2]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.6.1...v1.6.2
[1.6.1]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.7...v1.3.0
[1.2.7]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.6...v1.2.7
[1.2.6]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.5...v1.2.6
[1.2.5]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.4...v1.2.5
[1.2.4]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.3...v1.2.4
[1.2.3]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/releases/tag/v1.0.0
