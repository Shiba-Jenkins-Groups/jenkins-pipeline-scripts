import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// This fixture executes both real Jenkins Shared Library scripts in plain
// Groovy. Every Jenkins step, credential, shell, and artifact is synthetic.
def root = new File(args ? args[0] : '.').canonicalFile
def product = 'shiba-go-ditch-api-project'
def folder = 'shiba-release-automation'
def coordinatorJob = folder + '/' + product + '-auto-release'
def ownerJob = folder + '/' + product + '-prod-deploy'
def kind = 'app-binding-105-committed-continuation/v1'
def revision = 'a' * 40
def driver = 'b' * 64
def userCause = [_class: 'hudson.model.Cause$UserIdCause', userId: 'incident-owner']
def ownerCause = [_class: 'org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause',
                  upstreamProject: coordinatorJob, upstreamBuild: 239]
def review = JsonOutput.toJson([payload: [kind: kind + '/observe/receipt', status: 'OBSERVED'], signature: 'fixture'])
def history = JsonOutput.toJson([failed: 'fixture-105-archive', original: 'fixture-103-archive'])
def configured = [enabled: true, approvers: ['other-reviewer', 'incident-owner'], jenkinsApiUrl: 'http://jenkins.invalid',
    jenkinsReadCredentials: 'jenkins-read', harborCredentials: 'harbor', harborApiUrl: 'http://harbor.invalid',
    scmCredentials: 'scm', mergeCredentials: 'merge', approvalKeyCredentials: 'approval',
    receiptKeyCredentials: 'receipt', stateDirectory: '/fixture/state', builderLabel: 'fixture-builder',
    deploymentJob: ownerJob, deploymentNodeLabel: 'fixture-owner', nodeLabel: 'fixture-owner',
    runtimeRoot: '/fixture/prod', dockerEngineId: 'fixture-engine', libraryRevision: revision,
    finalizationStateDirectory: '/fixture/finalization', finalizationWriterCredentials: 'writer',
    nexusCredentials: 'nexus', nexusBaseUrl: 'http://nexus.invalid',
    binding105Enabled: true, binding105DriverSha256: driver, binding105Approver: 'incident-owner']
def readJson = { String input -> new JsonSlurperClassic().parseText(input) }

