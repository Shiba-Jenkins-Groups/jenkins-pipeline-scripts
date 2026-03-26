def call(Map config = [:]) {
    def githubCredentials   = config.githubCredentials   ?: error('githubCredentials is required')
    // harborCredentials：Harbor Robot Account Credential ID（Jenkins Credentials 中定義）
    def harborCredentials   = config.harborCredentials   ?: error('harborCredentials is required')

    pipeline {
        agent {
            label 'docker-agent'
        }

        options {
            timestamps()
            disableConcurrentBuilds()
        }

        environment {
            GITHUB_CREDENTIALS  = credentials("${githubCredentials}")
        }

        stages {

            stage('Checkout') {
                steps {
                    checkout scm
                    script {
                        // checkout scm 完成後 GIT_BRANCH 才可用
                        // 去除 origin/ 前綴後比對 develop / main / prod
                        def branch = env.GIT_BRANCH?.replaceAll(/^origin\//, '') ?: ''
                        // CD_ENABLED：develop / main / prod 啟用；其他 branch 跳過 Harbor Push 與 Deploy
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
                            'scripts/java/java-env.sh',
                            'scripts/java/java-build.sh',
                            'scripts/java/java-test.sh',
                            'scripts/java/java-archive.sh',
                            'scripts/node/node-build.sh',
                            'scripts/node/node-test.sh',
                            'scripts/node/node-archive.sh',
                            'scripts/python/python-build.sh',
                            'scripts/python/python-test.sh',
                            'scripts/python/python-archive.sh',
                        ]
                        scripts.each { path ->
                            def content = libraryResource(path)
                            writeFile file: ".pipeline/${path}", text: content
                        }

                        def dockerfiles = [
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

            stage('Build') {
                steps {
                    sh "bash .pipeline/scripts/${env.LANGUAGE}/${env.LANGUAGE}-build.sh"
                }
            }

            stage('Test') {
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
                steps {
                    sh "bash .pipeline/scripts/${env.LANGUAGE}/${env.LANGUAGE}-archive.sh"
                }
            }

            stage('Docker Build') {
                steps {
                    sh 'bash .pipeline/scripts/cd.sh docker-build'
                }
            }

            stage('Harbor Push') {
                when {
                    expression { env.CD_ENABLED == 'true' }
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

            stage('Deploy') {
                when {
                    expression { env.CD_ENABLED == 'true' }
                }
                steps {
                    sh 'bash .pipeline/scripts/cd.sh deploy'
                }
            }

        }

        post {
            always {
                cleanWs()
            }
            success {
                echo "Pipeline SUCCESS — ${env.JOB_NAME} #${env.BUILD_NUMBER}"
            }
            failure {
                echo "Pipeline FAILED — ${env.JOB_NAME} #${env.BUILD_NUMBER}"
            }
        }
    }
}
