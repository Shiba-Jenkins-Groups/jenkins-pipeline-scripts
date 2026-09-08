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
