# jenkins-pipeline-scripts

Jenkins Shared Library，統一管理所有專案的 CI/CD 流程。

各專案 Jenkinsfile 只需宣告 Library 名稱與傳入 Credentials，其餘語言偵測、建置、測試、封存、Docker Build 全部自動處理。

---

## 使用方式

在專案根目錄的 `Jenkinsfile` 加入以下內容即可：

```groovy
@Library('jenkins-pipeline@main') _

ciPipeline(
    githubCredentials: 'github-credentials',
    harborCredentials: 'harbor-robot-<project>'
)
```

> 版本策略：`@main` 單一真相，各專案永遠追蹤最新 main（升級＝commit 到 main，無需改 Jenkinsfile）。

---

## 目錄結構

```
jenkins-pipeline-scripts/
├── vars/
│   └── ciPipeline.groovy          # Shared Library 入口
└── resources/
    ├── scripts/
    │   ├── detect.sh              # 語言 / Build Tool 自動偵測
    │   ├── ci.sh                  # CI 入口
    │   ├── cd.sh                  # CD 入口（依政策旗標執行）
    │   ├── smoke-test.sh          # Smoke Test 入口（分派至各語言）
    │   ├── common/
    │   │   ├── error-handler.sh   # 共用錯誤處理（trap ERR）
    │   │   ├── docker.sh          # Docker Build 共通
    │   │   ├── git-tag.sh         # Git Tag 共通
    │   │   ├── nexus-upload.sh    # Nexus raw-artifacts 上傳 / 下載（產出物單一真相）
    │   │   ├── version.sh         # 跨語言版本號解析（單一版本契約）
    │   │   └── branch-policy.sh   # branch 政策單一真相表（旗標推導）
    │   ├── go/                    # go-env / build / test / archive / smoke-test
    │   ├── java/                  # java-env / build / test / archive / smoke-test
    │   ├── node/                  # build / test / archive / smoke-test
    │   └── python/                # build / test / archive / smoke-test（⏳ 佔位）
    └── dockerfiles/
        ├── Dockerfile-go          # Go 預設容器 image（debian bookworm-slim）
        ├── Dockerfile-java        # Java 預設容器 image
        ├── Dockerfile-node        # Node 預設容器 image
        └── Dockerfile-python      # ⏳ TODO
```

---

## 語言自動偵測

`detect.sh` 依專案根目錄的特定檔案自動判斷語言與建置工具：

| 偵測條件（依優先序）| Language | Build Tool |
|----------|----------|------------|
| `go.mod`（優先，Go 專案可能帶 package.json）| go | go |
| `pom.xml` | java | maven |
| `build.gradle` | java | gradle |
| `package.json` | node | npm / yarn |
| `requirements.txt` / `pyproject.toml` | python | pip |

---

## Pipeline Stages

```
Prepare（Checkout → Load Scripts → Detect）
  → CI（Build → Test → Archive）
  → CD（Docker Build → Image Scan → Harbor Push → Smoke Test → Deploy）
```

| Stage | 說明 |
|-------|------|
| Checkout | git checkout 專案程式碼 |
| Load Scripts | 透過 `libraryResource()` 將 scripts 寫入 Agent `.pipeline/` |
| Detect | 偵測語言、Build Tool、appName；`branch-policy.sh` 推導政策旗標注入 env |
| Build | 依語言執行建置 |
| Test | 依 `TEST_LEVEL` 政策旗標決定測試檔位 |
| Archive | 產出物命名、上傳 Nexus `raw-artifacts`（版本化路徑，單一真相），寫 build.env |
| Docker Build | 建置 Docker image（取檔 `ARTIFACT_LOCAL` → Nexus 下載；政策旗標控制）|
| Image Scan | Trivy 弱點掃描（exit code 依 branch 政策：main=warn、prod=fail）|
| Harbor Push | 推送 image 至 Harbor registry |
| Smoke Test | 以 Harbor image 起容器驗證健康狀態 |
| Deploy | 部署至 k3s（develop→dev、prod→prod namespace；prod 有 input 人工閘）|

---

## Branch 政策（單一真相表）

