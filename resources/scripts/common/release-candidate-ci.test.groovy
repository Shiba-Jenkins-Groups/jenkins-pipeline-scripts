// Offline Declarative-shaped mock. No sh/checkout/publish/credentials actually run.
def libraryRoot = new File(args ? args[0] : '.').canonicalFile
def simulate = { boolean enabled, Map options = [:] ->
    def calls = [], stack = []
    def branch = (options.branch ?: 'prod').toString()
    def environment = [JOB_NAME: options.job ?: "shiba-go-ditch-api-project/${branch}".toString(), BRANCH_NAME: branch, BUILD_NUMBER: '1',
                       BUILD_URL: 'http://offline/build/1/', GIT_COMMIT: 'b' * 40, CHANGE_ID: null]
    def current = [currentResult: 'SUCCESS', durationString: 'offline']
    def binding = new Binding([env: environment, currentBuild: current, scm: [:]])
    ['pipeline', 'stages', 'script', 'post', 'always', 'failure', 'unstable', 'success'].each { name ->
        binding.setVariable(name, { Object argument ->
            if (argument instanceof Closure) { argument() }
            else { calls << 'unstable'; current.currentResult = 'UNSTABLE' }
        })
    }
    binding.setVariable('stage', { String name, Closure body ->
        stack << [name: name, skip: false]
        body()
        stack.remove(stack.size() - 1)
    })
    binding.setVariable('steps', { Closure body -> if (!stack.any { it.skip }) { body() } })
    ['when', 'allOf'].each { name -> binding.setVariable(name, { Closure body -> body() }) }
    binding.setVariable('expression', { Closure condition -> if (!condition()) { stack[-1].skip = true } })
    ['agent', 'options'].each { name -> binding.setVariable(name, { Closure body -> }) }
    binding.setVariable('echo', { Object text -> })
    binding.setVariable('error', { String reason -> throw new IllegalStateException(reason) })
    binding.setVariable('checkout', { Object value -> [GIT_BRANCH: "origin/${branch}", GIT_COMMIT: 'b' * 40] })
    binding.setVariable('libraryResource', { String path -> 'offline resource' })
    binding.setVariable('writeFile', { Map value -> })
    binding.setVariable('archiveArtifacts', { Map value -> calls << 'archive:' + value.artifacts })
    binding.setVariable('junit', { Map value -> calls << 'junit' })
    binding.setVariable('publishHTML', { Map value -> })
    binding.setVariable('fileExists', { String path -> path == 'image-ref.txt' })
    binding.setVariable('readFile', { String path -> 'IMAGE_REF=localhost:9290/shiba-go-ditch-api-project/prod/1.0.32:1\nIMAGE_DIGEST=sha256:' + 'c' * 64 })
    binding.setVariable('cleanWs', { -> calls << 'clean' })
    ['withCredentials', 'withEnv', 'timeout', 'catchError'].each { name ->
        binding.setVariable(name, { Object value, Closure body -> body() })
    }
    binding.setVariable('file', { Map value ->
        calls << "credential-file:${value.credentialsId}:${value.variable}"
        value
    })
    ['usernamePassword', 'string'].each { name -> binding.setVariable(name, { Map value -> value }) }
    binding.setVariable('sh', { Object value ->
        def command = value instanceof Map ? value.script.toString() : value.toString()
        calls << command
        if (command.contains('detect.sh &&')) {
            return "LANGUAGE=go\nBUILD_TOOL=go\nPOLICY_NAME=${branch}\nDO_PROD_DEPLOY=${branch == 'prod'}\nDO_ARTIFACT_PUBLISH=true\nDO_GIT_TAG=true\nDO_PACKAGE=true\nDO_DOCKER_BUILD=true\nDO_SCAN=true\nDO_PUSH=true\nDO_DEPLOY=true\nDO_K3S_VERIFY=true\nDEPLOY_NAMESPACE=${branch == 'prod' ? 'prod' : 'dev'}\nPIPELINE_TRUST=trusted\nDO_SECRET_SCAN=true"
        }
        if (options.buildFailure && command.contains('/go-build.sh')) { throw new IllegalStateException('Build is not waivable') }
        if (command.contains("--stage 'Test'")) { return options.testResult ?: 0 }
        return 0
    })
    def failed = false
    def failureReason = ''
    try {
        new GroovyShell(binding).parse(new File(libraryRoot, 'vars/ciPipeline.groovy')).call([
            controlledReleaseCandidate: enabled, githubCredentials: 'offline-read', harborCredentials: 'offline-harbor',
            releaseFinalizeAfterVerification: true, fastContractCommand: "echo 'offline contract'", deployInputGate: false,
            dockerPruneEnabled: false, ciCapacityBuilder: options.ciBuilder ?: false,
            developLeanFlow: options.lean ?: false,
            profile: options.profile ?: 'full',
            runtimeVerification: options.compose ? 'container' : null, scanAuthority: options.compose && enabled ? 'coordinator' : null
        ])
    } catch (IllegalStateException expected) {
        failed = true
        failureReason = expected.message ?: expected.class.name
    }
    [calls: calls, failed: failed, failureReason: failureReason, environment: environment, result: current.currentResult]
}
def original = simulate(false)
assert !original.failed
assert original.calls.any { it == 'bash .pipeline/scripts/common/release-finalize.sh' }
assert original.calls.contains('junit')
def candidate = simulate(true)
assert !candidate.failed
assert !candidate.calls.any { it == 'bash .pipeline/scripts/common/release-finalize.sh' }
assert !candidate.calls.contains('junit') // Raw reports archived; no post-stage misattribution.
assert candidate.environment.DO_ARCHIVE_ARTIFACT_PUBLISH == 'false'
assert candidate.environment.DO_ARCHIVE_GIT_TAG == 'false'
assert candidate.calls.any { it.contains('release-candidate.py manifest') }
def capacity = simulate(true, [ciBuilder: true])
assert !capacity.failed
assert capacity.calls.any { it.contains('ci-capacity.py --builder') }
assert capacity.calls.any { it.contains('ci-builder-ensure.sh') }
assert capacity.calls.any { it == 'credential-file:k3s-kubeconfig:KUBECONFIG' }
assert capacity.calls.indexOf(capacity.calls.find { it.contains('ci-capacity.py --builder') }) <
       capacity.calls.indexOf(capacity.calls.find { it.contains('/go-build.sh') })
