# App 自動發布控制：LOCAL 實作，尚未啟用

適用 `ShibaDev2026/shiba-go-ditch-api-project`。新增入口不由 branch Jenkinsfile 呼叫，必須由中央管理的兩個 job 使用**同一個審核後的 40 位 Library commit** 載入。

## 範例配置（不可直接啟用）

以下是待審核的 job Pipeline body 範本；所有佔位值必須由授權的 Jenkins 設定階段定案。`enabled: false` 不提供任何 merge／deploy 能力，也不應在建立 job 時先跑一次來註冊 trigger。

```groovy
@Library('jenkins-pipeline@REPLACE_WITH_REVIEWED_40_CHAR_SHA') _
autoReleasePipeline(
    enabled: false,
    libraryRevision: 'REPLACE_WITH_SAME_REVIEWED_40_CHAR_SHA',
    jenkinsApiUrl: 'REPLACE_WITH_AGENT_REACHABLE_JENKINS_URL',
    jenkinsReadCredentials: 'REPLACE_WITH_READ_ONLY_API_CREDENTIAL_ID',
    harborApiUrl: 'REPLACE_WITH_AGENT_REACHABLE_HARBOR_API_URL',
    harborCredentials: 'REPLACE_WITH_PRODUCT_HARBOR_CREDENTIAL_ID',
    scmCredentials: 'REPLACE_WITH_GITHUB_READ_CREDENTIAL_ID',
    mergeCredentials: 'REPLACE_WITH_PRODUCT_MERGE_CREDENTIAL_ID',
    approvalKeyCredentials: 'REPLACE_WITH_APPROVAL_SECRET_FILE_ID',
    receiptKeyCredentials: 'REPLACE_WITH_RECEIPT_SECRET_FILE_ID',
    stateDirectory: 'REPLACE_WITH_PERSISTENT_COORDINATOR_MOUNT',
    finalizationStateDirectory: 'REPLACE_WITH_SEPARATE_PERSISTENT_FINALIZATION_MOUNT',
    finalizationWriterCredentials: 'REPLACE_WITH_PRODUCT_TAG_WRITER_ID',
    nexusCredentials: 'REPLACE_WITH_NEXUS_RELEASE_WRITER_ID',
    nexusBaseUrl: 'REPLACE_WITH_AGENT_REACHABLE_NEXUS_BASE_URL',
    builderLabel: 'REPLACE_WITH_TRUSTED_RELEASE_BUILDER_LABEL',
    deploymentNodeLabel: 'REPLACE_WITH_UNIQUE_MAC_OWNER_LABEL',
    deploymentJob: 'shiba-go-ditch-api-project-prod-deploy',
    approvers: ['REPLACE_WITH_REAL_JENKINS_USER_ID'],
    revokedApprovalIds: []
)
```

Job 名稱固定為 `shiba-go-ditch-api-project-auto-release`。JCasC／Job DSL 必須先建立為 disabled，並配置 develop threshold=`UNSTABLE` upstream trigger（包含 SUCCESS；不接受 FAILURE／ABORTED）；在授權啟用前不得呼叫正式協調流程。程序內的 `properties` 會維持這個 trigger，而不是用 cron／外部 7×24 代理監看。

App `Jenkinsfile` 的 `controlledReleaseCandidate` 本機仍為 `false`。須經授權的配置／CI/CD 同步啟用 candidate 契約，協調 job 才能接受該 build；不得只啟用 coordinator，亦不得把原失敗 build 的結果手動改成 SUCCESS。

```groovy
@Library('jenkins-pipeline@REPLACE_WITH_SAME_REVIEWED_40_CHAR_SHA') _
prodDeploymentPipeline(
    enabled: false,
    nodeLabel: 'REPLACE_WITH_UNIQUE_MAC_OWNER_LABEL',
    runtimeRoot: 'REPLACE_WITH_CANONICAL_EXISTING_APP_RUNTIME_ROOT',
    stateDirectory: 'REPLACE_WITH_PERSISTENT_MAC_DEPLOY_STATE_DIRECTORY',
    dockerEngineId: 'REPLACE_WITH_APPROVED_DOCKER_ENGINE_ID',
    receiptKeyCredentials: 'REPLACE_WITH_RECEIPT_SECRET_FILE_ID',
    scmCredentials: 'REPLACE_WITH_GITHUB_READ_CREDENTIAL_ID'
)
```

