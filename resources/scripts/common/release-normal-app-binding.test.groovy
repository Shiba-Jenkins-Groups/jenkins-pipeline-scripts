import groovy.json.JsonOutput

// Execute the real owner Pipeline with plain Groovy mocks. No Jenkins, shell,
// Docker, launchd, network, credentials or product database is accessed.
def root = new File(args ? args[0] : '.').canonicalFile
def product = 'shiba-go-ditch-api-project'
def coordinator = 'shiba-release-automation/' + product + '-auto-release'
def owner = 'shiba-release-automation/' + product + '-prod-deploy'
def revision = 'a' * 40
def commit = 'b' * 40
def config = [enabled: true, nodeLabel: 'fixture-owner', runtimeRoot: '/fixture/runtime',
    stateDirectory: '/fixture/state', dockerEngineId: 'fixture-engine',
    receiptKeyCredentials: 'fixture-receipt', scmCredentials: 'fixture-scm',
    libraryRevision: revision]

def simulate = { Map options = [:] ->
    def calls = [], written = [:], archives = [], resources = [], credentials = []
    def binding = new Binding()
    binding.setVariable('env', [JOB_NAME: owner, BUILD_NUMBER: '42'])
    binding.setVariable('params', [SIGNED_RELEASE_REQUEST: options.repair ?
        JsonOutput.toJson([payload: [kind: 'app-binding-repair']]) : '{"fixture":"signed-normal-deploy"}'])
    binding.setVariable('currentBuild', [getBuildCauses: { String type = null ->
        [[_class: 'org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause',
          upstreamProject: options.wrongCause ? 'untrusted/job' : coordinator, upstreamBuild: 200]]
    }])
    binding.setVariable('error', { String message -> throw new IllegalStateException(message) })
    ['disableConcurrentBuilds', 'text', 'string', 'parameters'].each { name ->
        binding.setVariable(name, { Object... values -> [step: name] })
    }
    binding.setVariable('properties', { Object value -> calls << 'properties' })
    ['node', 'dir', 'withEnv', 'withCredentials', 'timeout'].each { name ->
        binding.setVariable(name, { Object value, Closure body -> body() })
    }
    ['usernamePassword', 'file'].each { name ->
        binding.setVariable(name, { Map value -> [step: name] + value })
    }
    binding.setVariable('withCredentials', { List values, Closure body ->
        credentials << values.collect { it.credentialsId }
        body()
    })
    binding.setVariable('stage', { String name, Closure body -> calls << 'stage:' + name; body() })
    binding.setVariable('pwd', { -> '/fixture/workspace' })
    binding.setVariable('libraryResource', { String name ->
        resources << name
        if (name == options.missingResource) { throw new IllegalStateException('fixture missing resource') }
        def path = new File(root, 'resources/' + name)
        assert path.isFile(): 'Real library resource missing: ' + name
        path.getText('UTF-8')
    })
    binding.setVariable('writeFile', { Map value ->
        written[value.file] = value.text
        calls << 'write:' + value.file
    })
    binding.setVariable('archiveArtifacts', { Map value -> archives << value })
    binding.setVariable('checkout', { Map value ->
        assert value.branches == [[name: commit]]
        calls << 'checkout-exact-source'
    })
    binding.setVariable('readFile', { String path ->
        calls << 'read:' + path
        if (path == 'identity.json') {
            return JsonOutput.toJson([commit: commit, version: '1.1.17', digest: 'sha256:' + 'c' * 64,
                library_revision: options.wrongRevision ? 'd' * 40 : revision,
                coordinator_build: options.wrongCoordinator ? 199 : 200,
                kind: options.repair && !options.wrongVerifiedKind ? 'app-binding-repair' : 'deployment'])
        }
        if (path == 'runtime-receipt.json') {
            return JsonOutput.toJson([payload: [status: options.failedReceipt ? 'FAILED' : 'SUCCESS',
                commit: commit, version: '1.1.17', library_revision: revision,
                kind: options.repair ? 'app-binding-maintenance' : 'runtime-deployment',
                app_binding: [status: 'completed']]])
        }
        throw new IllegalStateException('unexpected fixture read: ' + path)
    })
    binding.setVariable('sh', { Object input ->
        def script = input instanceof Map ? input.script.toString() : input.toString()
        if (script.contains('release-binding-repair.py inspect')) {
            assert written['control/release-app-binding.py'] && written['control/release-binding-repair.py']
            calls << 'inspect-signed-repair'
        } else if (script.contains('release-binding-repair.py repair')) {
            assert calls.contains('inspect-signed-repair')
            assert !calls.contains('checkout-exact-source') && !calls.contains('normal-deploy')
            calls << 'native-binding-maintenance'
            if (options.deployFailure) { throw new IllegalStateException('fixture maintenance unknown') }
        } else if (script.contains('release-deploy.py inspect')) {
            assert written['control/release-app-binding.py']
            assert written['control/release-deploy.py']
            calls << 'inspect-signed-request'
        } else if (script.contains('release-deploy.py deploy')) {
            assert written['control/release-app-binding.py'] ==
                new File(root, 'resources/scripts/common/release-app-binding.py').getText('UTF-8')
            assert calls.contains('inspect-signed-request') && calls.contains('checkout-exact-source')
            calls << 'normal-deploy'
            if (options.deployFailure) { throw new IllegalStateException('fixture deployment unknown') }
        } else {
            throw new IllegalStateException('unexpected shell command in normal route')
        }
    })
    def failed = false
    try {
        new GroovyShell(binding).parse(new File(root, 'vars/prodDeploymentPipeline.groovy')).call(config)
    } catch (IllegalStateException expected) {
        failed = true
    }
    [calls: calls, written: written, resources: resources, archives: archives,
     credentials: credentials, failed: failed]
}