所有「哪個 branch 做什麼」集中在 **`scripts/common/branch-policy.sh`**；
Detect stage 推導旗標注入 env，`ciPipeline.groovy` 的 `when` 與各腳本只讀旗標。
**改政策＝只改 branch-policy.sh 一處。**

| 旗標 | develop | main | prod | 其他 |
|------|---------|------|------|------|
| DO_DOCKER_BUILD | true | true | true | false |
| DO_SCAN（Trivy）| false | true | true | false |
| SCAN_EXIT_CODE | 0 | 0（warn）| 1（fail）| 0 |
| DO_PUSH（Harbor）| true | true | true | false |
| DO_DEPLOY（k3s）| true | false | true | false |
| DEPLOY_NAMESPACE / NODE_PORT | dev / 30090 | — | prod / 30091 | — |
| DEPLOY_INPUT_GATE（人工閘）| false | false | true | false |
| TEST_LEVEL | unit | coverage | coverage | unit |

> Integration（TODO）附掛於 coverage 檔位；Security scan（gitleaks / OWASP）
> 由 Phase 2（v1.7.x）以獨立政策旗標實作。

---

## 產出物命名規則

| Branch | 範例（無 `v` 前綴）|
|--------|------|
| develop | `claude-project-dev-0.0.1-SNAPSHOT-42.jar` |
| main | `claude-project-main-0.0.1-RC-42.jar` |
| prod | `claude-project-prod-0.0.1.jar` |

| 語言 | 格式 | 範例 |
|------|------|------|
| Java | `.jar` | `claude-project-dev-0.0.1-SNAPSHOT-42.jar` |
| Node | `.zip` | `woof-woof-project-main-1.0.0-RC-14.zip` |
| Go | 無副檔名 binary | `{app}-dev-0.0.1-SNAPSHOT-42` |
| Python | `.zip` | `{app}-dev-0.0.1-SNAPSHOT-42.zip`（⏳ 佔位）|

產出物單一真相在 Nexus raw hosted repo `raw-artifacts`，路徑契約
`{app}/{branch}/{version}-{build}-{sha7}/{filename}`（Disable redeploy＝防覆蓋防競態）；
retention 由 cleanup policy 管理（`lastBlobUpdated > 90d`）。
本地 release/backup 輪替已於 v1.15.0 退役。

---

## Git Tag 策略

| Branch | Tag 來源 | 範例 |
|--------|----------|------|
| develop | Jenkins 自動 | `ci-dev-42` |
| main | Jenkins 自動 | `ci-main-42` |
| prod | 開發者手動 | `v0.0.1` |
| 其他 | Jenkins 自動 | `ci-feature-login-42` |

---

## Dockerfile 優先順序

`docker.sh` 依以下順序尋找 Dockerfile，優先使用專案自訂版本：

```
1. 專案根目錄 Dockerfile-{lang}        ← 自訂，優先使用
2. 專案根目錄 Dockerfile               ← 舊版相容
3. jenkins-pipeline-scripts 預設       ← 無自訂時使用
```

---

## 版本管理

**策略：`@main` 單一真相**（1-developer 環境）。各專案永遠引用 `@main`，升級＝commit 到 main。

```bash
# 1. 更新 CHANGELOG.md（變更歷史的單一真相）
# 2. commit 到 main → 所有專案下次 build 自動取得最新版
# 3. （選用）打 audit tag，僅供歷史查閱，不用於引用
git tag vX.Y.Z -m "vX.Y.Z - 說明" && git push origin main --tags
```

- 各專案 Jenkinsfile 引用 `@Library('jenkins-pipeline@main') _`，**無需**手動更新版本號。
- 變更歷史見 [`CHANGELOG.md`](./CHANGELOG.md)（本 README 不再重列版本表，避免多處漂移）。
- 應用程式（各專案）版本號由 `common/version.sh` 統一解析（`VERSION` 檔 > 語言原生 > CHANGELOG > git）。

---

## 相關專案

| 專案 | 說明 |
|------|------|
| [claude-project](https://github.com/ShibaDev2026/claude-project) | Spring Boot 後端，引用本 Library 的 CI/CD 範例 |
