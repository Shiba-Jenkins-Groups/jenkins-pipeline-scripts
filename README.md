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
    │   ├── cd.sh                  # CD 入口（開關控制）
    │   ├── common/
    │   │   ├── error-handler.sh   # 共用錯誤處理（trap ERR）
    │   │   ├── docker.sh          # Docker Build 共通
    │   │   ├── git-tag.sh         # Git Tag 共通
    │   │   └── archive-base.sh    # release / backup 搬移共通
    │   ├── java/
    │   │   ├── java-env.sh        # JDK 多版本自動切換
    │   │   ├── java-build.sh      # Maven / Gradle 建置
    │   │   ├── java-test.sh       # 測試（依 branch 決定範圍）
    │   │   └── java-archive.sh    # JAR 版本管理
    │   ├── node/
    │   │   ├── node-build.sh      # Node.js 建置（nvm 版本切換）
    │   │   ├── node-test.sh       # Node.js 測試
    │   │   └── node-archive.sh    # ZIP 版本管理
    │   └── python/
    │       ├── python-build.sh    # ⏳ TODO
    │       ├── python-test.sh     # ⏳ TODO
    │       └── python-archive.sh  # ⏳ TODO
    └── dockerfiles/
        ├── Dockerfile-java        # Java 預設容器 image
        ├── Dockerfile-node        # ⏳ TODO
        └── Dockerfile-python      # ⏳ TODO
```

---

## 語言自動偵測

`detect.sh` 依專案根目錄的特定檔案自動判斷語言與建置工具：

| 偵測條件 | Language | Build Tool |
|----------|----------|------------|
| `pom.xml` | java | maven |
| `build.gradle` | java | gradle |
| `package.json` | node | npm / yarn |
| `requirements.txt` / `pyproject.toml` | python | pip |

---

## Pipeline Stages

```
Checkout → Load Scripts → Detect → Build → Test → Archive → Docker Build
```

| Stage | 說明 |
|-------|------|
| Checkout | git checkout 專案程式碼 |
| Load Scripts | 透過 `libraryResource()` 將 scripts 寫入 Agent `.pipeline/` |
| Detect | 偵測語言、Build Tool、appName |
| Build | 依語言執行建置 |
| Test | 依 branch 決定測試範圍 |
| Archive | 產出物命名、搬移至 release / backup |
| Docker Build | 建置 Docker image（Harbor Push / Deploy 由 `CD_ENABLED` 控制）|

---

## CD 開關

`cd.sh` 依 branch 決定執行哪些 CD 步驟（預設 `CD_ENABLED=false`，目前不執行）：

| Branch | Docker Build | Harbor Push | Deploy |
|--------|-------------|-------------|--------|
| develop | ✅ | ❌ | ❌ |
| main | ✅ | ⏳ TODO | ❌ |
| prod | ✅ | ⏳ TODO | ⏳ TODO |
| 其他 | ❌ | ❌ | ❌ |

---

## 測試範圍

`java-test.sh` 依 branch 決定執行哪些測試：

| Branch | Unit Test | Coverage | Integration | Security |
|--------|-----------|----------|-------------|----------|
| develop | ✅ | ❌ | ❌ | ❌ |
| main | ✅ | ⏳ TODO | ⏳ TODO | ❌ |
| prod | ✅ | ⏳ TODO | ⏳ TODO | ⏳ TODO |
| 其他 | ✅ | ❌ | ❌ | ❌ |

---

## 產出物命名規則

| Branch | 範例 |
|--------|------|
| develop | `claude-project-dev-v0.0.1-SNAPSHOT-42.jar` |
| main | `claude-project-main-v0.0.1-RC-42.jar` |
| prod | `claude-project-prod-v0.0.1.jar` |

| 語言 | 格式 | 範例 |
|------|------|------|
| Java | `.jar` | `claude-project-dev-v0.0.1-SNAPSHOT-42.jar` |
| Node | `.zip` | `claude-project-frontend-dev-v0.0.1-SNAPSHOT-42.zip` |
| Python | `.zip` | `claude-project-service-dev-v0.0.1-SNAPSHOT-42.zip` |

產出物存放於 Jenkins Controller 的 `/var/jenkins_home/artifacts/{appName}/release/`，舊版自動移至 `backup/`（最多保留 10 份）。

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