Job 名稱固定為 `shiba-go-ditch-api-project-prod-deploy`，預先宣告 Text 參數 `SIGNED_RELEASE_REQUEST`，無 SCM trigger；只接受 coordinator upstream cause 及有效簽章。先建 disabled job，不安裝 launchd、keep-awake 或高可用機制。

## 啟用前必須逐項驗證

1. Jenkins 設定、Shared Library 及 App 的來源都經審查與既有 CI/CD。不得連同既有 dirty lean-lane／backup 等修改混送。本機程式不是線上已生效設定。
2. prod branch 的**有效子 job** 必須具有 `jenkins.branch.NoTriggerBranchProperty`。目前 branch-api 版本使用 strategy=`NONE` 搭配不匹配任何分支的 regex（`^$`）；handler 會在 strategy 判定前因 regex 不匹配而同時抑制 indexing 與 event cause。舊版若序列化為 strategy=`ALL` 亦可接受。此設定須從 multibranch 的 named-branch property strategy 配置，不能只編輯下次 indexing 會覆寫的子 job；重建／indexing 後仍須 REST 驗證。develop 保留既有 branch event trigger。
3. 協調者與 runner 的 job Configure／Build／Replay、Library 修改權、agent 排程權、憑證使用權必須受控。禁止一般 branch job 取得簽章 key 或排入 deployment node。節點 label 本身不是安全隔離；目前 builder 的共享 Docker socket 仍是信任邊界缺口，必須先驗證隔離／存取控制，不能宣稱新 label 已解决。
4. 兩個 HMAC Secret File 各使用至少 32 bytes 的獨立隨機 key；只在簽發／驗證 stage 綁定，不放 Git、參數、環境設定檔或報告。簽章 key 與對應 state 目錄須受限制且持久化。金鑰遺失、輪替及核准撤銷需人工核對在途收據。
5. `approvers` 必須是真實 user ID。Input 15 分鐘逾時；核准單最長 15 分鐘，僅綁當次 gate、報告與 findings。PROD handoff 的有效期不延長 PROD CVE 核准期限。更新撤銷清單不會追溯改寫已簽發收據；撤銷在途 handoff 必須同步停止待執行部署。
6. promotion、finalization、macOS deployment 的 state 使用三個分離持久目錄。動態工作區或跨 macOS／Linux 共用 lock file 不合格。兩 job 序列化，source SHA 已 claim 就不自動重送；失聯後保留 `PREPARING`／`PUSHING`／`FINALIZING`／`CLAIMED` 狀態，人工查證後才決定恢復。
7. macOS runner 是唯一既有 PROD DB owner host；使用 canonical runtime root、固定 engine ID、同一使用者與必要非互動 Keychain 存取。不得把 runtime root 換成乾淨 checkout。candidate image 必須已在該 engine 中且含相符 RepoDigest 與 OCI revision／version／branch labels；缺失不自行建置／拉取。
8. 所有會異動相同 runtime 的入口須納入互斥協議。新的 `deploy.sh` 會對 dev／prod lifecycle mutation 使用各自 host lock；舊版腳本、直接 Compose、refresh/backup/maintenance 其他入口尚未全面整合，正式啟用前必須完成入口盤點與排他保證。不得宣稱 lock 能阻擋未遵守協議的操作者。
9. 真實 SQLite migration、snapshot 完整性、Recognition active identity、外部請求路径與必要功能 smoke 尚需隔離 DEV owner-runtime 演練。LOCAL tests 只有替身，沒有打開 PROD DB。現在 runner 的最終檢查為 image ID／digest／labels／環境／DB mount／Docker health／loopback heartbeat；產品級功能 smoke 仍須補齊。
10. 先建立不具正式 merge credential／PROD node 權限的隔離驗收 harness，才可做 Stage 3 演練。正式入口 hard-code 真實產品，因此不可把範本直接改成 `enabled:true` 來「試跑 DEV」。測試遠端、隔離 runtime 與允許的 API／credential 必須另列範圍。

## 已實作行為

