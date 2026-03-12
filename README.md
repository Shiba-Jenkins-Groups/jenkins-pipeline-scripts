# jenkins-pipeline

Jenkins Shared Library，統一管理所有專案的 CI/CD 流程。

各專案 Jenkinsfile 只需宣告 Library 名稱與傳入 Credentials，其餘語言偵測、建置、測試、封存、Docker Build 全部自動處理。

---

## 使用方式

在專案根目錄的 `Jenkinsfile` 加入以下內容即可：

```groovy
@Library('jenkins-pipeline@v1.0.0') _

ciPipeline(
    githubCredentials: 'github-credentials'
)
```

---

## 目錄結構

```
jenkins-pipeline/
├── vars/
│   └── ciPipeline.groovy          # Shared Library 入口
└── resources/
    ├── scripts/
    │   ├── ci.sh                  # CI 入口（含語言自動偵測）
    │   ├── cd.sh                  # CD 入口（開關控制）
    │   ├── common/
    │   │   ├── error-handler.sh   # 共用錯誤處理（trap ERR）
    │   │   ├── docker.sh          # Docker Build 共通
    │   │   ├── git-tag.sh         # Git Tag 共通
    │   │   └── archive-base.sh    # release / backup 搬移共通
    │   ├── java/
    │   │   ├── java-build.sh      # Maven / Gradle 建置
    │   │   ├── java-test.sh       # 單元測試（依 branch 決定範圍）
    │   │   └── java-archive.sh    # JAR 版本管理
    │   ├── node/
    │   │   ├── node-build.sh      # TODO
    │   │   ├── node-test.sh       # TODO
    │   │   └── node-archive.sh    # TODO
    │   └── python/
    │       ├── python-build.sh    # TODO
    │       ├── python-test.sh     # TODO
    │       └── python-archive.sh  # TODO
    └── dockerfiles/
        ├── Dockerfile-java        # Java 預設容器 image
        ├── Dockerfile-node        # TODO
        └── Dockerfile-python      # TODO
```

---

## 語言自動偵測

`ci.sh` 依專案根目錄的特定檔案自動判斷語言與建置工具：

| 偵測條件 | Language | Build Tool |
|----------|----------|-----------|
| `pom.xml` | java | maven |
| `build.gradle` | java | gradle |
| `package.json` | node | npm / yarn |
| `requirements.txt` / `pyproject.toml` | python | pip |

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
1. 專案根目錄 Dockerfile-{lang}   ← 自訂，優先使用
2. 專案根目錄 Dockerfile           ← 舊版相容
3. jenkins-pipeline 預設           ← 無自訂時使用
```

---

## 版本管理

本 Library 以 **git tag** 作為版本號，不使用 branch 區隔：

```bash
# 發布新版本
git tag v1.1.0
git push origin v1.1.0
```

各專案在 Jenkinsfile 指定版本：

```groovy
@Library('jenkins-pipeline@v1.1.0') _
```

---

## 相關專案

| 專案 | 說明 |
|------|------|
| [claude-project](https://github.com/ShibaDev2026/claude-project) | Spring Boot 後端，引用本 Library 的 CI/CD 範例 |
