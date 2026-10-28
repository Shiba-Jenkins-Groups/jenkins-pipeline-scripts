import groovy.json.JsonOutput

// Only a centrally reviewed, pinned-library standalone job may call this.
// It never invokes the application's branch Jenkinsfile or ciPipeline.
def call(Map config = [:]) {
    def job = 'shiba-local-artifacts/recognition-autorecovery'
    def source = '9d0400d003de68be827524d21543200bf78bfdfd'
    def keys = ['enabled', 'libraryRevision', 'sourceCommit', 'approvers', 'builderEngineId'] as Set
    if (config.keySet() != keys || config.enabled != true || config.sourceCommit != source ||
        !(config.libraryRevision ==~ /[0-9a-f]{40}/) ||
        !(config.builderEngineId ==~ /[0-9a-f-]{36}/) ||
        !(config.approvers instanceof List) || !config.approvers ||
        !config.approvers.every { it instanceof String && it ==~ /[A-Za-z0-9_.@-]{1,80}/ }) {
        error('Explicit reviewed LOCAL artifact configuration required')
    }
    def causes = currentBuild.getBuildCauses()
    if (env.JOB_NAME != job || env.CHANGE_ID?.trim() ||
        !(env.BUILD_NUMBER ==~ /[1-9][0-9]{0,8}/) ||
        causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        !config.approvers.contains(causes[0].userId) ||
        (params ?: [:]).any { key, value -> value?.toString()?.trim() }) {
        error('LOCAL artifact job requires one approved manual cause and no parameters')
    }
    properties([disableConcurrentBuilds(), buildDiscarder(logRotator(numToKeepStr: '20')),
                pipelineTriggers([])])
    def elfReceiptSha = ''
    timeout(time: 35, unit: 'MINUTES') {
        node('ci-untrusted') {
            dir("recognition-local-artifact-${env.BUILD_NUMBER}") {
                stage('Check isolated nonprivileged agent') {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 && "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]]
[[ ! -e /var/run/docker.sock && ! -e /run/docker.sock ]]
[[ -z "${DOCKER_HOST:-}" && -z "${DOCKER_CONTEXT:-}" ]]
[[ ! -e /var/jenkins_home && ! -e /Users/surpend/Developer ]]
python3 --version
git --version
'''
                }
                deleteDir()
                stage('Checkout reviewed runtime source') {
                    dir('source') {
                        checkout([$class: 'GitSCM', branches: [[name: source]],
                            userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-recognition-project.git',
                                                 credentialsId: 'github-scm-readonly']],
                            extensions: [[$class: 'CloneOption', noTags: true, shallow: false,
                                          honorRefspec: false], [$class: 'CleanBeforeCheckout']]])
                    }
                }
                stage('Load reviewed builder and pinned SDK') {
                    ['recognition-local-artifact.py', 'recognition-local-go-toolchain.py'].each { name ->
                        writeFile file: ".ci/${name}", text: libraryResource("scripts/common/${name}")
                    }
                    writeFile file: '.ci/identity.json', text: JsonOutput.toJson([
                        schema_version: 1, job_name: job, build_number: env.BUILD_NUMBER,
                        build_url: env.BUILD_URL, source_commit: source,
                        library_revision: config.libraryRevision, requested_by: causes[0].userId]) + '\n'
                    sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 .ci/recognition-local-go-toolchain.py --workspace "$PWD" >.ci/sdk-root.txt
'''
                }
                stage('Build test and archive LOCAL runtime only') {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
IFS= read -r sdk_root <.ci/sdk-root.txt
python3 .ci/recognition-local-artifact.py build --source "$PWD/source" --output "$PWD/artifact" --identity "$PWD/.ci/identity.json" --sdk-root "$sdk_root"
'''
                    archiveArtifacts artifacts: 'artifact/recognition-local.tar,artifact/artifact-manifest.json,artifact/artifact-receipt.json,artifact/SHA256SUMS,artifact/contracts.log',
                                     allowEmptyArchive: false, fingerprint: true
                    elfReceiptSha = sh(script: 'sha256sum artifact/artifact-receipt.json', returnStdout: true).trim().tokenize(' ')[0]
                    if (!(elfReceiptSha ==~ /[0-9a-f]{64}/)) { error('ELF receipt transfer binding missing') }
                    stash name: 'reviewed-recognition-local-elf', includes: 'artifact/recognition-local.tar,artifact/artifact-manifest.json,artifact/artifact-receipt.json', useDefaultExcludes: true
                }
            }
        }
        node('ci-image-builder') {
            dir("recognition-local-image-${env.BUILD_NUMBER}") {
                stage('Build and save approved LOCAL image') {
                    deleteDir()
                    unstash 'reviewed-recognition-local-elf'
                    writeFile file: '.ci/recognition-local-artifact.py', text: libraryResource('scripts/common/recognition-local-artifact.py')
                    withEnv(["LOCAL_ELF_RECEIPT_SHA=${elfReceiptSha}",
                             "LOCAL_LIBRARY_REVISION=${config.libraryRevision}",
                             "LOCAL_BUILD_NUMBER=${env.BUILD_NUMBER}",
                             "LOCAL_BUILDER_ENGINE_ID=${config.builderEngineId}"]) {
                        sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 .ci/recognition-local-artifact.py image --elf-archive "$PWD/artifact/recognition-local.tar" --elf-receipt "$PWD/artifact/artifact-receipt.json" --expected-elf-receipt-sha256 "$LOCAL_ELF_RECEIPT_SHA" --build-number "$LOCAL_BUILD_NUMBER" --library-revision "$LOCAL_LIBRARY_REVISION" --expected-engine-id "$LOCAL_BUILDER_ENGINE_ID" --output "$PWD/image-artifact"
'''
                    }
                    archiveArtifacts artifacts: 'image-artifact/recognition-local-image.tar,image-artifact/image-receipt.json,image-artifact/SHA256SUMS',
                                     allowEmptyArchive: false, fingerprint: true
                    currentBuild.description = 'LOCAL Recognition binary + Docker image archive only; no deployment or PROD downstream'
                }
            }
        }
    }
}