def simulate = { String jobType, Map options = [:] ->
    def isOwner = jobType == 'owner'
    def mode = options.mode ?: 'observe'
    def requestKind = kind + (mode == 'observe' ? '/observe' : '')
    def grant = JsonOutput.toJson([payload: [kind: requestKind], signature: 'fixture-verified-by-python'])
    def defaultParams = isOwner ? [SIGNED_RELEASE_REQUEST: grant, APP_BINDING_105_HISTORY: history] :
        (mode == 'observe' ? [APP_BINDING_105_OBSERVE: '105'] : [APP_BINDING_105_REVIEW: review])
    def params = defaultParams + (options.params ?: [:])
    def causes = options.containsKey('causes') ? options.causes : [isOwner ? ownerCause : userCause]
    def config = configured + (options.config ?: [:])
    def env = [JOB_NAME: isOwner ? ownerJob : coordinatorJob, BUILD_NUMBER: isOwner ? '106' : '239']
    def events = [], files = [:], archives = [], reads = [], declared = []
    def build = [description: null, getBuildCauses: { String type = null ->
        type ? causes.findAll { it._class == type } : causes
    }]
    def binding = new Binding([env: env, params: params, currentBuild: build])

    ['disableConcurrentBuilds', 'pipelineTriggers', 'upstream'].each { name ->
        binding.setVariable(name, { Object... values -> [step: name] })
    }
    ['string', 'text', 'file', 'usernamePassword'].each { name ->
        binding.setVariable(name, { Map values -> [step: name] + values })
    }
    binding.setVariable('parameters', { List values -> declared.addAll(values); [step: 'parameters'] })
    binding.setVariable('properties', { Object values -> })
    binding.setVariable('node', { String label, Closure body ->
        assert label == (isOwner ? config.nodeLabel : config.builderLabel)
        events << 'node'
        body()
    })
    binding.setVariable('timeout', { Map optionsIgnored, Closure body -> body() })
    binding.setVariable('dir', { String path, Closure body -> body() })
    binding.setVariable('stage', { String name, Closure body -> body() })
    binding.setVariable('withEnv', { List pairs, Closure body ->
        def previous = new LinkedHashMap(env)
        pairs.each { String item ->
            def at = item.indexOf('=')
            env[item.substring(0, at)] = item.substring(at + 1)
        }
        try {
            body()
        } finally {
            env.clear()
            env.putAll(previous)
        }
    })
    binding.setVariable('withCredentials', { List credentials, Closure body ->
        def ids = credentials.collect { it.credentialsId }
        assert ids.every { it in (isOwner ? ['receipt'] : ['jenkins-read', 'receipt']) }
        events << 'credentials'
        body()
    })
    binding.setVariable('libraryResource', { String name -> reads << name; 'synthetic-library-resource' })
    binding.setVariable('pwd', { -> '/fixture/workspace' })
    binding.setVariable('writeFile', { Map args -> files[args.file] = args.text })
    binding.setVariable('readFile', { String path -> assert files.containsKey(path); files[path] })
    binding.setVariable('error', { String message -> throw new IllegalStateException(message) })
    binding.setVariable('archiveArtifacts', { Map args -> archives << args.artifacts.tokenize(',') })
    binding.setVariable('deleteDir', { -> events << 'delete' })
    binding.setVariable('build', { Map args ->
        assert !isOwner && args.job == ownerJob && args.wait && args.propagate == false
        assert args.parameters.collectEntries { [(it.name): it.value] } == [
            SIGNED_RELEASE_REQUEST: grant, APP_BINDING_105_HISTORY: history
        ]
        events << 'build-owner'
        [number: 106, result: options.ownerResult ?: 'SUCCESS']
    })
    ['checkout', 'waitForBuild', 'input'].each { name ->
        binding.setVariable(name, { Object... args -> throw new AssertionError("Unexpected ordinary release: " + name) })
    }
    binding.setVariable('sh', { String command ->
        if (command.contains('release-evidence.py check-routing')) {
            assert !isOwner && env.RELEASE_LIBRARY_REVISION == revision
            events << 'routing-check'
            return
        }
        assert command.contains('release-app-binding-committed-job.py')
        assert env.COMMIT_LIBRARY == revision && env.COMMIT_DRIVER_SHA == driver
        assert command.contains('--driver-sha256 "$COMMIT_DRIVER_SHA"')
        if (command.contains('.py collect ')) {
            assert !isOwner && env.COMMIT_AUTHOR == 'incident-owner'
            assert command.contains('--job "$JOB_NAME" --current-build "$BUILD_NUMBER"')
            assert readJson(files['control/policy.json']).approvers == ['incident-owner']
            assert files['binding-105-review.json'] == (mode == 'observe' ? '{"intent":"observe"}' : review)
            events << 'collect'
            files['deployment-request.json'] = grant
            files['binding-105-history.json'] = history
        } else if (command.contains('.py verify-owner ')) {
            assert !isOwner && env.COMMIT_OWNER_BUILD == '106'
            assert command.contains('--coordinator-build "$BUILD_NUMBER" --owner-build "$COMMIT_OWNER_BUILD"')
            events << 'verify-owner'
            if (options.readbackFailure) { throw new IllegalStateException('archived owner receipt failed verification') }
            files['binding-105-owner-receipt.json'] = JsonOutput.toJson([payload: [status: 'fixture'], signature: 'fixture'])
        } else if (command.contains('.py inspect ')) {
            assert isOwner && env.COMMIT_UPSTREAM == '239'
            assert command.contains('--upstream-build "$COMMIT_UPSTREAM"')
            assert files['binding-105-history.json'] == history
            assert readJson(files['control/binding-105-approvers.json']) == ['incident-owner']
            events << 'inspect'
            if (options.inspectFailure) { throw new IllegalStateException('unauthenticated grant') }
            files['identity.json'] = JsonOutput.toJson([kind: requestKind, coordinator_build: options.verifiedUpstream ?: 239,
                library_revision: revision, driver_sha256: options.verifiedDriver ?: driver])
        } else if (command.contains('.py finish ')) {
            assert isOwner && env.COMMIT_UPSTREAM_JOB == coordinatorJob && env.BUILD_NUMBER == '106'
            assert command.contains('--job "$JOB_NAME" --owner-build "$BUILD_NUMBER" --upstream-job "$COMMIT_UPSTREAM_JOB"')
            assert command.contains('--runtime-root "$RELEASE_RUNTIME_ROOT" --state-directory "$RELEASE_STATE"')
            events << 'finish'
            if (options.finishFailure) { throw new IllegalStateException('managed owner result unconfirmed') }
            files['runtime-receipt.json'] = JsonOutput.toJson([payload: [status: mode == 'observe' ? 'OBSERVED' : 'SUCCESS']])
        } else if (command.contains('.py verify-result ')) {
            assert isOwner && files['runtime-receipt.json']
            assert command.contains('--owner-build "$BUILD_NUMBER" --upstream-build "$COMMIT_UPSTREAM"')
            events << 'verify-result'
            if (options.readbackFailure) { throw new IllegalStateException('bad runtime receipt') }
        } else {
            throw new AssertionError('Unrelated shell command entered #105')
        }
    })
    Boolean failed = false
    try {
        def source = isOwner ? 'vars/prodDeploymentPipeline.groovy' : 'vars/autoReleasePipeline.groovy'
        new GroovyShell(binding).parse(new File(root, source)).call(config)
    } catch (IllegalStateException expected) {
        failed = true
    }
    [events: events, files: files, archives: archives, reads: reads, declared: declared, failed: failed, description: build.description]
}

