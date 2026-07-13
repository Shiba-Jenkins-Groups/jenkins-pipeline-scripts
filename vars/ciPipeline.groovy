def call(Map config = [:]) {
    def githubCredentials = config.githubCredentials ?: error('githubCredentials is required')
    // harborCredentials：Harbor Robot Account Credential ID（Jenkins Credentials 中定義）
    def harborCredentials = config.harborCredentials ?: error('harborCredentials is required')

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
                            script {
                                // checkout scm 完成後 GIT_BRANCH 才可用
                                // 去除 origin/ 前綴後比對 develop / main / prod
                                def branch = env.GIT_BRANCH?.replaceAll(/^origin\//, '') ?: ''
                                // CD_ENABLED：develop / main / prod 啟用；其他 branch 跳過 CD stage
                                env.CD_ENABLED = (branch ==~ /(develop|main|prod)/) ? 'true' : 'false'
                                echo "[checkout] GIT_BRANCH: ${env.GIT_BRANCH}, CD_ENABLED: ${env.CD_ENABLED}"
                            }
                        }
                    }

                    stage('Load Scripts') {
                        steps {
                            script {
                                // Shared Library scripts 在 Controller，Agent 無法直接存取
                                // 用 libraryResource() 讀取後寫入 Agent workspace 的 .pipeline/
                                def scripts = [
                                    'scripts/detect.sh',
                                    'scripts/ci.sh',
                                    'scripts/cd.sh',
                                    'scripts/common/error-handler.sh',
                                    'scripts/common/docker.sh',
                                    'scripts/common/git-tag.sh',
                                    'scripts/common/archive-base.sh',
                                    'scripts/common/version.sh',
                                    'scripts/go/go-env.sh',
                                    'scripts/go/go-build.sh',
                                    'scripts/go/go-test.sh',
                                    'scripts/go/go-archive.sh',
                                    'scripts/go/go-smoke-test.sh',
                                    'scripts/java/java-env.sh',
                                    'scripts/java/java-build.sh',
                                    'scripts/java/java-test.sh',
                                    'scripts/java/java-archive.sh',
                                    'scripts/node/node-build.sh',
                                    'scripts/node/node-test.sh',
                                    'scripts/node/node-archive.sh',
                                    'scripts/node/node-smoke-test.sh',
                                    'scripts/python/python-build.sh',
                                    'scripts/python/python-test.sh',
                                    'scripts/python/python-archive.sh',
                                    'scripts/python/python-smoke-test.sh',
                                    'scripts/smoke-test.sh',
                                    'scripts/java/java-smoke-test.sh',
                                ]
                                scripts.each { path ->
                                    def content = libraryResource(path)
                                    writeFile file: ".pipeline/${path}", text: content
                                }

                                def dockerfiles = [
                                    'dockerfiles/Dockerfile-go',
                                    'dockerfiles/Dockerfile-java',
                                    'dockerfiles/Dockerfile-node',
                                    'dockerfiles/Dockerfile-python',
                                ]
                                dockerfiles.each { path ->
                                    def content = libraryResource(path)
                                    writeFile file: ".pipeline/${path}", text: content
                                }

                                sh 'find .pipeline/scripts -name "*.sh" -exec chmod +x {} +'
                            }
                        }
                    }

                    stage('Detect') {
                        steps {
                            script {
                                def output = sh(
                                    script: 'bash .pipeline/scripts/detect.sh',
                                    returnStdout: true
                                ).trim()
                                output.split('\n').each { line ->
                                    def parts = line.split('=', 2)
                                    if (parts.size() == 2) {
                                        env[parts[0].trim()] = parts[1].trim()
                                    }
                                }
                                echo "[detect] Language: ${env.LANGUAGE}, BuildTool: ${env.BUILD_TOOL}"
                            }
                        }
                    }

                }
            }

            // ── Continuous Integration（持續整合）群組 ──────────────────────
            // 程式碼整合與驗證：編譯 → 測試 → 打包成 artifact
            stage('Continuous Integration（持續整合）') {
                stages {

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
                                junit allowEmptyResults: true,
                                      testResults: '**/target/surefire-reports/*.xml'
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
            // Docker Build 僅受 flag 控制；Harbor Push 以後受 CD_ENABLED（branch）+ flag 雙重把關
            stage('Continuous Delivery（持續交付）') {
                stages {

                    stage('Docker Build') {
                        // cdStages.dockerBuild = false 時跳過（archive: false 時依賴推導自動關閉）
                        when { expression { cdStages.dockerBuild } }
                        steps {
                            sh 'bash .pipeline/scripts/cd.sh docker-build'
                        }
                    }

                    stage('Image Scan') {
                        // Trivy 掃描 Docker Build 產生的本地 image
                        // main branch：warn only（exit-code 0）；prod：HIGH/CRITICAL 時 fail（exit-code 1）
                        // develop branch：cd.sh 內部依 branch 自動跳過
                        // cdStages.imageScan = false 時跳過（dockerBuild: false 時依賴推導自動關閉）
                        when { expression { cdStages.imageScan } }
                        steps {
                            sh 'bash .pipeline/scripts/cd.sh image-scan'
                        }
                        // Trivy JUnit XML 已移至 pipeline post.always 統一收集
                    }

                    stage('Harbor Push') {
                        when {
                            allOf {
                                // branch 判斷（develop / main / prod）
                                expression { env.CD_ENABLED == 'true' }
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
                                expression { env.CD_ENABLED == 'true' }
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
                                expression { env.CD_ENABLED == 'true' }
                                expression { cdStages.deploy }
                            }
                        }
                        steps {
                            script {
                                // prod branch 需人工確認後才執行部署（防止誤觸）
                                def branch = env.GIT_BRANCH?.replaceAll(/^origin\//, '') ?: ''
                                if (branch == 'prod') {
                                    input message: "Deploy ${env.JOB_NAME} #${env.BUILD_NUMBER} to PROD namespace?",
                                          ok: 'Deploy to PROD'
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

                // JaCoCo Coverage HTML（main / prod branch 才產生，allowMissing 避免其他 branch fail）
                publishHTML(target: [
                    allowMissing          : true,
                    alwaysLinkToLastBuild : false,
                    keepAll               : true,
                    reportDir             : 'target/site/jacoco',
                    reportFiles           : 'index.html',
                    reportName            : 'JaCoCo Coverage Report'
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
