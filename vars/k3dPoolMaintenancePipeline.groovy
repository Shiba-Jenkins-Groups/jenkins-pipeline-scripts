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
                        sh 'chmod +x .pipeline/k3d-pool-janitor.sh'
                    }
                    withCredentials([file(credentialsId: kubeconfigCredentials, variable: 'KUBECONFIG')]) {
                        withEnv(["K3D_POOL_NAMESPACE_TTL_SECONDS=${namespaceTtlSeconds}"]) {
                            sh 'bash .pipeline/k3d-pool-janitor.sh'
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
