import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// Decode only inside the trusted Library. The sandboxed job calls one function.
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

// The only runtime mutation job for the scoped native OCR release. All inputs
// are reviewed immutable pins; it never accepts a command, image tag, or retry.
def call(Object input = [:]) {
    def config = decodeNativeConfiguration(input)
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
    timeout(time: 20, unit: 'MINUTES') {
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
import hashlib, json, pathlib, re, subprocess
request=json.loads(pathlib.Path('request.json').read_text())
source=pathlib.Path('source').resolve()
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()==request['source_commit']
assert not subprocess.check_output(['git','status','--porcelain'],cwd=source).strip()
plan_raw=pathlib.Path(request['plan']).read_bytes()
assert hashlib.sha256(plan_raw).hexdigest()==request['plan_sha256']
plan=json.loads(plan_raw)
command=['python3','-B',str(source/'scripts/ocr_release_session.py'),'apply']
for key in ('plan','plan_sha256','ci_receipt','ci_receipt_sha256','artifact_directory'):
    command += ['--'+key.replace('_','-'),request[key]]
command += ['--output',str(pathlib.Path('runtime-receipt.json').resolve()),'--confirm-prod-ocr']
subprocess.run(command,check=True,timeout=960)
receipt=json.loads(pathlib.Path('runtime-receipt.json').read_text())
assert receipt['schema']=='ocr-native-deployment-v1' and receipt['state']=='succeeded'
assert receipt['owner_reactivated'] is True
schema=plan.get('schema')
if schema in {'managed-recognition-runtime-binding-refresh/v1','managed-recognition-runtime-binding-refresh/v2'}:
    assert receipt['route']=='recognition_runtime_binding' and receipt['ocr_replacement_submitted'] is False
    refresh=receipt['recognition_refresh']
    assert refresh['state'] in {'binding_committed', 'binding_committed_unready'}
    assert refresh['binding_committed'] is True and refresh['file_mutated'] is True
    assert refresh['platform_mutated'] is False and refresh['maintenance_released'] is True
    assert receipt['binding_checkpoint']['state']==refresh['state']
    assert receipt['operation_id']==refresh['operation_id']==plan['operation_id']
    if schema=='managed-recognition-runtime-binding-refresh/v2':
        assert plan['schema_version']==2 and refresh['state']=='binding_committed'
        assert receipt['recognition_status']['contract_health']=='ready'
        assert len(receipt['restart_continuity_validations'])==2
        for validation in receipt['restart_continuity_validations']:
            assert validation['operation_id']==plan['operation_id']
            assert validation['restart_continuity']==plan['restart_continuity']
            assert validation['native_ci_receipt']=={'path':request['ci_receipt'],'sha256':request['ci_receipt_sha256']}
        observations=receipt['recognition_refresh_continuity_observations']
        assert observations['path']==str(pathlib.Path(refresh['receipt_directory'])/'continuity-observations.json')
        assert re.fullmatch(r'[0-9a-f]{64}',observations['sha256'])
else:
    assert receipt['replacement']['state']=='succeeded'
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
