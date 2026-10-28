import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import java.time.Instant

// Plain Groovy control-flow fixtures. Jenkins, credentials, Python collectors,
// builds, shell, checkout, scans, services and product databases are mocked.
def root = new File(args ? args[0] : '.').canonicalFile
def product = 'shiba-go-ditch-api-project'
def folder = 'shiba-release-automation'
def revision = 'a' * 40
def config = [enabled: true, approvers: ['reviewer'], jenkinsApiUrl: 'http://jenkins.invalid',
    jenkinsReadCredentials: 'read', harborCredentials: 'harbor', harborApiUrl: 'http://harbor.invalid',
    scmCredentials: 'scm', mergeCredentials: 'merge', approvalKeyCredentials: 'approval',
    receiptKeyCredentials: 'receipt', stateDirectory: '/fixture/promotion', builderLabel: 'fixture-builder',
    deploymentJob: folder + '/' + product + '-prod-deploy', deploymentNodeLabel: 'fixture-owner',
    libraryRevision: revision, finalizationStateDirectory: '/fixture/finalization',
    finalizationWriterCredentials: 'writer', nexusCredentials: 'nexus', nexusBaseUrl: 'http://nexus.invalid']
def pins = ['owner.json': '1' * 64, 'registry.json': '2' * 64,
    'installation.json': '3' * 64, 'consumer-admission.json': '4' * 64]
def repairParams = [APP_BINDING_REPAIR_COORDINATOR_BUILD: '200', APP_BINDING_REPAIR_OWNER_BUILD: '92',
    APP_BINDING_REPAIR_PINS: JsonOutput.toJson(pins)]

