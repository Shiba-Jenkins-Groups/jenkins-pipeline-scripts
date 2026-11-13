import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// Plain Groovy mocks only: sh, checkout, build, node and credentials NEVER run.
def root = new File(args ? args[0] : '.').canonicalFile
def product = 'shiba-go-ditch-api-project'
def releaseFolder = 'shiba-release-automation'
def config = [enabled: true, approvers: ['reviewer'], jenkinsApiUrl: 'http://jenkins.invalid',
    jenkinsReadCredentials: 'read', harborCredentials: 'harbor', harborApiUrl: 'http://harbor.invalid',
    scmCredentials: 'scm', mergeCredentials: 'merge', approvalKeyCredentials: 'approval', receiptKeyCredentials: 'receipt',
    stateDirectory: '/fake/state', builderLabel: 'builder', deploymentJob: releaseFolder + '/' + product + '-prod-deploy',
    deploymentNodeLabel: 'mac-prod', nodeLabel: 'mac-prod', runtimeRoot: '/fake/runtime', dockerEngineId: 'fake-engine', libraryRevision: 'f' * 40,
    finalizationStateDirectory: '/fake/finalization', finalizationWriterCredentials: 'tag-writer', nexusCredentials: 'nexus', nexusBaseUrl: 'http://nexus.invalid']

