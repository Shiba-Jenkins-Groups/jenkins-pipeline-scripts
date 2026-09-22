import groovy.json.JsonOutput

// Plain Groovy mocks only: sh, checkout, build, node and credentials NEVER run.
def root = new File(args ? args[0] : '.').canonicalFile
def product = 'shiba-go-ditch-api-project'
def releaseFolder = 'shiba-release-automation'
def config = [rebuildDockerEngineId: '9f2f05b6-4637-45c7-ab13-cf5a35e2a539', enabled: true, approvers: ['reviewer'], jenkinsApiUrl: 'http://jenkins.invalid',
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


def params = [REBUILD_PROD_COMMIT: 'b' * 40, PUBLISHED_COORDINATOR_BUILD: '74']
def normal = simulate('autoReleasePipeline.groovy')
assert !normal.failed
assert normal.calls.count('build:' + product + '/prod') == 1
def rebuilt = simulate('autoReleasePipeline.groovy', [recoveryParams: params])
assert !rebuilt.failed
assert rebuilt.calls.count('build:' + product + '/prod') == 1
assert rebuilt.calls.count('build:' + releaseFolder + '/' + product + '-prod-deploy') == 1
assert !rebuilt.calls.contains('Promote Verified Commit')
assert !rebuilt.calls.contains('Finalize Revalidated PROD Candidate')
assert !rebuilt.calls.contains('develop: Complete Build Evidence')
for (options in [[badUser:true], [replay:true], [wrongProdSha:true], [prodResult:'FAILURE'], [review:'BLOCKED']]) {
    def blocked = simulate('autoReleasePipeline.groovy', [recoveryParams:params] + options)
    assert blocked.failed
    assert !blocked.calls.contains('build:' + releaseFolder + '/' + product + '-prod-deploy')
}
println 'PASS: 7 focused normal/rebuild authorization and failed-gate cases; no real commands executed'
