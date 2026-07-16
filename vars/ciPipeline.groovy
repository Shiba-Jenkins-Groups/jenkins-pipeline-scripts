def call(Map config = [:]) {
    def githubCredentials = config.githubCredentials ?: error('githubCredentials is required')
    // harborCredentials：Harbor Robot Account Credential ID（Jenkins Credentials 中定義）
    def harborCredentials = config.harborCredentials ?: error('harborCredentials is required')
    // nexusCredentials：Nexus 部署帳號 Credential ID（改善計畫 #4a artifact 上傳）
    // 與 harbor 的 per-project robot 不同：全專案共用單一部署帳號，故給預設值免改各專案 Jenkinsfile
    def nexusCredentials = config.nexusCredentials ?: 'nexus-ci-deploy'

    // ── 1. Profile 預設矩陣 ──────────────────────────────────────────────────
    // 組織策略層：預定義 pipeline 規模，統一由 Shared Library 維護
    // ciStages：build / test / archive
    // cdStages：dockerBuild / imageScan / harborPush / smokeTest / deploy
    def profiles = [
        // full：跑完所有 stage，適用 main / prod 正式交付
        'full'   : [ci: [build: true,  test: true, archive: true],
                    cd: [dockerBuild: true,  imageScan: true,  harborPush: true,  smokeTest: true,  deploy: true]],
        // ci-only：僅 CI 階段，不含任何 CD stage，適用 PR 快速驗證、feature branch
        'ci-only': [ci: [build: true,  test: true, archive: true],
                    cd: [dockerBuild: false, imageScan: false, harborPush: false, smokeTest: false, deploy: false]],
        // ci-cd：CI + Docker Build + Image Scan + Harbor Push，需要打包但不部署
        'ci-cd'  : [ci: [build: true,  test: true, archive: true],
                    cd: [dockerBuild: true,  imageScan: true,  harborPush: true,  smokeTest: false, deploy: false]],
        // smoke：完整 CI/CD + Smoke Test，不 deploy，適用 staging 環境驗證
        'smoke'  : [ci: [build: true,  test: true, archive: true],
                    cd: [dockerBuild: true,  imageScan: true,  harborPush: true,  smokeTest: true,  deploy: false]],
    ]

    // ── 2. 套用 profile（預設 full）──────────────────────────────────────────
    def profileName = config.profile ?: 'full'
    def base        = profiles[profileName] ?: profiles['full']

    // 複製 profile 預設值（避免直接修改 profiles map）
    Map ciStages = [:] + base.ci
    Map cdStages = [:] + base.cd

    // ── 3. 專案級覆蓋（ciStages / cdStages 參數）──────────────────────────
    // 各專案可在 profile 基礎上針對個別 stage 做開關微調
    if (config.ciStages instanceof Map) ciStages.putAll(config.ciStages)
    if (config.cdStages instanceof Map) cdStages.putAll(config.cdStages)

    // ── 4. 強制依賴推導（自動，不需手動設定）──────────────────────────────
    // 上游 stage 關閉時，自動關閉所有依賴的下游 stage
    if (!ciStages.build)         ciStages.test           = false
    if (!ciStages.build)         ciStages.archive        = false
    if (!ciStages.archive)       cdStages.dockerBuild    = false
    if (!cdStages.dockerBuild) {
        cdStages.imageScan  = false   // image 不存在時無法掃描
        cdStages.harborPush = false
        cdStages.smokeTest  = false
        cdStages.deploy     = false
    }
    if (!cdStages.harborPush)    cdStages.smokeTest      = false
    // 注意：imageScan 與 harborPush 為獨立 flag，可各自關閉（不互相阻斷）

    // 初始化 log：Pipeline 啟動時輸出推導後的 stage 設定，方便 debug
    echo "[ciPipeline] profile  : ${profileName}"
    echo "[ciPipeline] ciStages : ${ciStages}"
    echo "[ciPipeline] cdStages : ${cdStages}"

    pipeline {
        agent {
            label 'docker-agent'
        }

        options {
            timestamps()
            disableConcurrentBuilds()
        }

        environment {
            GITHUB_CREDENTIALS = credentials("${githubCredentials}")
            // usernamePassword 型 credential 自動展開 NEXUS_CRED_USR / NEXUS_CRED_PSW，
            // 供 nexus-upload.sh（Archive 上傳／cd.sh 下載）使用
            NEXUS_CRED = credentials("${nexusCredentials}")
        }

        stages {

            // ── Prepare（準備）群組 ──────────────────────────────────────────
            // Pipeline 基礎建設：取得程式碼、載入腳本、偵測語言
            // 永遠執行，不暴露 flag
            stage('Prepare（準備）') {
                stages {

                    stage('Checkout') {
                        steps {
                            checkout scm
                            // branch 政策旗標統一由 Detect stage 的 branch-policy.sh 推導
                            echo "[checkout] GIT_BRANCH: ${env.GIT_BRANCH}"
                        }
                    }

                    stage('Load Scripts') {
                        steps {
                            script {
                                // Shared Library scripts 在 Controller，Agent 無法直接存取
                                // 用 libraryResource() 讀取後寫入 Agent workspace 的 .pipeline/
                                //
                                // 慣例載入（OCP）：新增語言＝LANGUAGES 加一個字串＋依慣例放好檔案
                                //   scripts/{lang}/{lang}-{build,test,archive,smoke-test}.sh
                                //   dockerfiles/Dockerfile-{lang}
                                // 慣例之外的語言特例檔以 LANG_EXTRAS 收斂
                                // 缺檔＝結構錯誤：libraryResource() 直接 fail，不靜默跳過
                                def LANGUAGES   = ['go', 'java', 'node', 'python']
                                def LANG_STEPS  = ['build', 'test', 'archive', 'smoke-test']
                                def LANG_EXTRAS = [go: ['go-env.sh'], java: ['java-env.sh']]

                                def resources = [
                                    'scripts/detect.sh',
                                    'scripts/ci.sh',
                                    'scripts/cd.sh',
                                    'scripts/smoke-test.sh',
                                    'scripts/common/error-handler.sh',
                                    'scripts/common/docker.sh',
                                    'scripts/common/git-tag.sh',
                                    'scripts/common/version.sh',
                                    'scripts/common/branch-policy.sh',
                                    'scripts/common/nexus-upload.sh',
                                    'scripts/common/secret-scan.sh',
                                    'scripts/common/dependency-check.sh',
                                ]
                                for (lang in LANGUAGES) {
                                    for (step in LANG_STEPS) {
                                        resources << "scripts/${lang}/${lang}-${step}.sh".toString()
                                    }
                                    for (extra in (LANG_EXTRAS[lang] ?: [])) {
                                        resources << "scripts/${lang}/${extra}".toString()
                                    }
                                    resources << "dockerfiles/Dockerfile-${lang}".toString()
                                }

                                for (path in resources) {
                                    writeFile file: ".pipeline/${path}", text: libraryResource(path)
                                }

                                sh 'find .pipeline/scripts -name "*.sh" -exec chmod +x {} +'
                            }
                        }
                    }

                    stage('Detect') {
                        steps {
                            script {
                                // detect.sh：語言偵測；branch-policy.sh：branch 政策旗標（單一真相表）
                                // 兩者皆以 KEY=VALUE 輸出，統一解析後注入 env，供 when 條件與下游腳本讀取
                                def output = sh(
                                    script: 'bash .pipeline/scripts/detect.sh && bash .pipeline/scripts/common/branch-policy.sh',
                                    returnStdout: true
                                ).trim()
                                output.split('\n').each { line ->
                                    def parts = line.split('=', 2)
                                    if (parts.size() == 2) {
                                        env[parts[0].trim()] = parts[1].trim()
                                    }
                                }

                                // NodePort 不再由 branch-policy.sh 提供（避免所有專案撞用同一固定值）
                                // 改由各專案 Jenkinsfile 的 devNodePort/prodNodePort 參數依 DEPLOY_NAMESPACE 決定
                                if (env.DO_DEPLOY == 'true') {
                                    if (env.DEPLOY_NAMESPACE == 'dev') {
                                        env.NODE_PORT = (config.devNodePort ?: error('ciPipeline: devNodePort is required when DEPLOY_NAMESPACE=dev')).toString()
                                    } else if (env.DEPLOY_NAMESPACE == 'prod') {
                                        env.NODE_PORT = (config.prodNodePort ?: error('ciPipeline: prodNodePort is required when DEPLOY_NAMESPACE=prod')).toString()
                                    }
                                }

                                // build tags 依部署環境注入（如 develop→devseed：dev 專屬 admin token）。
                                // 專案未宣告 devBuildTags 即不帶 tag；prod／main 分支一律不帶（編譯期隔離）。
                                if (env.DO_DEPLOY == 'true' && env.DEPLOY_NAMESPACE == 'dev' && config.devBuildTags) {
                                    env.GO_BUILD_TAGS = config.devBuildTags.toString()
                                }

                                echo "[detect] Language: ${env.LANGUAGE}, BuildTool: ${env.BUILD_TOOL}"
                                echo "[detect] Policy: DO_SECRET_SCAN=${env.DO_SECRET_SCAN}(exit=${env.SECRET_SCAN_EXIT_CODE}), DO_DEP_SCAN=${env.DO_DEP_SCAN}(cvss=${env.DEP_SCAN_CVSS}), DO_DOCKER_BUILD=${env.DO_DOCKER_BUILD}, DO_SCAN=${env.DO_SCAN}(exit=${env.SCAN_EXIT_CODE}), " +
                                     "DO_PUSH=${env.DO_PUSH}, DO_DEPLOY=${env.DO_DEPLOY}(ns=${env.DEPLOY_NAMESPACE}, port=${env.NODE_PORT}), TEST_LEVEL=${env.TEST_LEVEL}, GO_BUILD_TAGS=${env.GO_BUILD_TAGS ?: '(none)'}"
                            }
                        }
                    }

                }
            }

            // ── Continuous Integration（持續整合）群組 ──────────────────────
            // 程式碼整合與驗證：編譯 → 測試 → 打包成 artifact
            stage('Continuous Integration（持續整合）') {
                stages {

                    stage('Secret Scan') {
                        // gitleaks 秘密掃描（Security Phase 2）；DO_SECRET_SCAN 由 branch-policy 決定（全 branch）
                        // 置於 Build 前 fail fast：發現秘密即擋，不浪費後續 build/test
                        when { expression { env.DO_SECRET_SCAN == 'true' } }
                        steps {
                            sh "bash .pipeline/scripts/common/secret-scan.sh"
                        }
                    }

                    stage('Build') {
                        // ciStages.build = false 時跳過
                        when { expression { ciStages.build } }
                        steps {
                            sh "bash .pipeline/scripts/${env.LANGUAGE}/${env.LANGUAGE}-build.sh"
                        }
                    }

                    stage('Test') {
                        // ciStages.test = false 時跳過（build: false 時依賴推導自動關閉）
                        when { expression { ciStages.test } }
                        steps {
                            sh "bash .pipeline/scripts/${env.LANGUAGE}/${env.LANGUAGE}-test.sh"
                        }
                        post {
                            always {
                                // 語言中立報告契約（#2）：各語言 test 腳本統一產 JUnit 至 reports/junit/
                                junit allowEmptyResults: true,
                                      testResults: 'reports/junit/*.xml'
                            }
                        }
                    }

                    stage('Dependency Scan') {
                        // OWASP Dependency-Check（第三方依賴 CVE，Security Phase 2）
                        // DO_DEP_SCAN 由 branch-policy 決定（main warn / prod fail）；語言 guard 在腳本內（僅 java/maven）
                        // 置 Archive 前：prod 依賴含高危 CVE 時擋在打 tag／發佈 artifact 之前
                        when { expression { env.DO_DEP_SCAN == 'true' } }
                        steps {
                            // NVD API key 由 credential 綁定注入 env（console 自動 mask）
                            withCredentials([string(credentialsId: 'nvd-api-key', variable: 'NVD_API_KEY')]) {
                                sh "bash .pipeline/scripts/common/dependency-check.sh"
                            }
                        }
                    }

                    stage('Archive') {
                        // ciStages.archive = false 時跳過（build: false 時依賴推導自動關閉）
                        when { expression { ciStages.archive } }
                        steps {
                            sh "bash .pipeline/scripts/${env.LANGUAGE}/${env.LANGUAGE}-archive.sh"
                        }
                    }

                }
            }

            // ── Continuous Delivery（持續交付）群組 ────────────────────────
            // 交付流程：容器化 → 推送至 registry → 健康驗證 → 部署
            // 各 stage 受「profile flag（cdStages）＋ branch 政策旗標（branch-policy.sh）」雙重把關
            // 政策旗標由 Detect stage 推導注入 env；此處只讀，不自帶 branch 判斷
            stage('Continuous Delivery（持續交付）') {
                stages {

                    stage('Docker Build') {
                        // cdStages.dockerBuild = false 時跳過（archive: false 時依賴推導自動關閉）
                        when {
                            allOf {
                                expression { cdStages.dockerBuild }
                                expression { env.DO_DOCKER_BUILD == 'true' }
                            }
                        }
                        steps {
                            sh 'bash .pipeline/scripts/cd.sh docker-build'
                        }
                    }

                    stage('Image Scan') {
                        // Trivy 掃描 Docker Build 產生的本地 image
                        // exit-code 由政策表 SCAN_EXIT_CODE 決定：main=0（warn only）；prod=1（HIGH/CRITICAL fail）
                        // cdStages.imageScan = false 時跳過（dockerBuild: false 時依賴推導自動關閉）
                        when {
                            allOf {
                                expression { cdStages.imageScan }
                                expression { env.DO_SCAN == 'true' }
                            }
                        }
                        steps {
                            sh 'bash .pipeline/scripts/cd.sh image-scan'
                        }
                        // Trivy JUnit XML 已移至 pipeline post.always 統一收集
                    }

                    stage('Harbor Push') {
                        when {
                            allOf {
                                // branch 政策（branch-policy.sh 推導）
                                expression { env.DO_PUSH == 'true' }
                                // profile / 專案微調 flag
                                expression { cdStages.harborPush }
                            }
                        }
                        steps {
                            // Harbor Robot Account 憑證透過 withCredentials 注入環境變數
                            // HARBOR_USER / HARBOR_PASS 由 cd.sh harbor_push_if_needed() 使用
                            withCredentials([usernamePassword(
                                credentialsId: harborCredentials,
                                usernameVariable: 'HARBOR_USER',
                                passwordVariable: 'HARBOR_PASS'
                            )]) {
                                sh 'bash .pipeline/scripts/cd.sh harbor-push'
                            }
                        }
                    }

                    stage('Smoke Test') {
                        when {
                            allOf {
                                // Smoke Test 驗證的是已 push 的 image，故跟隨 DO_PUSH 政策
                                expression { env.DO_PUSH == 'true' }
                                // harborPush: false 時依賴推導自動關閉
                                expression { cdStages.smokeTest }
                            }
                        }
                        steps {
                            // Harbor Push 後自動驗證 image 可正常啟動
                            // Java：起臨時容器輪詢 Actuator health，UP 才算通過
                            // Node / Python：空殼，尚未實作
                            sh 'bash .pipeline/scripts/smoke-test.sh'
                        }
                    }

                    stage('Deploy') {
                        when {
                            allOf {
                                expression { env.DO_DEPLOY == 'true' }
                                expression { cdStages.deploy }
                            }
                        }
                        steps {
                            script {
                                // 人工確認閘由政策表 DEPLOY_INPUT_GATE 決定（prod=true，防止誤觸）
                                if (env.DEPLOY_INPUT_GATE == 'true') {
                                    input message: "Deploy ${env.JOB_NAME} #${env.BUILD_NUMBER} to ${env.DEPLOY_NAMESPACE} namespace?",
                                          ok: "Deploy to ${env.DEPLOY_NAMESPACE}"
                                }
                            }
                            // KUBECONFIG 由 Jenkins Secret File credential（ID: k3s-kubeconfig）注入
                            // Jenkins agent 使用 k3s-kubeconfig-agent（server: jenkins-network 內部 IP:6443）
                            withCredentials([file(credentialsId: 'k3s-kubeconfig', variable: 'KUBECONFIG')]) {
                                sh 'bash .pipeline/scripts/cd.sh deploy'
                            }
                        }
                    }

                }
            }

        }

        post {
            always {
                // ── Build 摘要（Shiba 與 Claude Code 均可快速掃描）─────────────
                echo "╔══════════════════════════════════════════════╗"
                echo "║  BUILD SUMMARY                               ║"
                echo "╠══════════════════════════════════════════════╣"
                echo "║  Job     : ${env.JOB_NAME}"
                echo "║  Build   : #${env.BUILD_NUMBER}"
                echo "║  Result  : ${currentBuild.currentResult}"
                echo "║  Duration: ${currentBuild.durationString}"
                echo "║  URL     : ${env.BUILD_URL}"
                echo "╚══════════════════════════════════════════════╝"

                // ── 報告保存（順序：archive → publishHTML → junit → cleanWs）──
                // archiveArtifacts 必須在 cleanWs 之前，否則檔案已被清除
                // allowEmptyArchive: true — 報告不存在時（無 secret scan）不 fail
                archiveArtifacts artifacts: 'gitleaks-report.json',
                                 allowEmptyArchive: true

                // 語言中立 Coverage HTML 契約（#2）：各語言 test 腳本統一產至 reports/coverage/index.html
                // （Java=JaCoCo、Go=go tool cover）；coverage 檔位才產生，allowMissing 避免其他 branch fail
                publishHTML(target: [
                    allowMissing          : true,
                    alwaysLinkToLastBuild : false,
                    keepAll               : true,
                    reportDir             : 'reports/coverage',
                    reportFiles           : 'index.html',
                    reportName            : 'Coverage Report'
                ])
                // OWASP Dependency-Check HTML（Phase 2 預留，allowMissing: true 目前不會 fail）
                publishHTML(target: [
                    allowMissing          : true,
                    alwaysLinkToLastBuild : false,
                    keepAll               : true,
                    reportDir             : 'target',
                    reportFiles           : 'dependency-check-report.html',
                    reportName            : 'OWASP Dependency-Check Report'
                ])
                // Trivy JUnit XML（main / prod branch 才產生，allowEmptyResults 避免其他 branch fail）
                junit allowEmptyResults: true,
                      testResults: 'trivy-results.xml'

                // ── Docker dangling image 清理（無 tag 殘留層，每次 build 後自動清除）──
                sh 'docker image prune -f'

                cleanWs()
            }
            failure {
                // ── 失敗診斷提示（快速指引查錯方向）────────────────────────────
                echo "╔══════════════════════════════════════════════╗"
                echo "║  FAILURE DIAGNOSIS                           ║"
                echo "╠══════════════════════════════════════════════╣"
                echo "║  1. Stage View: 確認哪個 Stage 標紅          ║"
                echo "║  2. Console Log: 搜尋 [ERROR] 或 PIPELINE    ║"
                echo "║     ERROR 框線區塊                           ║"
                echo "║  3. 報告: Build Artifacts 下載各項報告       ║"
                echo "║  Console: ${env.BUILD_URL}console            ║"
                echo "╚══════════════════════════════════════════════╝"
            }
            unstable {
                echo "[BUILD UNSTABLE] 部分 Stage 回報警告，請檢查 Stage View 與 Test Results。"
            }
            success {
                echo "Pipeline SUCCESS — ${env.JOB_NAME} #${env.BUILD_NUMBER}"
            }
        }
    }
}
