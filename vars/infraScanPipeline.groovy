// infraScanPipeline — 基礎設施 image 弱點掃描（週期性）
//
// 為何獨立於 ciPipeline：掃描對象不是任何專案的交付物，沒有 Checkout／Build／Deploy，
// 也不該套用 branch-policy（無 branch 概念）。硬塞進 ciPipeline 會違反 SRP。
//
// 掃描分工（三者互補、不重疊）：
//   cd.sh            → 各專案 app image，build 當下掃一次
//   Harbor auto_scan → Harbor 內既有 app image，持續重掃（新 CVE 出現時才會發現）
//   本 pipeline       → 基礎設施 image（agent／Harbor 組件），週期性掃描
//
// 政策：純 warn。掃的是基礎設施非交付物，fail 沒有「該擋下這次 build」的意義。
//       junit 發布僅供趨勢觀察——**掃到弱點會使 build UNSTABLE，此為預期**（與 app image 同型）。
//
// 用法（jenkins.yaml 的 jobs 宣告）：
//   infraScanPipeline()                         // 預設清單：agent ＋ Harbor 全組件
//   infraScanPipeline(images: ['foo', 'bar'])   // 指定清單
//   infraScanPipeline(cron: 'H 3 * * 1')        // 覆寫排程

def call(Map config = [:]) {
    // 每週一凌晨 3 點（H＝散列到該小時內某分鐘，避免所有 job 同時觸發）
    def cronSpec = config.get('cron', 'H 3 * * 1')
    def images   = config.get('images', [])

    pipeline {
        agent {
            label 'docker-agent'
        }

        triggers {
            cron(cronSpec)
        }

        options {
            timestamps()
            buildDiscarder(logRotator(numToKeepStr: '30'))
            // 掃描含 Trivy DB 下載，給足時間；卡住即中止避免佔用 agent
            timeout(time: 45, unit: 'MINUTES')
        }

        stages {
            stage('Load Scripts') {
                steps {
                    script {
                        // 同 ciPipeline 慣例：Shared Library resources 在 Controller，
                        // Agent 無法直接存取，需 libraryResource() 讀出後寫入 workspace
                        writeFile file: '.pipeline/scripts/common/infra-scan.sh',
                                  text: libraryResource('scripts/common/infra-scan.sh')
                        sh 'chmod +x .pipeline/scripts/common/infra-scan.sh'
                    }
                }
            }

            stage('Infra Scan') {
                steps {
                    script {
                        def targets = images.join(' ')
                        // TRIVY_CACHE_DIR 指向 agent 的持久化 cache volume（jenkins-agent-cache，#5）
                        // → Trivy DB 跨動態 agent 存活，免每次重抓 ~100MB
                        withEnv(["TRIVY_CACHE_DIR=${env.HOME}/.cache/trivy"]) {
                            sh "bash .pipeline/scripts/common/infra-scan.sh reports/infra-scan ${targets}"
                        }
                    }
                }
            }
        }

        post {
            always {
                // 報告路徑契約化（同 #2）：發布者只認 reports/ 下的產出
                junit allowEmptyResults: true, testResults: 'reports/infra-scan/*.xml'
                archiveArtifacts artifacts: 'reports/infra-scan/*.xml',
                                 allowEmptyArchive: true, fingerprint: false
            }
            unstable {
                echo '''
╔══════════════════════════════════════════════╗
║  INFRA SCAN：發現 HIGH/CRITICAL 弱點          ║
╠══════════════════════════════════════════════╣
║  UNSTABLE 為預期（warn 檔位，不擋任何交付）    ║
║  查看：本 build 的 Test Result / Artifacts    ║
╠══════════════════════════════════════════════╣
║  處理方式：                                   ║
║   agent image  → 升 base image 的 pin 版本    ║
║                  （docker-compose/agent/      ║
║                   Dockerfile 註解有流程）      ║
║   Harbor 組件  → 升 Harbor 版本（需 DB        ║
║                  migration，不可逆，先備份）   ║
╚══════════════════════════════════════════════╝
'''
            }
        }
    }
}