def simulate = { String filename, Map options = [:] ->
    boolean deploy = filename == 'prodDeploymentPipeline.groovy'
    def calls = []
    def credentialGroups = []
    def handoffEvents = []
    def deploymentParameters = []
    def writtenFiles = [:]
    def binding = new Binding()
    binding.setVariable('env', [JOB_NAME: releaseFolder + '/' + product + (deploy ? '-prod-deploy' : '-auto-release'), BUILD_NUMBER: '1'])
    binding.setVariable('params', [SIGNED_RELEASE_REQUEST: '{}'] + (options.recoveryParams ?: [:]))
    binding.setVariable('currentBuild', [getBuildCauses: { String type = null ->
        if (options.recoveryParams) {
            if (type == null) { return [[_class: options.replay ? 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause' : 'hudson.model.Cause$UserIdCause', userId: options.badUser ? 'intruder' : 'reviewer']] }
            if (type == 'hudson.model.Cause$UserIdCause') { return [[userId: options.badUser ? 'intruder' : 'reviewer']] }
            return []
        }
        def causes = [[upstreamProject: options.wrongCause ? 'untrusted' : (deploy ? releaseFolder + '/' + product + '-auto-release' : product + '/develop'), upstreamBuild: 188]]
        options.multipleCause ? causes + [[upstreamProject: 'unexpected', upstreamBuild: 1]] : causes
    }])
    binding.setVariable('error', { String message -> throw new IllegalStateException(message) })
    ['properties', 'archiveArtifacts', 'checkout', 'writeFile'].each { name ->
        binding.setVariable(name, { Object value ->
            calls << name
            if (name == 'archiveArtifacts') {
                def entries = value.artifacts.tokenize(',')
                if (!value.allowEmptyArchive && entries.contains('deployment-request.json')) {
                    handoffEvents << 'signed-request-archived'
                }
                if (entries.contains('finalization.json') && !entries.contains('control/policy.json')) {
                    handoffEvents << 'signed-finalization-archived'
                }
                if (entries == ['omlx-recovery-handoff.json']) { handoffEvents << 'omlx-handoff-archived' }
                if (entries == ['omlx-recovery-receipt.json']) { handoffEvents << 'omlx-receipt-archived' }
            }
            if (name == 'writeFile') { writtenFiles[value.file] = value.text }
            if (name == 'archiveArtifacts' && options.archiveFailure) { throw new IllegalStateException('archive unavailable') }
        })
    }
    binding.setVariable('deleteDir', { -> calls << 'deleteDir' })
    ['disableConcurrentBuilds', 'pipelineTriggers', 'upstream', 'usernamePassword', 'file', 'string', 'parameters'].each { name ->
        binding.setVariable(name, { Object... value -> [step: name] })
    }
    binding.setVariable('text', { Map value -> [step: 'text'] + value })
    ['node', 'dir', 'withEnv', 'withCredentials', 'timeout'].each { name ->
        binding.setVariable(name, { Object value, Closure body -> body() })
    }
    ['usernamePassword', 'file'].each { name ->
        binding.setVariable(name, { Map value -> [step: name] + value })
    }
    binding.setVariable('withCredentials', { List values, Closure body ->
        def ids = values.collect { it.credentialsId }
        credentialGroups << ids
        if (options.missingCredential && ids.contains(options.missingCredential)) {
            throw new IllegalStateException('fake missing credential')
        }
        body()
    })
    binding.setVariable('stage', { String name, Closure body ->
        calls << name
        if (name == options.failStage) { throw new IllegalStateException('fake failed stage') }
        body()
    })
    binding.setVariable('pwd', { -> '/fake/workspace' })
    binding.setVariable('libraryResource', { String name -> '# fake trusted resource' })
    binding.setVariable('sh', { Object script ->
        calls << script.toString()
        if (options.preflightFailure && script.toString().contains('release-preflight.py')) {
            throw new IllegalStateException('fake invalid bound credential')
        }
    })
    binding.setVariable('waitForBuild', { Map values ->
        assert values.runId == product + '/develop#188'
        [result: options.upstreamResult ?: 'SUCCESS']
    })
    binding.setVariable('build', { Map values ->
        calls << 'build:' + values.job
        if (values.job == config.deploymentJob) {
            handoffEvents << 'owner-build'
            deploymentParameters << values.parameters
        }
        [number: 103, result: options.prodResult ?: 'SUCCESS']
    })
    binding.setVariable('timeout', { Map values, Closure body ->
        handoffEvents << [timeout: values]
        if (options.handoffTimeout) {
            throw new IllegalStateException('mocked handoff timeout')
        }
        body()
    })
    binding.setVariable('input', { Map values ->
        calls << 'input'
        handoffEvents << [input: values]
        if (options.handoffAbort) { throw new IllegalStateException('mocked handoff rejection') }
        [APPROVER: options.badApprover ? 'intruder' : 'reviewer', REASON: 'offline exact CVE review'] + (options.inputResponse ?: [:])
    })
    binding.setVariable('readFile', { String path ->
        if (path.endsWith('source-status.json')) { return JsonOutput.toJson([
            decision: options.sourceStatus ?: 'NEW', source_commit: 'b' * 40, merge_commit: 'c' * 40]) }
        if (path.endsWith('review.json')) { return JsonOutput.toJson([decision: options.review ?: 'PASS', findings: [fake: [:]]]) }
        if (path.endsWith('runtime-receipt.json')) { return JsonOutput.toJson([payload: [status: 'SUCCESS']]) }
        if (path.endsWith('deployment-request.json') && options.signedRequest) { return options.signedRequest }
        if (path.endsWith('promotion.json')) {
            return JsonOutput.toJson([payload: [merge_commit: 'b' * 40, source_commit: options.promotionSource ?: 'b' * 40,
                                               version: options.promotionVersion ?: '1.0.32']])
        }
        if (path.endsWith('finalization.json')) {
            return JsonOutput.toJson([payload: [kind: 'finalization', product: product, status: options.finalizationStatus ?: 'SUCCESS',
                commit: options.finalizationCommit ?: 'b' * 40, version: options.finalizationVersion ?: options.promotionVersion ?: '1.0.32',
                digest: options.finalizationDigest ?: 'sha256:' + 'd' * 64, manifest: [IMAGE_REF: options.finalizationImage ?: 'localhost:9290/' + product + '/prod/1.1.24:103',
                    IMAGE_DIGEST: options.finalizationManifestDigest ?: 'sha256:' + 'd' * 64]]])
        }
        if (path.endsWith('identity.json')) {
            return JsonOutput.toJson([commit: options.wrongProdSha && path.contains('/prod/') ? 'c' * 40 : 'b' * 40,
                                      version: options.promotionVersion ?: '1.0.32', digest: 'sha256:' + 'd' * 64,
                                      image_digest: 'sha256:' + 'd' * 64,
                                      immutable_image: 'fixture/repo@sha256:' + 'd' * 64, library_revision: config.libraryRevision])
        }
        return '{}'
    })
    def failed = false
    def failureMessage = null
    try {
        def pipeline = new GroovyShell(binding).parse(new File(root, 'vars/' + filename))
        pipeline.call(config + (options.disabled ? [enabled: false] : [:]) + (options.trustedConfig ?: [:]))
    } catch (IllegalStateException | groovy.json.JsonException expected) {
        failed = true
        failureMessage = expected.message
    }
    [calls: calls, credentialGroups: credentialGroups, failed: failed, failureMessage: failureMessage, handoffEvents: handoffEvents,
     deploymentParameters: deploymentParameters, writtenFiles: writtenFiles]
}

int tests = 0
def preflight = simulate('autoReleasePipeline.groovy')
assert !preflight.failed
def allCredentials = ['read', 'harbor', 'scm', 'merge', 'tag-writer', 'nexus', 'approval', 'receipt']
assert preflight.credentialGroups[0] == allCredentials
assert preflight.calls.indexOf('python3 control/release-preflight.py') < preflight.calls.indexOf('develop: Complete Build Evidence')
assert preflight.calls.findIndexOf { it.toString().contains('check-routing') } < preflight.calls.indexOf('develop: Complete Build Evidence')
tests++
for (options in allCredentials.collect { [missingCredential: it] } + [[preflightFailure: true]]) {
    def rejected = simulate('autoReleasePipeline.groovy', options)
    assert rejected.failed
    assert !rejected.calls.contains('develop: Complete Build Evidence')
    assert !rejected.calls.contains('checkout')
    assert !rejected.calls.any { it.toString().startsWith('build:') }
    tests++
}
for (options in [[:], [prodResult: 'FAILURE'], [review: 'BLOCKED'], [failStage: 'develop: Release Gate']]) {
    def cleaned = simulate('autoReleasePipeline.groovy', options)
    assert cleaned.calls.count('deleteDir') == 1
    assert cleaned.calls[-2] == 'archiveArtifacts'
    assert cleaned.calls[-1] == 'deleteDir'
    tests++
}
def retained = simulate('autoReleasePipeline.groovy', [archiveFailure: true])
assert retained.failed && !retained.calls.contains('deleteDir')
tests++
def recovery = [SOURCE_BUILD: '188', EXPECTED_COMMIT: 'b' * 40]
def recovered = simulate('autoReleasePipeline.groovy', [recoveryParams: recovery])
assert !recovered.failed
assert recovered.calls.any { it.toString().contains('release-promotion.py recover') }
assert !recovered.calls.any { it.toString().contains('release-promotion.py promote') }
tests++
def deploymentRecovery = recovery + [RECOVER_COORDINATOR_BUILD: '53', RECOVER_OWNER_BUILD: '19']
def deployedRecovery = simulate('autoReleasePipeline.groovy', [recoveryParams: deploymentRecovery])
assert !deployedRecovery.failed
assert deployedRecovery.calls.count('build:' + releaseFolder + '/' + product + '-prod-deploy') == 1
assert !deployedRecovery.calls.contains('build:' + product + '/prod')
assert !deployedRecovery.calls.contains('Promote Verified Commit')
assert !deployedRecovery.calls.contains('Finalize Revalidated PROD Candidate')
assert deployedRecovery.calls.any { it.toString().contains('release-recovery.py collect') }
assert deployedRecovery.calls.any { it.toString().contains('release-recovery.py handoff') }
tests++
for (options in [[recoveryParams: deploymentRecovery, badUser: true], [recoveryParams: deploymentRecovery, replay: true],
                 [recoveryParams: recovery + [RECOVER_COORDINATOR_BUILD: '53']],
                 [recoveryParams: [RECOVER_COORDINATOR_BUILD: '53', RECOVER_OWNER_BUILD: '19']],
                 [recoveryParams: deploymentRecovery, failStage: 'Revalidate Original Finalized Release'],
                 [recoveryParams: deploymentRecovery, failStage: 'Verify Existing Published Artifact']]) {
    def rejected = simulate('autoReleasePipeline.groovy', options)
    assert rejected.failed
    assert !rejected.calls.any { it.toString().startsWith('build:') }
    tests++
}
for (options in [[recoveryParams: recovery, badUser: true],
                 [recoveryParams: [SOURCE_BUILD: '188']],
                 [recoveryParams: [SOURCE_BUILD: '-1', EXPECTED_COMMIT: 'b' * 40]],
                 [recoveryParams: [SOURCE_BUILD: '188', EXPECTED_COMMIT: 'c' * 40]]]) {
    def rejected = simulate('autoReleasePipeline.groovy', options)
    assert rejected.failed
    assert !rejected.calls.any { it.toString().contains('release-promotion.py promote') || it.toString().startsWith('build:') }
    tests++
}
def result = simulate('autoReleasePipeline.groovy')
assert !result.failed
assert result.calls.count('build:' + product + '/prod') == 1
assert result.calls.count('build:' + releaseFolder + '/' + product + '-prod-deploy') == 1
assert !result.calls.contains('develop: All Severity Scans')
assert result.calls.any { it.toString().contains('--lean-develop') }
assert result.calls.any { it.toString().contains('bind-source') }
assert result.calls.indexOf('prod: Release Gate') < result.calls.indexOf('Finalize Revalidated PROD Candidate')
assert result.calls.indexOf('Finalize Revalidated PROD Candidate') < result.calls.indexOf('Authorize Deployment Handoff')
tests++
def duplicate = simulate('autoReleasePipeline.groovy', [sourceStatus: 'FINALIZED_DUPLICATE'])
assert !duplicate.failed
assert !duplicate.calls.contains('Prepare Managed Release Prerequisites')
assert !duplicate.calls.contains('Promote Verified Commit')
assert !duplicate.calls.any { it.toString().startsWith('build:') }
tests++
for (options in [[disabled: true], [wrongCause: true], [upstreamResult: 'FAILURE'],
                 [failStage: 'develop: Release Gate'], [review: 'BLOCKED'],
                 [review: 'NEEDS_APPROVAL', badApprover: true]]) {
    result = simulate('autoReleasePipeline.groovy', options)
    assert result.failed
    assert !result.calls.any { it.toString().contains('release-promotion.py promote') || it.toString().startsWith('build:') }
    tests++
}
for (options in [[prodResult: 'FAILURE'], [wrongProdSha: true], [failStage: 'prod: All Severity Scans'], [failStage: 'Finalize Revalidated PROD Candidate']]) {
    result = simulate('autoReleasePipeline.groovy', options)
    assert result.failed
    assert !result.calls.contains('build:' + releaseFolder + '/' + product + '-prod-deploy')
    tests++
}
result = simulate('autoReleasePipeline.groovy', [review: 'NEEDS_APPROVAL'])
assert result.failed && result.calls.count('input') == 0 // Lean develop cannot be waived into PROD.
tests++
assert !simulate('prodDeploymentPipeline.groovy').failed
tests++
for (options in [[disabled: true], [wrongCause: true], [multipleCause: true], [failStage: 'Verify Signed Deployment Request']]) {
    result = simulate('prodDeploymentPipeline.groovy', options)
    assert result.failed
    assert !result.calls.any { it.toString().contains('release-deploy.py deploy') }
    tests++
}
def targetSource = '66c9b360c066eafea0b681787cb8ee7fa826825c'
def targetOperation = '463a250290b6207ecafed7ffacf53d8f'
def trustedGate = [omlxRecoverySource: targetSource, omlxRecoveryVersion: '1.1.24',
    omlxRecoveryOperation: targetOperation, omlxRecoveryApprover: 'reviewer']
def target = [promotionSource: targetSource, promotionVersion: '1.1.24', trustedConfig: trustedGate,
    signedRequest: '{"fixture":"exact signed deployment request bytes"}']
def recoveryReceipt = [operation_id: targetOperation, resolved: true,
    start: [id: 'd' * 32, state: 'succeeded', changed: true, service: 'omlx', action: 'start',
            environment: 'prod', actor: 'owner-recovery']]
def receiptEnvelope = [source_commit: targetSource, commit: 'b' * 40, digest: 'sha256:' + 'd' * 64,
    coordinator_build: 1, prod_build: 103, owner: recoveryReceipt]
def approvedInput = [APPROVER: 'reviewer', RECOVERY_RECEIPT: JsonOutput.toJson(receiptEnvelope)]
def ownerJobCall = 'build:' + config.deploymentJob
def gateStage = 'Recover oMLX Before Deployment'
def incident = simulate('autoReleasePipeline.groovy', target + [inputResponse: approvedInput])
assert !incident.failed: incident.failureMessage
assert incident.calls.count(gateStage) == 1
assert incident.calls.count(ownerJobCall) == 1
assert incident.calls.indexOf('Finalize Revalidated PROD Candidate') < incident.calls.indexOf(gateStage)
assert incident.calls.indexOf(gateStage) < incident.calls.indexOf('Authorize Deployment Handoff')
assert incident.calls.indexOf('Authorize Deployment Handoff') < incident.calls.indexOf(ownerJobCall)
assert incident.handoffEvents[0..1] == ['signed-finalization-archived', 'omlx-handoff-archived']
assert incident.handoffEvents[2].timeout == [time: 15, unit: 'MINUTES']
assert incident.handoffEvents[3].input.id == 'omlx-recovery-ready'
assert incident.handoffEvents[3].input.submitter == 'reviewer'
assert incident.handoffEvents[3].input.submitterParameter == 'APPROVER'
assert incident.handoffEvents[3].input.parameters.any { it.name == 'RECOVERY_RECEIPT' }
assert incident.handoffEvents[4..-1] == ['omlx-receipt-archived', 'signed-request-archived', 'owner-build']
def expectedHandoff = receiptEnvelope.findAll { key, value -> key != 'owner' } + [version: '1.1.24',
    operation_id: targetOperation, image: 'localhost:9290/' + product + '/prod/1.1.24@sha256:' + 'd' * 64]
assert new JsonSlurperClassic().parseText(incident.writtenFiles['omlx-recovery-handoff.json']) == expectedHandoff
assert new JsonSlurperClassic().parseText(incident.writtenFiles['omlx-recovery-receipt.json']) == receiptEnvelope + [approver: 'reviewer']
assert incident.deploymentParameters == [[[step: 'text', name: 'SIGNED_RELEASE_REQUEST', value: target.signedRequest]]]
tests++

for (options in [[promotionSource: 'c' * 40], [trustedConfig: [:]],
                 [recoveryParams: deploymentRecovery],
                 [recoveryParams: [APP_BINDING_REPAIR_COORDINATOR_BUILD: '90',
                                   APP_BINDING_REPAIR_OWNER_BUILD: '19', APP_BINDING_REPAIR_PINS: '{}']],
                 [recoveryParams: [REBUILD_PROD_COMMIT: 'b' * 40, PUBLISHED_COORDINATOR_BUILD: '49'],
                  trustedConfig: trustedGate + [rebuildDockerEngineId: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee']]]) {
    def unaffected = simulate('autoReleasePipeline.groovy', target + options + [inputResponse: approvedInput])
    assert !unaffected.failed && !unaffected.calls.contains(gateStage) && unaffected.calls.count(ownerJobCall) == 1: unaffected.failureMessage
    tests++
}

def badReceipts = [[source_commit: 'c' * 40], [commit: 'c' * 40], [digest: 'sha256:' + 'a' * 64],
    [coordinator_build: 2], [prod_build: 104]].collect { JsonOutput.toJson(receiptEnvelope + it) }
for (badOwner in [recoveryReceipt + [resolved: false], recoveryReceipt + [operation_id: 'b' * 32],
    recoveryReceipt + [start: recoveryReceipt.start + [state: 'unknown']],
    recoveryReceipt + [start: recoveryReceipt.start + [id: targetOperation]],
    recoveryReceipt + [start: recoveryReceipt.start + [changed: false]],
    recoveryReceipt + [start: recoveryReceipt.start + [service: 'ollama']],
    recoveryReceipt + [start: recoveryReceipt.start + [action: 'restart']],
    recoveryReceipt + [start: recoveryReceipt.start + [environment: 'dev']],
    recoveryReceipt + [start: recoveryReceipt.start + [actor: 'different-actor']]]) {
    badReceipts << JsonOutput.toJson(receiptEnvelope + [owner: badOwner])
}
badReceipts << 'not-json'
def refused = badReceipts.collect { [inputResponse: approvedInput + [RECOVERY_RECEIPT: it]] } +
    [[inputResponse: approvedInput + [APPROVER: 'intruder']], [handoffTimeout: true], [handoffAbort: true],
     [trustedConfig: trustedGate + [omlxRecoveryApprover: 'intruder']], [promotionVersion: '1.1.25'],
     [finalizationStatus: 'FAILED'], [finalizationCommit: 'c' * 40], [finalizationVersion: '1.1.25'],
     [finalizationDigest: 'sha256:invalid'], [finalizationImage: 'fixture/unrelated-image'],
     [finalizationManifestDigest: 'sha256:' + 'a' * 64]]
for (options in refused) {
    def denied = simulate('autoReleasePipeline.groovy', target + [inputResponse: approvedInput] + options)
    assert denied.failed
    assert !denied.calls.contains('Authorize Deployment Handoff') && !denied.calls.contains(ownerJobCall)
    assert !denied.handoffEvents.contains('signed-request-archived') && denied.deploymentParameters.empty
    tests++
}

println "PASS: ${tests} offline Pipeline control-flow cases (not Jenkins CPS integration)"
