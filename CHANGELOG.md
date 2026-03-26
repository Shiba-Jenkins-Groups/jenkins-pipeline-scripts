# Changelog

本檔案依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.0.0/) 格式，
版號遵循 [Semantic Versioning（語意化版本）](https://semver.org/lang/zh-TW/)。

## [Unreleased]

---

## [1.4.0] - 2026-03-26

### Added
- `ciPipeline.groovy`：新增 `profile` 參數，支援四種預定義 pipeline 規模（`full` / `ci-only` / `ci-cd` / `smoke`），統一由 Shared Library 維護，代表組織層策略選擇
- `ciPipeline.groovy`：新增 `ciStages` / `cdStages` 參數，各專案可在 profile 基礎上針對個別 stage 做開關微調
- `ciPipeline.groovy`：強制依賴推導——上游 stage 關閉時自動關閉所有下游 stage（build→test、archive→dockerBuild、dockerBuild→全 cdStages、harborPush→smokeTest）
- `ciPipeline.groovy`：Pipeline 啟動時輸出推導後的 profile / ciStages / cdStages，方便 debug

### Changed
- `ciPipeline.groovy`：CI stage（Build / Test / Archive / Docker Build）`when` 條件改為讀取 `ciStages` flag
- `ciPipeline.groovy`：CD stage（Harbor Push / Smoke Test / Deploy）`when` 條件改為 `CD_ENABLED`（branch）+ `cdStages` flag 雙重把關
- 向下相容：不傳任何 profile / stages 設定時，行為與 v1.3.0 完全一致（預設 `profile: 'full'`）

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

[Unreleased]: https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts/compare/v1.4.0...HEAD
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
