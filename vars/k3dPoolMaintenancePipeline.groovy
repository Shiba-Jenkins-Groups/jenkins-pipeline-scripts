def call(Map config = [:]) {
    def kubeconfigCredentials = config.kubeconfigCredentials ?: 'k3s-kubeconfig'
    def namespaceTtlSeconds = (config.namespaceTtlSeconds ?: 7200).toString()

    pipeline {
        agent { label 'ci-image-builder' }

        options {
            timestamps()
            disableConcurrentBuilds()
            skipDefaultCheckout(true)
            buildDiscarder(logRotator(numToKeepStr: '30'))
        }

        stages {
            stage('Reclaim idle K3D verification slots') {
                steps {
                    script {
                        writeFile file: '.pipeline/k3d-pool-janitor.sh',
                            text: libraryResource('scripts/common/k3d-pool-janitor.sh')
                        writeFile file: '.pipeline/k3d-capacity.py',
                            text: libraryResource('scripts/common/k3d-capacity.py')
                        sh 'chmod +x .pipeline/k3d-pool-janitor.sh'
                    }
                    withCredentials([file(credentialsId: kubeconfigCredentials, variable: 'KUBECONFIG')]) {
                        withEnv(["K3D_POOL_NAMESPACE_TTL_SECONDS=${namespaceTtlSeconds}"]) {
                            sh 'bash .pipeline/k3d-pool-janitor.sh'
                        }
                    }
                }
            }
            stage('Observe K3D capacity') {
                steps {
                    withCredentials([file(credentialsId: kubeconfigCredentials, variable: 'KUBECONFIG')]) {
                        script {
                            int status = sh(script: 'python3 .pipeline/k3d-capacity.py --mode monitor --output .pipeline/k3d-capacity.json', returnStatus: true)
                            archiveArtifacts artifacts: '.pipeline/k3d-capacity.json', allowEmptyArchive: false
                            if (status == 1) {
                                unstable('K3D capacity warning threshold reached')
                            } else if (status != 0) {
                                error('K3D capacity critical threshold reached or observation failed')
                            }
                        }
                    }
                }
            }
        }

        post {
            always { cleanWs(deleteDirs: true, notFailBuild: true) }
        }
    }
}