- Candidate mode 只收集完整 CI evidence，關閉該子 build 的 Nexus artifact／正式 tag finalization；Harbor image 是隔離候選產物，不代表 runtime 上線。未啟用的產品保留原邏輯。
- `Test`／`Fast Contract Test`／`Dependency Scan`／`Image Scan` 可產出 `WAIVER_REQUIRED`，原 CI 保持 UNSTABLE。Go Test 必須有實際 JUnit assertions；go vet／編譯失敗且無 assertions、缺報告、scanner 非零執行错误、timeout／abort 不可豁免。Fast Contract Test 以固定命令、正常 exit=1 與完整 log 為人工審查單位，不推測錯誤是否無害。
- Secret Scan、Build、package、Docker Build、Harbor Push、Harbor API 故障、smoke、k3d／cleanup、post、簽章、finalization 及 runtime 驗證都不可豁免。Harbor CVE 由後續 immutable scan 的精確 findings 核准，不豁免 Harbor API error。
- 協調 job 等待全部 post 完成，交叉比對原生 stage record、所有檢查與產物 hash，再獨立重掃 Trivy 全嚴重度、govulncheck JSON stream、Harbor digest 報告。這是**證據重驗 lane，不是自動重跑失敗 build**；缺少產物／未執行必要 stage 時直接停止。
- Test-stage 與 CVE findings 共同綁定精確簽章核准（gate／job／build／commit／digest／報告內容／期限）。掃描報告、測試內容或任何 finding 改變，原核准即失效；develop 核准不涵蓋 prod。
- Jenkins `build(wait:true)` 綁定唯一 PROD run，核對 merge SHA 再重驗。PROD gate 通過後才 finalization：核對 binary SHA256 與 image label，再執行原 finalizer，回讀 annotated tag 的 commit 及 Nexus binary SHA256；失敗保存狀態，不自動 retry／覆寫。
- schema v2 deployment handoff 必須內嵌匹配的成功 finalization 簽章；runner 拒絕舊格式、未 finalization、錯誤 digest 或證據 hash。不因原 CI 維持 UNSTABLE 而宣稱其曾 SUCCESS；正式發布成功指受控 release/deployment job 完成。
- runner 先驗 request、唯一 PROD owner／mount、host open-file inventory（不開 SQLite）、Docker engine／image，再持有 runtime lock 呼叫固定 commit 的 `scripts/deploy.sh`。
- controlled 模式不修復共用 MinIO／Notification／模型服務，不盲重試 migration verify，不自動 rollback／restore DB；失敗保存收據、journal／backup／migration 路徑。終態成功前核對實際 runtime。

## 未完成與保守限制

- 上述 stage 重驗与 finalization 排序已完成 LOCAL 程式／替身測試，尚未在線上生效。舊（非 candidate）CI 的 FAILURE 不可直接沿用：沒有完整 candidate 契約便停止，必要時另行授權新 build。
- 新流程對原 CI `UNSTABLE` 與最終協調／部署 `SUCCESS` 的区分，必須在啟用前同步核准 CI/CD 準則；不能在現行規則仍要求原 build SUCCESS 時逕行部署。
- Harbor API 不提供可核對的漏洞 DB hash；目前明列 `not-exposed-by-harbor` 加 report time，不能等同已驗證 DB 新鮮度。版本／DB 時間的 parser 還需以線上 scanner 版本做 integration 驗證。
- 未建立 effective JCasC／Job DSL 設定、runner、正式 approver、credential／ACL；未驗 CPS suspend/resume、controller restart、queue response loss、真實 scan 或 owner-runtime。未啟用任何自動 merge／deploy。
- GitHub 私有 repo 方案缺 branch protection；使用者已接受 Jenkins 路徑內保證。Git writers 仍能直接推 prod，但這不是自動部署收據。
- 本次排除 7×24、HA、防休眠、開機復原、zero-downtime 與自動資料還原。

## LOCAL 驗證

```sh
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-gate.test.py
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-control.test.py
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-deploy.test.py
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-candidate.test.py
# 使用本機 Groovy 2.4 的 GroovyMain 執行：
# resources/scripts/common/release-pipelines.test.groovy <library-root>
# resources/scripts/common/release-candidate-ci.test.groovy <library-root>
```

App 另跑 `tests/lifecycle-lock.py`、`tests/controlled-deploy.py`、`tests/app-lifecycle.sh`。Groovy mocks 僅測語法與控制流，不是 Jenkins CPS integration。正式操作仍只透過 Jenkins REST，由 Pipeline 持有 runtime 部署與最後驗證責任。

參考：[Pipeline Build Step](https://www.jenkins.io/doc/pipeline/steps/pipeline-build-step/)、[Pipeline Input](https://www.jenkins.io/doc/pipeline/steps/pipeline-input-step/)、[NoTriggerBranchProperty](https://github.com/jenkinsci/branch-api-plugin/blob/master/src/main/java/jenkins/branch/NoTriggerBranchProperty.java)、[govulncheck JSON 語意](https://github.com/golang/vuln/blob/master/cmd/govulncheck/doc.go)。