int tests = 0
def normal = simulate()
assert !normal.failed
assert normal.calls.count('normal-deploy') == 1
assert normal.calls.indexOf('write:control/release-app-binding.py') < normal.calls.indexOf('inspect-signed-request')
assert normal.calls.indexOf('inspect-signed-request') < normal.calls.indexOf('checkout-exact-source')
assert normal.calls.indexOf('checkout-exact-source') < normal.calls.indexOf('normal-deploy')
assert normal.calls.indexOf('normal-deploy') < normal.calls.indexOf('read:runtime-receipt.json')
assert normal.archives[-1].artifacts.tokenize(',').containsAll(
    ['request.json', 'identity.json', 'runtime-receipt.json', 'app-binding-failure.json'])
tests++

def missing = simulate(missingResource: 'scripts/common/release-app-binding.py')
assert missing.failed
assert !missing.calls.contains('inspect-signed-request')
assert !missing.calls.contains('checkout-exact-source') && !missing.calls.contains('normal-deploy')
assert missing.archives[-1].allowEmptyArchive
tests++

def unknown = simulate(deployFailure: true)
assert unknown.failed && unknown.calls.count('normal-deploy') == 1
assert !unknown.calls.contains('read:runtime-receipt.json')
assert unknown.archives[-1].artifacts.tokenize(',').contains('app-binding-failure.json')
tests++

def failed = simulate(failedReceipt: true)
assert failed.failed && failed.calls.count('normal-deploy') == 1
tests++

def untrusted = simulate(wrongCause: true)
assert untrusted.failed && !untrusted.resources && !untrusted.calls.contains('normal-deploy')
tests++

def mismatched = simulate(wrongRevision: true)
assert mismatched.failed && mismatched.calls.contains('inspect-signed-request')
assert !mismatched.calls.contains('checkout-exact-source') && !mismatched.calls.contains('normal-deploy')
tests++

def repair = simulate(repair: true)
assert !repair.failed && repair.calls.count('native-binding-maintenance') == 1
assert !repair.calls.contains('checkout-exact-source') && !repair.calls.contains('normal-deploy')
assert !repair.calls.contains('inspect-signed-request')
assert repair.credentials.flatten().every { it == 'fixture-receipt' }
tests++

def unverified = simulate(repair: true, wrongVerifiedKind: true)
assert unverified.failed && unverified.calls.contains('inspect-signed-repair')
assert !unverified.calls.contains('native-binding-maintenance')
assert !unverified.calls.contains('checkout-exact-source') && !unverified.calls.contains('normal-deploy')
tests++

def repairUnknown = simulate(repair: true, deployFailure: true)
assert repairUnknown.failed && repairUnknown.calls.count('native-binding-maintenance') == 1
assert !repairUnknown.calls.contains('read:runtime-receipt.json')
assert !repairUnknown.calls.contains('checkout-exact-source') && !repairUnknown.calls.contains('normal-deploy')
assert repairUnknown.archives[-1].artifacts.tokenize(',').contains('runtime-receipt.json')
tests++

def wrongCoordinator = simulate(repair: true, wrongCoordinator: true)
assert wrongCoordinator.failed && wrongCoordinator.calls.contains('inspect-signed-repair')
assert !wrongCoordinator.calls.contains('native-binding-maintenance')
assert !wrongCoordinator.calls.contains('checkout-exact-source') && !wrongCoordinator.calls.contains('normal-deploy')
tests++

println "Normal App binding owner route: ${tests} offline contracts passed"
