import groovy.json.JsonOutput

// The only runtime mutation job for the scoped native OCR release. All inputs
// are reviewed immutable pins; it never accepts a command, image tag, or retry.
def call(Map config = [:]) {
    def required = ['enabled', 'libraryRevision', 'sourceCommit', 'approvers',
                    'plan', 'planSHA256', 'ciReceipt', 'ciReceiptSHA256', 'artifactDirectory'] as Set
    if (config.keySet() != required || config.enabled != true ||
        ![config.libraryRevision, config.sourceCommit].every { it ==~ /[0-9a-f]{40}/ } ||
        ![config.planSHA256, config.ciReceiptSHA256].every { it ==~ /[0-9a-f]{64}/ } ||
        !(config.approvers instanceof List) || !config.approvers ||
        !config.approvers.every { it ==~ /[A-Za-z0-9_.@-]{1,80}/ } ||
        ![config.plan, config.ciReceipt, config.artifactDirectory].every {
            it instanceof String && it.startsWith('/Users/surpend/') && !it.contains('..') &&
            !(it =~ /[\u0000-\u001f\u007f]/) }) {
        error('Exact reviewed OCR deployment inputs required')
    }
    def causes = currentBuild.getBuildCauses()
    if (env.JOB_NAME != 'shiba-release-automation/apple-vision-native-deploy' ||
        causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        !config.approvers.contains(causes[0].userId) ||
        (params ?: [:]).any { key, value -> value?.toString()?.trim() }) {
        error('OCR deployment requires one approved manual cause and no parameters')
    }
    properties([disableConcurrentBuilds(), buildDiscarder(logRotator(numToKeepStr: '30')), pipelineTriggers([])])
    timeout(time: 15, unit: 'MINUTES') {
        node('shiba-prod-owner-macos') {
            dir("apple-ocr-deploy-${env.BUILD_NUMBER}") {
                deleteDir()
                stage('Checkout approved OCR owner session') {
                    dir('source') {
                        checkout([$class: 'GitSCM', branches: [[name: config.sourceCommit]],
                            userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-recognition-project.git',
                                                 credentialsId: 'github-scm-readonly']],
                            extensions: [[$class: 'CloneOption', noTags: true, shallow: false], [$class: 'CleanBeforeCheckout']]])
                    }
                }
                writeFile file: 'request.json', text: JsonOutput.toJson([
                    source_commit: config.sourceCommit, library_revision: config.libraryRevision,
                    plan: config.plan, plan_sha256: config.planSHA256,
                    ci_receipt: config.ciReceipt, ci_receipt_sha256: config.ciReceiptSHA256,
                    artifact_directory: config.artifactDirectory]) + '\n'
                try {
                stage('Replace OCR under owner transaction') {
                    // Argument values travel in JSON, never shell interpolation.
                    sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 -B - <<'PY'
import json, pathlib, subprocess
request=json.loads(pathlib.Path('request.json').read_text())
source=pathlib.Path('source').resolve()
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()==request['source_commit']
assert not subprocess.check_output(['git','status','--porcelain'],cwd=source).strip()
command=['python3','-B',str(source/'scripts/ocr_release_session.py'),'apply']
for key in ('plan','plan_sha256','ci_receipt','ci_receipt_sha256','artifact_directory'):
    command += ['--'+key.replace('_','-'),request[key]]
command += ['--output',str(pathlib.Path('runtime-receipt.json').resolve()),'--confirm-prod-ocr']
subprocess.run(command,check=True,timeout=660)
receipt=json.loads(pathlib.Path('runtime-receipt.json').read_text())
assert receipt['schema']=='ocr-native-deployment-v1' and receipt['state']=='succeeded'
assert receipt['owner_reactivated'] is True and receipt['replacement']['state']=='succeeded'
assert receipt['plan_sha256']==request['plan_sha256'] and receipt['ci_receipt_sha256']==request['ci_receipt_sha256']
assert receipt['job_name']=='shiba-release-automation/apple-vision-native-deploy'
assert receipt['build_number']==int(__import__('os').environ['BUILD_NUMBER'])
PY
'''
                }
                } finally {
                stage('Archive OCR owner receipt') {
                    archiveArtifacts artifacts: 'runtime-receipt*.json,request.json', allowEmptyArchive: false, fingerprint: true
                }
                }
            }
        }
    }
}
