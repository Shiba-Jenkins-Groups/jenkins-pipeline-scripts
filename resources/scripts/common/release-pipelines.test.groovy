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

def simulate = { String filename, Map options = [:] ->
    boolean deploy = filename == 'prodDeploymentPipeline.groovy'
    def calls = []
    def credentialGroups = []
    def binding = new Binding()
    binding.setVariable('env', [JOB_NAME: releaseFolder + '/' + product + (deploy ? '-prod-deploy' : '-auto-release'), BUILD_NUMBER: '1'])
    binding.setVariable('params', [SIGNED_RELEASE_REQUEST: '{}'] + (options.recoveryParams ?: [:]))
    binding.setVariable('currentBuild', [getBuildCauses: { String type = null ->
        if (options.recoveryParams) {
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
        [APPROVER: options.badApprover ? 'intruder' : 'reviewer', REASON: 'offline exact CVE review']
    })
    binding.setVariable('readFile', { String path ->
        if (path.endsWith('review.json')) { return JsonOutput.toJson([decision: options.review ?: 'PASS', findings: [fake: [:]]]) }
        if (path.endsWith('runtime-receipt.json')) { return JsonOutput.toJson([payload: [status: 'SUCCESS']]) }
        if (path.endsWith('promotion.json')) { return JsonOutput.toJson([payload: [merge_commit: 'b' * 40]]) }
        if (path.endsWith('identity.json')) {
            return JsonOutput.toJson([commit: options.wrongProdSha && path.contains('/prod/') ? 'c' * 40 : 'b' * 40,
                                      version: '1.0.32', digest: 'sha256:' + 'd' * 64])
        }
        return '{}'
    })
    def failed = false
    try {
        def pipeline = new GroovyShell(binding).parse(new File(root, 'vars/' + filename))
        pipeline.call(config + (options.disabled ? [enabled: false] : [:]))
    } catch (IllegalStateException expected) {
        failed = true
    }
    [calls: calls, credentialGroups: credentialGroups, failed: failed]
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
for (options in [[:], [prodResult: 'FAILURE'], [review: 'BLOCKED'], [failStage: 'develop: All Severity Scans']]) {
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
assert result.calls.indexOf('prod: Release Gate') < result.calls.indexOf('Finalize Revalidated PROD Candidate')
assert result.calls.indexOf('Finalize Revalidated PROD Candidate') < result.calls.indexOf('Authorize Deployment Handoff')
tests++
for (options in [[disabled: true], [wrongCause: true], [upstreamResult: 'FAILURE'],
                 [failStage: 'develop: All Severity Scans'], [review: 'BLOCKED'],
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
assert !result.failed && result.calls.count('input') == 2 // Gate A approval never covers Gate B.
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
