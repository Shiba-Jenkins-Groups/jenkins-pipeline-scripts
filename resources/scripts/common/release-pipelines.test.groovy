import groovy.json.JsonOutput

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
def ocrSource = '40dc79be8cb72a9480b718663294d97a7487388c'
def ocrRepository = "localhost:9290/${product}/prod/1.1.38"

def simulate = { String filename, Map options = [:] ->
    boolean deploy = filename == 'prodDeploymentPipeline.groovy'
    def calls = []
    def credentialGroups = []
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
            if (name == 'writeFile') { writtenFiles[value.file] = value.text }
            if (name == 'archiveArtifacts' && options.archiveFailure) { throw new IllegalStateException('archive unavailable') }
        })
    }
    binding.setVariable('deleteDir', { -> calls << 'deleteDir' })
    ['disableConcurrentBuilds', 'pipelineTriggers', 'upstream', 'usernamePassword', 'file', 'text', 'string', 'parameters'].each { name ->
        binding.setVariable(name, { Object... value -> [step: name] })
    }
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
    binding.setVariable('fileExists', { String path -> path == 'develop/source/VERSION' && options.ocrVersionMissing != true })
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
        [number: 103, result: options.prodResult ?: 'SUCCESS']
    })
    binding.setVariable('input', { Map values ->
        calls << 'input'
        if (values.id == 'ocr-owner-138-ready') {
            assert values.submitter == 'reviewer'
            if (options.ocrInputClosed) { throw new IllegalStateException('fake input closed') }
            return [APPROVER: options.badApprover ? 'intruder' : 'reviewer', OWNER_RECEIPT: options.ocrReceipt]
        }
        [APPROVER: options.badApprover ? 'intruder' : 'reviewer', REASON: 'offline exact CVE review']
    })
    binding.setVariable('readFile', { String path ->
        if (path == 'develop/source/VERSION') { return (options.ocrDevelopVersion ?: '1.0.32') + '\n' }
        if (path.endsWith('source-status.json')) { return JsonOutput.toJson([
            decision: options.sourceStatus ?: 'NEW', source_commit: 'b' * 40, merge_commit: 'c' * 40]) }
        if (path.endsWith('review.json')) { return JsonOutput.toJson([decision: options.review ?: 'PASS', findings: [fake: [:]]]) }
        if (path.endsWith('runtime-receipt.json')) { return JsonOutput.toJson([payload: [status: 'SUCCESS']]) }
        if (path.endsWith('promotion.json')) {
            return JsonOutput.toJson([payload: [merge_commit: 'b' * 40, source_commit: options.ocrPromotionSource ?: options.ocrDevelopSource,
                                                version: options.ocrDevelopVersion]])
        }
        if (path == 'finalization.json' && options.ocrDevelopVersion) {
            def digest = 'sha256:' + 'd' * 64
            return JsonOutput.toJson([payload: [
                kind: 'finalization', status: 'SUCCESS', product: product, version: '1.1.38', commit: 'b' * 40, digest: digest,
                manifest: [RELEASE_TAG: 'v1.1.38', GIT_COMMIT: 'b' * 40, IMAGE_DIGEST: digest,
                           IMAGE_REF: "${ocrRepository}:${options.ocrManifestBuild ?: 103}".toString()]]])
        }
        if (path.endsWith('identity.json')) {
            boolean develop = path.endsWith('/develop/identity.json')
            def commit = options.wrongProdSha && path.contains('/prod/') ? 'c' * 40 : 'b' * 40
            if (develop) {
                return JsonOutput.toJson([commit: options.ocrDevelopSource ?: commit, branch: 'develop', build: 188,
                                          library_revision: config.libraryRevision])
            }
            return JsonOutput.toJson([commit: commit, version: '1.0.32', digest: 'sha256:' + 'd' * 64,
                                      library_revision: config.libraryRevision])
        }
        return '{}'
    })
    String failure
    try {
        def pipeline = new GroovyShell(binding).parse(new File(root, 'vars/' + filename))
        pipeline.call(config + (options.disabled ? [enabled: false] : [:]) + (options.configOverrides ?: [:]))
    } catch (IllegalStateException expected) {
        failure = expected.message
    }
    [calls: calls, credentialGroups: credentialGroups, failed: failure != null, failure: failure, writtenFiles: writtenFiles]
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
def ocrStatus = { name, revision ->
    [id: name, environment: 'prod', registry_revision: revision, desired_state: 'running',
     process_state: 'running', contract_health: 'ready']
}
def ocrDigest = 'sha256:' + 'd' * 64
def ocrPins = [schema: 'ocr-owner-138-handoff/v1', version: '1.1.38', source_commit: ocrSource, commit: 'b' * 40,
    digest: ocrDigest, coordinator_build: 1, prod_build: 103, image: "${ocrRepository}@${ocrDigest}".toString()]
