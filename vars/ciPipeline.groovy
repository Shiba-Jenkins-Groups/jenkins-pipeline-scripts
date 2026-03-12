def call(Map config = [:]) {
    def githubCredentials = config.githubCredentials ?: error('githubCredentials is required')

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
            CD_ENABLED          = 'false'
        }

        stages {
            stage('Checkout') {
                steps {
                    checkout scm
                }
            }

            stage('Load Scripts') {
                steps {
                    script {
                        // Shared Library 的 scripts 在 Controller 上，Agent 無法直接存取
                        // 用 libraryResource() 讀取內容並寫入 Agent workspace
                        def scripts = [
                            'scripts/ci.sh',
                            'scripts/cd.sh',
                            'scripts/common/error-handler.sh',
                            'scripts/common/docker.sh',
                            'scripts/common/git-tag.sh',
                            'scripts/common/archive-base.sh',
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

                        // Dockerfile 也一併寫入（供 docker.sh 使用）
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

            stage('CI') {
                steps {
                    sh 'bash .pipeline/scripts/ci.sh'
                }
            }

            stage('CD') {
                when {
                    expression { env.CD_ENABLED == 'true' }
                }
                steps {
                    sh 'bash .pipeline/scripts/cd.sh'
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