assert capacity.environment.CI_BUILDX_BUILDER == 'shiba-app-ci'
assert candidate.environment.CI_BUILDX_BUILDER == null
def lean = simulate(true, [branch: 'develop', lean: true, ciBuilder: true])
assert !lean.failed
assert !lean.calls.any { it.contains('ci-capacity.py --builder') }
assert !lean.calls.any { it.contains('release-candidate.py') }
assert !lean.calls.any { it.contains('dependency-check.sh') }
assert !lean.calls.any { it == 'bash .pipeline/scripts/cd.sh docker-build' }
assert !lean.calls.any { it == 'bash .pipeline/scripts/cd.sh deploy' }
assert lean.calls.any { it.contains('/go-build.sh') }
assert lean.calls.any { it.contains('/go-test.sh') }
assert lean.environment.DO_ARTIFACT_PUBLISH == 'false'
assert lean.environment.DO_K3S_VERIFY == 'false'
candidate = simulate(true, [testResult: 10])
assert !candidate.failed && candidate.result == 'UNSTABLE'
assert candidate.calls.contains('bash .pipeline/scripts/cd.sh harbor-push')
assert candidate.calls.contains('bash .pipeline/scripts/cd.sh deploy') // ephemeral k3d, not runtime
for (options in [[testResult: 2], [buildFailure: true], [profile: 'ci-only']]) {
    def result = simulate(true, options)
    assert result.failed
    assert !result.calls.any { it == 'bash .pipeline/scripts/common/release-finalize.sh' }
}
println 'PASS: 8 offline candidate/lean-develop/legacy CI flow cases (not Jenkins CPS integration)'

def compose = simulate(true, [compose: true])
assert !compose.failed
assert compose.calls.contains('python3 .pipeline/scripts/common/runtime-image-verify.py')
assert !compose.calls.any { it.contains('cd.sh deploy') || it.contains('smoke-test.sh') || it.contains("--stage 'Dependency Scan'") || it.contains("--stage 'Image Scan'") || it.contains("--stage 'Harbor Vulnerability Report'") }
assert compose.environment.RELEASE_CANDIDATE_MODE == 'controlled-compose-v2'
assert compose.environment.DO_K3S_VERIFY == 'false'
def recognition = simulate(false, [compose: true, job: 'shiba-go-ditch-recognition-project/prod'])
assert !recognition.failed
assert recognition.calls.contains('python3 .pipeline/scripts/common/runtime-image-verify.py')
assert recognition.calls.contains('bash .pipeline/scripts/common/release-finalize.sh')
assert recognition.calls.any { it.contains('cd.sh image-scan') }
assert !recognition.calls.any { it.contains('cd.sh deploy') || it.contains('input') }
assert simulate(true, [compose: true, job: 'unrelated/prod']).failed
println 'PASS: Compose App/Recognition flows and cross-product denial'