def ocrOwner = [source_commit: 'b' * 40, version: '1.1.38', manifest_sha256: 'a' * 64, activated: true,
    ocrsvc: ocrStatus('ocrsvc', 3), recognition: ocrStatus('recognition', 5)]
def ocrOptions = [ocrDevelopSource: ocrSource, ocrDevelopVersion: '1.1.38',
    configOverrides: [ocrOwner138Approver: 'reviewer'], ocrReceipt: JsonOutput.toJson(ocrPins + [owner: ocrOwner])]
def ocrHandoff = simulate('autoReleasePipeline.groovy', ocrOptions)
assert !ocrHandoff.failed : ocrHandoff.failure
assert ocrHandoff.calls.indexOf('Finalize Revalidated PROD Candidate') < ocrHandoff.calls.indexOf('Recover OCR Owner Before Deployment')
assert ocrHandoff.calls.indexOf('Recover OCR Owner Before Deployment') < ocrHandoff.calls.indexOf('Authorize Deployment Handoff')
assert ocrHandoff.writtenFiles['ocr-owner-138-handoff.json'] == JsonOutput.toJson(ocrPins)
assert ocrHandoff.writtenFiles.containsKey('ocr-owner-138-receipt.json')
assert ocrHandoff.calls.count('build:' + config.deploymentJob) == 1
tests++
for (overrides in [[configOverrides: [:]], [configOverrides: [ocrOwner138Approver: 'intruder']],
                   [ocrDevelopSource: 'c' * 40], [ocrDevelopVersion: '1.1.39'], [ocrVersionMissing: true]]) {
    def rejected = simulate('autoReleasePipeline.groovy', ocrOptions + overrides)
    assert rejected.failed
    assert !rejected.calls.contains('Promote Verified Commit')
    assert !rejected.calls.contains('input')
    tests++
}
def ocrBadReports = [ocrPins + [prod_build: 999, owner: ocrOwner], ocrPins + [digest: 'sha256:' + 'e' * 64, owner: ocrOwner],
    ocrPins + [owner: ocrOwner + [source_commit: ocrSource]],
    ocrPins + [owner: ocrOwner + [manifest_sha256: 'missing']],
    ocrPins + [owner: ocrOwner + [activated: false]],
    ocrPins + [owner: ocrOwner + [ocrsvc: ocrStatus('ocrsvc', 2)]],
    ocrPins + [owner: ocrOwner + [recognition: ocrStatus('recognition', 5) + [contract_health: 'unavailable']]]]
for (overrides in ocrBadReports.collect { [ocrReceipt: JsonOutput.toJson(it)] } +
     [[badApprover: true], [ocrInputClosed: true], [ocrPromotionSource: 'c' * 40], [ocrManifestBuild: 999]]) {
    def rejected = simulate('autoReleasePipeline.groovy', ocrOptions + overrides)
    assert rejected.failed
    assert !rejected.writtenFiles.containsKey('ocr-owner-138-receipt.json')
    assert !rejected.calls.contains('Authorize Deployment Handoff')
    assert !rejected.calls.contains('build:' + config.deploymentJob)
    tests++
}
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
println "PASS: ${tests} offline Pipeline control-flow cases (not Jenkins CPS integration)"