def simulate = { Map options = [:] ->
    def calls = [], credentials = [], written = [:], archives = [], declared = []
    def collected = null
    def environment = [JOB_NAME: folder + '/' + product + '-auto-release', BUILD_NUMBER: '201']
    def supplied = repairParams + (options.params ?: [:])
    def causeList = options.containsKey('causes') ? options.causes :
        [[_class: 'hudson.model.Cause$UserIdCause', userId: 'reviewer']]
    def binding = new Binding()
    binding.setVariable('env', environment)
    binding.setVariable('params', supplied)
    binding.setVariable('currentBuild', [getBuildCauses: { String type = null ->
        type ? causeList.findAll { it._class == type } : causeList
    }])
    binding.setVariable('error', { String message -> throw new IllegalStateException(message) })
    ['disableConcurrentBuilds', 'pipelineTriggers', 'upstream'].each { name ->
        binding.setVariable(name, { Object... values -> [step: name] })
    }
    ['string', 'text', 'file', 'usernamePassword'].each { name ->
        binding.setVariable(name, { Map values -> [step: name] + values })
    }
    binding.setVariable('parameters', { List values -> declared.addAll(values); [step: 'parameters'] })
    binding.setVariable('properties', { Object values -> calls << 'properties' })
    binding.setVariable('node', { String label, Closure body ->
        assert label == config.builderLabel
        calls << 'node'; body()
    })
    binding.setVariable('dir', { String path, Closure body -> calls << 'dir'; body() })
    binding.setVariable('pwd', { -> '/fixture/workspace' })
    binding.setVariable('stage', { String name, Closure body -> calls << 'stage:' + name; body() })
    binding.setVariable('withEnv', { List values, Closure body ->
        def previous = new LinkedHashMap(environment)
        values.each { String value ->
            def at = value.indexOf('=')
            assert at > 0
            environment[value.substring(0, at)] = value.substring(at + 1)
        }
        try { body() } finally { environment.clear(); environment.putAll(previous) }
    })
    binding.setVariable('withCredentials', { List values, Closure body ->
        def ids = values.collect { it.credentialsId }
        assert ids.every { it in ['read', 'receipt'] }: 'Unrelated release credential entered maintenance'
        credentials << ids
        body()
    })
    binding.setVariable('libraryResource', { String name ->
        def path = new File(root, 'resources/' + name)
        assert path.isFile(): 'Missing real resource ' + name
        path.getText('UTF-8')
    })
    binding.setVariable('writeFile', { Map value -> written[value.file] = value.text })
    binding.setVariable('readFile', { String path ->
        assert written.containsKey(path): 'Collector output missing: ' + path
        written[path]
    })
    binding.setVariable('sh', { Object value ->
        def script = value instanceof Map ? value.script.toString() : value.toString()
        if (script.contains('release-evidence.py check-routing')) {
            assert script.contains('--library-revision "$RELEASE_LIBRARY_REVISION"')
            assert environment.RELEASE_LIBRARY_REVISION == revision
            calls << 'routing-read'
        } else if (script.contains('release-binding-repair.py collect')) {
            calls << 'collect'
            assert script.contains('--coordinator-build "$REPAIR_COORDINATOR"')
            assert script.contains('--owner-build "$REPAIR_OWNER"')
            assert script.contains('--current-build "$REPAIR_CURRENT_BUILD"')
            assert script.contains('--pins binding-repair-pins.json --output deployment-request.json')
            assert environment.REPAIR_COORDINATOR == supplied.APP_BINDING_REPAIR_COORDINATOR_BUILD
            assert environment.REPAIR_OWNER == supplied.APP_BINDING_REPAIR_OWNER_BUILD
            assert environment.REPAIR_CURRENT_BUILD == '201' && environment.REPAIR_AUTHOR == 'reviewer'
            assert written['binding-repair-pins.json'] == supplied.APP_BINDING_REPAIR_PINS
            def policy = new JsonSlurperClassic().parseText(written['control/policy.json'])
            assert policy.library_revision == revision && policy.approvers == ['reviewer']
            assert policy.max_exception_seconds == 900
            if (options.collectFailure) { throw new IllegalStateException('fixture collector rejected input') }
            // This synthetic collector result tests exact handoff transport;
            // Python tests own actual signature, provenance and TTL validation.
            collected = JsonOutput.toJson([payload: [schema_version: 1, kind: 'app-binding-repair',
                library_revision: revision, coordinator_build: 201,
                created_at: '2026-10-28T04:00:00Z', expires_at: '2026-10-28T04:15:00Z'],
                signature: 'offline-collector-fixture'])
            written['deployment-request.json'] = collected
        } else {
            throw new AssertionError('Unrelated shell command entered maintenance')
        }
    })
    binding.setVariable('build', { Map values ->
        calls << 'owner-build'
        assert values.job == config.deploymentJob && values.wait && values.propagate == false
        assert values.parameters.size() == 1 && values.parameters[0].name == 'SIGNED_RELEASE_REQUEST'
        assert values.parameters[0].value == collected && collected != null
        if (options.ownerException) { throw new IllegalStateException('fixture owner result unknown') }
        [number: 93, result: options.ownerResult ?: 'SUCCESS']
    })
    ['waitForBuild', 'checkout', 'input', 'finalizedImageRevalidation'].each { name ->
        binding.setVariable(name, { Object... values -> throw new AssertionError(name + ' entered maintenance') })
    }
    binding.setVariable('archiveArtifacts', { Map values -> archives << values; calls << 'archive' })
    binding.setVariable('deleteDir', { -> calls << 'deleteDir' })
    def failed = false
    try {
        new GroovyShell(binding).parse(new File(root, 'vars/autoReleasePipeline.groovy')).call(config)
    } catch (IllegalStateException expected) {
        failed = true
    }
    [calls: calls, credentials: credentials, written: written, archives: archives,
     declared: declared, collected: collected, failed: failed]
}

int tests = 0
def accepted = simulate()
assert !accepted.failed
assert accepted.declared.find { it.name == 'APP_BINDING_REPAIR_COORDINATOR_BUILD' }.step == 'string'
assert accepted.declared.find { it.name == 'APP_BINDING_REPAIR_OWNER_BUILD' }.step == 'string'
assert accepted.declared.find { it.name == 'APP_BINDING_REPAIR_PINS' }.step == 'text'
assert !accepted.declared.any { it.name == 'REVALIDATE_FINALIZED_IMAGE' }
assert accepted.calls.count('routing-read') == 1 && accepted.calls.count('collect') == 1
assert accepted.calls.count('owner-build') == 1
assert accepted.calls.indexOf('collect') < accepted.calls.indexOf('owner-build')
assert accepted.credentials == [['read'], ['read', 'receipt']]
def request = new JsonSlurperClassic().parseText(accepted.collected)
assert Instant.parse(request.payload.expires_at).epochSecond - Instant.parse(request.payload.created_at).epochSecond == 900
assert accepted.archives.any { it.artifacts.tokenize(',').containsAll(['binding-repair-pins.json', 'deployment-request.json']) }
assert accepted.calls[-1] == 'deleteDir'
tests++