def tests = 0
for (mode in ['observe', 'finalize']) {
    def coordinator = simulate('coordinator', [mode: mode])
    assert !coordinator.failed && coordinator.description.endsWith('owner #106')
    assert coordinator.events.findAll { it in ['routing-check', 'collect', 'build-owner', 'verify-owner'] } ==
        ['routing-check', 'collect', 'build-owner', 'verify-owner']
    assert coordinator.declared.find { it.name == 'APP_BINDING_105_OBSERVE' }.step == 'string'
    assert coordinator.declared.find { it.name == 'APP_BINDING_105_REVIEW' }.step == 'text'
    assert coordinator.archives.any { it.contains('binding-105-owner-receipt.json') }
    def owner = simulate('owner', [mode: mode])
    assert !owner.failed && owner.description
    assert owner.events.findAll { it in ['inspect', 'finish', 'verify-result'] } == ['inspect', 'finish', 'verify-result']
    assert owner.archives.last().containsAll(['binding-105-history.json', 'runtime-receipt.json'])
    tests += 2
}
for (input in [[config: [binding105Enabled: false]], [config: [binding105Approver: 'not-in-policy']],
               [params: [APP_BINDING_105_OBSERVE: '106']], [params: [APP_BINDING_105_REVIEW: review]],
               [params: [SOURCE_BUILD: '392']], [params: [APP_BINDING_REPAIR_OWNER_BUILD: '101']],
               [causes: [[_class: 'hudson.model.Cause$UpstreamCause', upstreamProject: product + '/develop']]],
               [causes: [[_class: 'hudson.model.Cause$UserIdCause', userId: 'other-reviewer']]]]) {
    def result = simulate('coordinator', input)
    assert result.failed && !result.events && !result.description
    tests++
}
for (input in [[config: [binding105Enabled: false]], [config: [binding105DriverSha256: '']],
               [params: [APP_BINDING_105_HISTORY: '']], [params: [APP_BINDING_105_HISTORY: 'a' * 65537]],
               [causes: [ownerCause + [_class: 'fixture.ForgedCause']]], [causes: [ownerCause, userCause]],
               [params: [SIGNED_RELEASE_REQUEST: '{"payload":{"kind":"prod-deployment"}}']]]) {
    def result = simulate('owner', input)
    assert result.failed && !result.events && !result.description
    tests++
}
for (input in [[inspectFailure: true], [verifiedUpstream: 240], [verifiedDriver: 'c' * 64]]) {
    def result = simulate('owner', input)
    assert result.failed && result.events.contains('inspect') && !result.events.contains('finish') && !result.description
    tests++
}
def failedOwner = simulate('coordinator', [ownerResult: 'FAILURE'])
assert failedOwner.failed && failedOwner.events.contains('build-owner') && !failedOwner.events.contains('verify-owner')
assert failedOwner.archives.last().contains('binding-105-history.json') && !failedOwner.description
def failedReadback = simulate('coordinator', [readbackFailure: true])
assert failedReadback.failed && failedReadback.events.contains('verify-owner') && !failedReadback.description
def failedNative = simulate('owner', [finishFailure: true])
assert failedNative.failed && failedNative.events.contains('finish') && !failedNative.events.contains('verify-result') && !failedNative.description
def failedNativeReceipt = simulate('owner', [readbackFailure: true])
assert failedNativeReceipt.failed && failedNativeReceipt.events.contains('verify-result') && !failedNativeReceipt.description
tests += 4

println "Exact #105 coordinator/owner routing: ${tests} offline contracts passed"
