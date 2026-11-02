import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// This code executes in the trusted Library, never the sandboxed job script.
def decodeNativeConfiguration(Object input) {
    if (input instanceof Map) { return input }
    if (!(input instanceof String) || input.size() == 0 || input.size() > 65536 ||
        input.size() % 4 != 0 || !(input ==~ /[A-Za-z0-9+\/]*={0,2}/)) {
        throw new IllegalArgumentException('Bounded canonical base64 native configuration required')
    }
    try {
        byte[] raw = java.util.Base64.decoder.decode(input)
        if (raw.length > 49152 || java.util.Base64.encoder.encodeToString(raw) != input) {
            throw new IllegalArgumentException('Native configuration encoding differs')
        }
        def decoder = java.nio.charset.StandardCharsets.UTF_8.newDecoder()
        decoder.onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
        decoder.onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
        def value = new JsonSlurperClassic().parseText(decoder.decode(java.nio.ByteBuffer.wrap(raw)).toString())
        if (!(value instanceof Map)) { throw new IllegalArgumentException('Native configuration must be an object') }
        return value
    } catch (Exception failure) {
        throw new IllegalArgumentException('Invalid encoded native configuration')
    }
}

// Native artifact producer only. Runtime replacement belongs to the managed owner.
def call(Object input = [:]) {
    def config = decodeNativeConfiguration(input)
    def job = 'shiba-release-automation/apple-vision-native-release'
    def required = ['enabled', 'libraryRevision', 'sourceCommit', 'sourceBaselineCommit', 'ownerSourceCommit', 'ownerBaselineCommit',
                    'ownerPackage', 'ownerTestRun', 'goVersion', 'approvers', 'parentCI', 'scannerTools'] as Set
    def parentKeys = ['schema', 'status', 'source_commit', 'image', 'job_name', 'build_number', 'build_url', 'artifact_hash_inventory'] as Set
    if (config.keySet() != required || config.enabled != true ||
        ![config.libraryRevision, config.sourceCommit, config.sourceBaselineCommit, config.ownerSourceCommit, config.ownerBaselineCommit].every { it ==~ /[0-9a-f]{40}/ } ||
        !(config.ownerPackage ==~ /\.\/cmd\/[A-Za-z0-9_\/-]+/) || config.ownerPackage.contains('..') ||
        !(config.ownerTestRun ==~ /\^Test[A-Za-z0-9_]+/) ||
        !(config.goVersion ==~ /go1\.[0-9]+\.[0-9]+/) ||
        !(config.approvers instanceof List) || !config.approvers ||
        !config.approvers.every { it instanceof String && it ==~ /[A-Za-z0-9_.@-]{1,80}/ } ||
        !(config.parentCI instanceof Map) || config.parentCI.keySet() != parentKeys ||
        config.parentCI.schema != 'recognition-parent-ci-v1' || config.parentCI.status != 'SUCCESS' ||
        !(config.parentCI.source_commit ==~ /[0-9a-f]{40}/) ||
        !(config.parentCI.image ==~ /[A-Za-z0-9.:_\/-]+@sha256:[0-9a-f]{64}/) ||
        config.parentCI.job_name != 'shiba-go-ditch-recognition-project/prod' ||
        !(config.parentCI.build_number.toString() ==~ /[1-9][0-9]{0,8}/) ||
        !(config.parentCI.artifact_hash_inventory instanceof Map) || !config.parentCI.artifact_hash_inventory ||
        !(config.scannerTools instanceof Map) || config.scannerTools.keySet() != (['gitleaks', 'govulncheck'] as Set)) {
        error('Exact reviewed native OCR release configuration required')
    }
    def expectedParentUrl = "http://127.0.0.1:9090/job/shiba-go-ditch-recognition-project/job/prod/${config.parentCI.build_number}/"
    if (config.parentCI.build_url != expectedParentUrl) { error('Parent build URL mismatch') }
    def causes = currentBuild.getBuildCauses()
    if (env.JOB_NAME != job || !(env.BUILD_NUMBER ==~ /[1-9][0-9]{0,8}/) ||
        causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        !config.approvers.contains(causes[0].userId) ||
        (params ?: [:]).any { key, value -> value?.toString()?.trim() }) {
        error('Native artifact job requires one approved manual cause and no parameters')
    }
    properties([disableConcurrentBuilds(), buildDiscarder(logRotator(numToKeepStr: '20')),
                pipelineTriggers([])])
    // Includes queue time. An offline owner agent is a prerequisite failure;
    // this job never installs, starts or stops agents or shared infrastructure.
    timeout(time: 30, unit: 'MINUTES') {
        node('shiba-prod-owner-macos') {
            dir("apple-ocr-native-${env.BUILD_NUMBER}") {
                deleteDir()
                stage('Check macOS native builder') {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 && "$(id -u)" != 0 ]]
python3 --version
xcrun --find swiftc >/dev/null
'''
                }
                stage('Checkout exact OCR and owner sources') {
                    [[directory: 'recognition-source', repository: 'shiba-go-ditch-recognition-project', commit: config.sourceCommit],
                     [directory: 'owner-source', repository: 'shiba-go-ditch-api-project', commit: config.ownerSourceCommit]].each { item ->
                        dir(item.directory) {
                            checkout([$class: 'GitSCM', branches: [[name: item.commit]],
                                userRemoteConfigs: [[url: "https://github.com/ShibaDev2026/${item.repository}.git",
                                                     credentialsId: 'github-scm-readonly']],
                                extensions: [[$class: 'CloneOption', noTags: true, shallow: false, honorRefspec: false],
                                             [$class: 'CleanBeforeCheckout']]])
                        }
                    }
                }
                stage('Build native OCR and owner artifacts') {
                    writeFile file: 'control/ocr-native-release.py', text: libraryResource('scripts/common/ocr-native-release.py')
                    writeFile file: 'identity.json', text: JsonOutput.toJson([
                        schema_version: 1, job_name: job, build_number: env.BUILD_NUMBER,
                        build_url: env.BUILD_URL, source_commit: config.sourceCommit, source_baseline_commit: config.sourceBaselineCommit,
                        owner_source_commit: config.ownerSourceCommit, owner_baseline_commit: config.ownerBaselineCommit,
                        library_revision: config.libraryRevision,
                        requested_by: causes[0].userId, owner_package: config.ownerPackage,
                        owner_test_run: config.ownerTestRun, go_version: config.goVersion,
                        parent_ci: config.parentCI, scanner_tools: config.scannerTools]) + '\n'
                    sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 -B control/ocr-native-release.py build --recognition-source "$PWD/recognition-source" --owner-source "$PWD/owner-source" --identity "$PWD/identity.json" --output "$PWD/artifact"
'''
                }
                stage('Archive immutable native release') {
                    archiveArtifacts artifacts: 'artifact/ocr-native-artifact.tar,artifact/ci-receipt.json,artifact/SHA256SUMS,artifact/contracts.log',
                                     allowEmptyArchive: false, fingerprint: true
                    currentBuild.description = "Native OCR ${config.sourceCommit.take(12)} + owner ${config.ownerSourceCommit.take(12)}; artifacts only"
                }
            }
        }
    }
}