def rejectedParams = [
    [SOURCE_BUILD: '362'], [EXPECTED_COMMIT: 'b' * 40], [REBUILD_PROD_COMMIT: 'b' * 40],
    [PUBLISHED_COORDINATOR_BUILD: '200'], [RECOVER_COORDINATOR_BUILD: '200'], [RECOVER_OWNER_BUILD: '92'],
    [REVALIDATE_FINALIZED_IMAGE: 'true'], [REVALIDATE_FINALIZED_IMAGE: 'false'],
    [RECOVER_COORDINATOR_BUILD: '200', RECOVER_OWNER_BUILD: '92', REVALIDATE_FINALIZED_IMAGE: 'true'],
    [APP_BINDING_REPAIR_COORDINATOR_BUILD: ''], [APP_BINDING_REPAIR_OWNER_BUILD: ''],
    [APP_BINDING_REPAIR_COORDINATOR_BUILD: '0'], [APP_BINDING_REPAIR_OWNER_BUILD: '-92'],
    [APP_BINDING_REPAIR_COORDINATOR_BUILD: '1000000000'],
    [APP_BINDING_REPAIR_PINS: ''], [APP_BINDING_REPAIR_PINS: 'x' * 4097]
]
for (values in rejectedParams) {
    def blocked = simulate(params: values)
    assert blocked.failed && !blocked.credentials
    assert !blocked.calls.contains('node') && !blocked.calls.contains('collect') && !blocked.calls.contains('owner-build')
    tests++
}
for (values in [
    [],
    [[_class: 'hudson.model.Cause$UserIdCause', userId: 'intruder']],
    [[_class: 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause', userId: 'reviewer']],
    [[_class: 'hudson.model.Cause$UpstreamCause', upstreamProject: product + '/develop', upstreamBuild: 362]],
    [[_class: 'hudson.model.Cause$UserIdCause', userId: 'reviewer'],
     [_class: 'hudson.model.Cause$UserIdCause', userId: 'reviewer']],
    [[_class: 'hudson.model.Cause$UserIdCause', userId: 'reviewer'],
     [_class: 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause', userId: 'reviewer']]
]) {
    def blocked = simulate(causes: values)
    assert blocked.failed && !blocked.credentials && !blocked.calls.contains('node')
    assert !blocked.calls.contains('collect') && !blocked.calls.contains('owner-build')
    tests++
}
// Malformed pin semantics are rejected by the real Python collector tests. This
// case verifies its nonzero result cannot fall through to an owner invocation.
for (values in [[collectFailure: true],
                [collectFailure: true, params: [APP_BINDING_REPAIR_PINS: '{invalid-json']]]) {
    def blocked = simulate(values)
    assert blocked.failed && blocked.calls.count('collect') == 1
    assert !blocked.calls.contains('owner-build')
    assert blocked.archives[-1].artifacts.tokenize(',').contains('binding-repair-pins.json')
    assert blocked.calls[-1] == 'deleteDir'
    tests++
}
for (values in [[ownerResult: 'FAILURE'], [ownerResult: 'UNSTABLE'], [ownerException: true]]) {
    def blocked = simulate(values)
    assert blocked.failed && blocked.calls.count('collect') == 1 && blocked.calls.count('owner-build') == 1
    assert blocked.archives[-1].artifacts.tokenize(',').containsAll(['binding-repair-pins.json', 'deployment-request.json'])
    assert blocked.calls[-1] == 'deleteDir'
    tests++
}

println "App binding repair coordinator: ${tests} offline flow contracts passed"
