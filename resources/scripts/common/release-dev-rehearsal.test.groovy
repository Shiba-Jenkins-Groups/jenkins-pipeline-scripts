def source = new File(args[0], 'vars/releaseDevRehearsalPipeline.groovy').text
def runCase = { config, job ->
    def calls = []
    def binding = new Binding([env: [JOB_NAME: job, BUILD_NUMBER: '1'], currentBuild: [:]])
    binding.setVariable('error', { message -> throw new IllegalStateException(message) })
    ['properties','disableConcurrentBuilds','buildDiscarder','logRotator','writeFile','archiveArtifacts'].each { name ->
        binding.setVariable(name, { Object... values -> calls << [name, values.toList()]; [:] })
    }
    ['timeout','node','dir','stage'].each { name ->
        binding.setVariable(name, { value, Closure body -> calls << [name, value]; body() })
    }
    binding.setVariable('sh', { script -> calls << ['sh', script] })
    binding.setVariable('libraryResource', { path -> new File(args[0], 'resources/' + path).text })
    def pipeline = new GroovyShell(binding).parse(source)
    pipeline.call(config)
    calls
}
def job = 'shiba-go-ditch-api-project-dev-contract-rehearsal'
def valid = [enabled: true, libraryRevision: 'a' * 40]
[[[:],job],[valid,'shiba-go-ditch-api-project-prod-deploy'],[[enabled:true],job]].each { pair ->
    try { runCase(pair[0],pair[1]); assert false : 'invalid config passed' }
    catch (IllegalStateException expected) {}
}
def calls = runCase(valid,job)
assert calls.findAll { it[0] == 'node' } == [['node','ci-untrusted']]
assert calls.findAll { it[0] == 'stage' }.size() == 3
assert calls.findAll { it[0] == 'sh' }.size() == 2
assert !source.contains('withCredentials')
assert !source.contains('checkout(')
assert !source.contains('build(job:')
assert source.contains('DEV_CONTRACT_ONLY')
println 'PASS 4 DEV rehearsal configuration/control cases (LOCAL DSL mocks only)'

// Execute the actual shell string after Groovy interpolation/escape handling.
// This catches errors that a DSL mock alone cannot, including receipt newline
// escaping. Only the offline suite runs; the node preflight is NOT run on host.
def shell = calls.findAll { it[0] == 'sh' }[1][1].toString()
def executeSuite = { boolean injectFailure ->
    def fixture = java.nio.file.Files.createTempDirectory('release-dev-shell-').toFile()
    try {
        calls.findAll { it[0] == 'writeFile' }.each { call ->
            def file = call[1][0]
            def target = new File(fixture, file.file.toString())
            target.parentFile.mkdirs()
            target.text = file.text.toString()
        }
        if (injectFailure) {
            new File(fixture, 'suite/release-gate.test.py').text = 'raise SystemExit(7)\n'
        }
        def process = new ProcessBuilder('/bin/bash', '-c', shell).directory(fixture).redirectErrorStream(true).start()
        def output = process.inputStream.getText('UTF-8')
        assert process.waitFor(60, java.util.concurrent.TimeUnit.SECONDS) : 'LOCAL offline shell timed out'
        def receipt = new File(fixture, 'reports/contract-receipt.json')
        if (injectFailure) {
            assert process.exitValue() != 0
            assert !receipt.exists() : 'failed tests must never produce SUCCESS receipt'
        } else {
            assert process.exitValue() == 0 : output
            def parsed = new groovy.json.JsonSlurperClassic().parseText(receipt.text)
            assert parsed.scope == 'DEV_CONTRACT_ONLY'
            assert parsed.result == 'SUCCESS'
            assert parsed.runtime_deployed == false && parsed.prod_enabled == false
            assert parsed.library_revision == valid.libraryRevision
            assert parsed.source_sha256.size() == 10
            assert output.contains('Ran 20 tests') && output.contains('Ran 16 tests')
            assert output.contains('Ran 19 tests') && output.contains('Ran 13 tests')
            assert receipt.text.endsWith(System.lineSeparator())
        }
    } finally {
        assert fixture.deleteDir() : 'Could not clean LOCAL test fixture'
    }
}
executeSuite(false)
executeSuite(true)
println 'PASS 2 actual shell cases: 68 offline tests + receipt; failure produces no receipt'
