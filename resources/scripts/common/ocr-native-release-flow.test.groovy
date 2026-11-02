// Plain DSL flow and Jenkins CPS compilation; no Jenkins or native OCR execution.
def root = new File(args[0])
def source = new File(root, 'vars/ocrNativeReleasePipeline.groovy').text
def job = 'shiba-release-automation/apple-vision-native-release'
def valid = [enabled: true, libraryRevision: 'f' * 40, sourceCommit: 'd' * 40, sourceBaselineCommit: '8' * 40,
             ownerSourceCommit: 'e' * 40, ownerBaselineCommit: '9' * 40,
             ownerPackage: './cmd/service-control-owner', ownerTestRun: '^TestOCRReplacement', goVersion: 'go1.27.2',
             approvers: ['claude-code-use'], scannerTools: [gitleaks: [module: 'github.com/zricethezav/gitleaks/v8', version: 'v8.30.1'],
                 govulncheck: [path: '/private/scanner', sha256: '1' * 64]],
             parentCI: [schema: 'recognition-parent-ci-v1', status: 'SUCCESS', source_commit: 'a' * 40,
                 image: 'registry/recognition@sha256:' + 'b' * 64, job_name: 'shiba-go-ditch-recognition-project/prod',
                 build_number: 46, build_url: 'http://127.0.0.1:9090/job/shiba-go-ditch-recognition-project/job/prod/46/',
                 artifact_hash_inventory: ['.pipeline/release-manifest.env': 'c' * 64,
                     'reports/runtime-image.json': 'c' * 64, 'image-ref.txt': 'c' * 64]]]
def cause = [[_class: 'hudson.model.Cause$UserIdCause', userId: 'claude-code-use']]
def lastCalls = []
def runCase = { config, actualJob = job, actualCauses = cause, actualParams = [:] ->
    def calls = []
    lastCalls = calls
    def binding = new Binding([env: [JOB_NAME: actualJob, BUILD_NUMBER: '1', BUILD_URL: 'http://localhost:9090/job/shiba-release-automation/job/apple-vision-native-release/1/'],
                               currentBuild: [getBuildCauses: { actualCauses }], params: actualParams])
    binding.setVariable('error', { message -> throw new IllegalStateException(message) })
    ['properties', 'disableConcurrentBuilds', 'buildDiscarder', 'logRotator', 'pipelineTriggers', 'deleteDir', 'checkout', 'archiveArtifacts', 'writeFile', 'sh'].each { name ->
        binding.setVariable(name, { Object... values -> calls << [name, values.toList()]; [:] })
    }
    ['timeout', 'node', 'dir', 'stage'].each { name ->
        binding.setVariable(name, { value, Closure body -> calls << [name, value]; body() })
    }
    binding.setVariable('libraryResource', { path -> new File(root, 'resources/' + path).text })
    new GroovyShell(binding).parse(source).call(config)
    calls
}
def calls = runCase(valid)
assert calls.findAll { it[0] == 'node' } == [['node', 'shiba-prod-owner-macos']]
assert calls.findAll { it[0] == 'stage' }*.getAt(1) == ['Check macOS native builder', 'Checkout exact OCR and owner sources', 'Build native OCR and owner artifacts', 'Archive immutable native release']
assert calls.findAll { it[0] == 'checkout' }.size() == 2
assert calls.findAll { it[0] == 'sh' }.size() == 2
assert calls.find { it[0] == 'archiveArtifacts' }[1][0].allowEmptyArchive == false
assert !source.contains('withCredentials') && !source.contains('launchctl') && !source.contains('build(job:')
def encodedConfig = groovy.json.JsonOutput.toJson(valid).getBytes('UTF-8').encodeBase64().toString()
assert runCase(encodedConfig).findAll { it[0] == 'node' } == [['node', 'shiba-prod-owner-macos']]
['%', 'a', '====', '/w==', 'W10=', 'e30=\n', 'A' * 65540].each { bad ->
    try { runCase(bad); assert false : 'invalid encoded input passed' }
    catch (IllegalArgumentException expected) {}
    assert lastCalls.isEmpty() : 'invalid encoding reached a Pipeline side effect'
}
def rejected = [
    [valid + [enabled: false], job, cause, [:]],
    [valid + [sourceCommit: 'main'], job, cause, [:]],
    [valid + [ownerSourceCommit: 'develop'], job, cause, [:]],
    [valid + [ownerPackage: './cmd/../../other'], job, cause, [:]],
    [valid + [parentCI: valid.parentCI + [status: 'UNSTABLE']], job, cause, [:]],
    [valid, 'shiba-go-ditch-recognition-project/prod', cause, [:]],
    [valid, job, cause + [[_class: 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause']], [:]],
    [valid, job, [[_class: 'hudson.model.Cause$UserIdCause', userId: 'unknown']], [:]],
    [valid, job, cause, [SOURCE_COMMIT: 'main']],
]
rejected.each { argsForCase ->
    try { runCase(*argsForCase); assert false : 'invalid job/source/cause passed' }
    catch (IllegalStateException expected) {}
}
println 'PASS native Pipeline control flow: one valid, nine rejected exact-source/manual-job configurations'
println 'PASS trusted native entry: encoded configuration accepted; seven malformed/oversized inputs rejected before Pipeline effects'

// Compile the actual function with Jenkins CPS and sandbox installed jars.
def jars = args.length > 1 ? args[1] : null
if (jars) {
    def configuration = new org.codehaus.groovy.control.CompilerConfiguration()
    configuration.addCompilationCustomizers(new com.cloudbees.groovy.cps.CpsTransformer())
    def loader = new GroovyClassLoader(this.class.classLoader, configuration)
    loader.parseClass(source, 'ocrNativeReleasePipeline.groovy')
    println 'PASS actual native Pipeline source compiled with Jenkins CPS'
}
