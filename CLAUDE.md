# CLAUDE.md — jenkins-pipeline-scripts 專案記憶

> 最後更新：2026-07-13（版本策略收斂為 @main + 應用程式版本契約 common/version.sh）

---

## 專案概述

Jenkins Shared Library，統一管理所有專案的 CI/CD 流程。
各專案 Jenkinsfile 只需宣告 Library 版本，語言偵測、建置、測試、封存、Docker Build 全部自動處理。

---

## 版本管理（Library 自身）

**策略：`@main` 單一真相（1-developer 環境）。** 各專案 Jenkinsfile 永遠追蹤最新 main：

```groovy
@Library('jenkins-pipeline@main') _
ciPipeline(
    githubCredentials: 'github-credentials',
    harborCredentials: 'harbor-robot-<project>'
)
```

版本號真相收斂為單一來源，避免多處漂移：

| 面向 | 單一真相位置 |
|------|------------|
| Library 變更歷史 | `CHANGELOG.md`（本檔不再重列版本表）|
| 日常引用版本 | `@main`（各專案 Jenkinsfile）|
| JCaC fallback | `config/system/jenkins.yaml` → `defaultVersion: "main"` |
| git tag `vX.Y.Z` | **僅供歷史審計**，不用於日常引用 |

- 升級＝直接 commit 到 main，所有專案下次 build 自動取得，無需改各專案版本號。
- 應用程式（各專案）的版本號由 `common/version.sh` 統一解析，見下節「應用程式版本契約」。

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
    │   ├── cd.sh                  # CD 入口（CD_ENABLED 開關控制）
    │   ├── common/                # 共用模組（error-handler / docker / git-tag / archive-base）
    │   ├── java/                  # java-env / java-build / java-test / java-archive
    │   ├── node/                  # node-build / node-test / node-archive
    │   └── python/                # ⏳ TODO
    └── dockerfiles/
        ├── Dockerfile-java        # Java 容器 image
        ├── Dockerfile-node        # ⏳ TODO
        └── Dockerfile-python      # ⏳ TODO
```

---

## 版本發布流程（@main 策略）

```bash
# 1. 更新 CHANGELOG.md（版本記錄的單一真相）
# 2. commit 到 main
git commit -am "feat(scope): 說明"
# 3. （選用）打 audit tag，僅供歷史查閱，不影響引用
git tag vX.Y.Z -m "vX.Y.Z - 說明" && git push origin main --tags
```

各專案引用 `@main`，**無需**更新任何 Jenkinsfile 版本號，下次 build 自動取得最新 main。

---

## 應用程式版本契約（各專案 artifact 命名用）

由 `common/version.sh` 的 `resolve_app_version` 統一解析，優先序：

```
1. VERSION 檔（專案根目錄，跨語言統一契約，opt-in 最高優先）
2. 語言原生來源（Java: pom.xml <version>；Node: package.json version；Go: 無）
3. CHANGELOG.md 首個 ## [x.y.z]
4. git describe --tags
5. 0.0.0（保底）
```

- **未放 `VERSION` 檔＝行為與舊版完全一致**（Java 走 pom.xml、Node 走 package.json、Go 走 CHANGELOG/git）。
- 要跨語言統一版本來源，只需在專案根目錄放一行版本號的 `VERSION` 檔。
- ⚠ Java 若採 `VERSION` 檔：pom.xml 需設 `<version>${revision}</version>` 並 `-Drevision=$(cat VERSION)` 建置，否則 artifact 命名會與 jar 內部版本分歧（SRP：命名歸命名、建置歸建置）。

## 引用此 Library 的專案

| 專案 | 語言 | 引用版本 |
|------|------|---------|
| claude-project | Java / Maven | `@main` |
| woof-woof-project | Node.js | `@main` |
| shiba-go-ditch-api-project | Go | `@main` |

---

## 重要注意事項

- prod branch 發版必須開發者手動打 GitHub tag，否則 pipeline exit 1
- `DOCKER_BUILDKIT=0` 為必要設定（Agent 環境限制）
- Dockerfile 優先順序：專案自訂 `Dockerfile-{lang}` > 專案 `Dockerfile` > Library 預設
- Jenkins Controller 引用設定在 JCaC（`jenkins.yaml`），調整版本後需重啟或 reload
