def source = new File(args[0], 'vars/releaseDevRehearsalPipeline.groovy').text
def runCase = { config, job ->
    def calls = []
    def binding = new Binding([ciPipeline: new Object(), autoReleasePipeline: new Object(),
                              prodDeploymentPipeline: new Object(),
                              env: [JOB_NAME: job, BUILD_NUMBER: '1'], currentBuild: [:]])
    binding.setVariable('error', { message -> throw new IllegalStateException(message) })
    ['echo','properties','disableConcurrentBuilds','buildDiscarder','logRotator','writeFile','archiveArtifacts'].each { name ->
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
assert calls.findAll { it[0] == 'stage' }.size() == 4
assert calls.findAll { it[0] == 'sh' }.size() == 2
assert calls.findAll { it[0] == 'echo' }.size() == 3
assert !source.contains('withCredentials')
assert !source.contains('checkout(')
assert !source.contains('build(job:')
assert source.contains('DEV_CONTRACT_ONLY')
println 'PASS 4 DEV rehearsal configuration/control cases (LOCAL DSL mocks only)'

// Execute the actual shell string after Groovy interpolation/escape handling.
// This catches errors that a DSL mock alone cannot, including receipt newline
// escaping. Only the offline suite runs; the node preflight is NOT run on host.
def shell = calls.findAll { it[0] == 'sh' }[1][1].toString()
def expectedSuites = ['release-gate', 'release-control', 'release-candidate', 'release-deploy',
                      'runtime-image-verify', 'release-recovery', 'release-rebuild',
                      'release-app-binding', 'release-owner-pause', 'release-app-binding-incident', 'release-app-binding-repair', 'release-binding-repair',
                      'release-owner-library-routing']
def expectedResources = ['release-gate.py', 'release-evidence.py', 'release-promotion.py',
                         'release-finalization.py', 'release-deploy.py', 'release-candidate.py',
                         'release-gate.test.py', 'release-control.test.py',
                         'release-candidate.test.py', 'release-deploy.test.py',
                         'runtime-image-verify.py', 'runtime-image-verify.test.py',
                         'release-recovery.py', 'release-recovery.test.py',
                         'release-rebuild.py', 'release-rebuild.test.py',
                         'release-app-binding.py', 'release-app-binding.test.py',
                         'release-owner-pause.test.py', 'release-app-binding-incident.py',
                         'release-app-binding-incident.test.py',
                         'release-app-binding-repair.test.py', 'release-binding-repair.py',
                         'release-binding-repair.test.py', 'release-owner-library-routing.test.py']
def executeSuite = { String failingSuite = null ->
    def fixture = java.nio.file.Files.createTempDirectory('release-dev-shell-').toFile()
    try {
        calls.findAll { it[0] == 'writeFile' }.each { call ->
            def file = call[1][0]
            def target = new File(fixture, file.file.toString())
            target.parentFile.mkdirs()
            target.text = file.text.toString()
        }
        if (failingSuite) {
            // Fault injection checks shell ordering and fail-closed receipt
            // behavior. Run real contracts once in the successful case.
            expectedSuites.each { name ->
                new File(fixture, "suite/${name}.test.py").text = name == failingSuite ?
                    'raise SystemExit(7)\n' : 'import unittest\nclass Offline(unittest.TestCase):\n def test_pass(self): self.assertTrue(True)\nunittest.main()\n'
            }
        }
        def process = new ProcessBuilder('/bin/bash', '-c', shell).directory(fixture).redirectErrorStream(true).start()
        def output = process.inputStream.getText('UTF-8')
        assert process.waitFor(60, java.util.concurrent.TimeUnit.SECONDS) : 'LOCAL offline shell timed out'
        def receipt = new File(fixture, 'reports/contract-receipt.json')
        if (failingSuite) {
            assert process.exitValue() != 0
            assert !receipt.exists() : 'failed tests must never produce SUCCESS receipt'
            def expectedLogs = expectedSuites.take(expectedSuites.indexOf(failingSuite) + 1)
            assert new File(fixture, 'reports').listFiles().collect { it.name }.toSet() ==
                expectedLogs.collect { "${it}.log".toString() }.toSet() : 'shell continued after failed suite'
        } else {
            assert process.exitValue() == 0 : output
            def parsed = new groovy.json.JsonSlurperClassic().parseText(receipt.text)
            assert parsed.scope == 'DEV_CONTRACT_ONLY'
            assert parsed.result == 'SUCCESS'
            assert parsed.runtime_deployed == false && parsed.prod_enabled == false
            assert parsed.library_revision == valid.libraryRevision
            assert parsed.executed_suites == expectedSuites
            assert parsed.source_sha256.keySet() == expectedResources.toSet()
            // Validate each named suite's own output, not a stale count or
            // aggregate stdout that could hide a skipped or repeated suite.
            expectedSuites.each { name ->
                def log = new File(fixture, "reports/${name}.log")
                assert log.isFile() : "Missing ${name} execution log"
                def result = log.text
                assert (result =~ /(?m)^Ran [1-9][0-9]* tests? in .+$/).find() : result
                assert result.trim().endsWith('\nOK') : result
                assert parsed.source_sha256.containsKey("${name}.test.py".toString())
            }
            assert receipt.text.endsWith(System.lineSeparator())
        }
    } finally {
        assert fixture.deleteDir() : 'Could not clean LOCAL test fixture'
    }
}
executeSuite()
expectedSuites.each { executeSuite(it) }
println "PASS ${expectedSuites.size() + 1} actual shell cases: all ${expectedSuites.size()} offline suites + receipt; failure in any suite stops subsequent suites and produces no receipt"
