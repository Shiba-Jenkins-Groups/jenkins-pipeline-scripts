import groovy.json.JsonOutput

// No subprocess/network/Docker/credentials are executed by this control-flow test.
def root = new File(args ? args[0] : '.').canonicalFile
def coordinator = 'shiba-release-automation/shiba-go-ditch-api-project-auto-release'
def owner = 'shiba-release-automation/shiba-go-ditch-api-project-prod-deploy'
def config = [approvers: ['operator'], jenkinsApiUrl: 'http://jenkins.invalid',
    harborApiUrl: 'http://harbor.invalid', nexusBaseUrl: 'http://nexus.invalid',
    jenkinsReadCredentials: 'read', scmCredentials: 'scm', receiptKeyCredentials: 'receipt',
    harborCredentials: 'harbor', nexusCredentials: 'nexus', deploymentJob: owner]
def simulate = { Map options = [:] ->
    def calls = []
    def binding = new Binding()
    binding.setVariable('env', [JOB_NAME: options.foreignJob ? 'untrusted' : coordinator, BUILD_NUMBER: '195'])
    binding.setVariable('params', [SOURCE_BUILD: '357', EXPECTED_COMMIT: 'b' * 40,
        RECOVER_COORDINATOR_BUILD: '193', RECOVER_OWNER_BUILD: '90', REVALIDATE_FINALIZED_IMAGE: 'true'])
    binding.setVariable('currentBuild', [getBuildCauses: { -> [[
        _class: options.replay ? 'org.jenkinsci.plugins.workflow.cps.replay.ReplayCause' : 'hudson.model.Cause$UserIdCause',
        userId: options.foreignUser ? 'intruder' : 'operator']] }])
    binding.setVariable('error', { String message -> throw new IllegalStateException(message) })
    binding.setVariable('pwd', { -> '/fake/workspace' })
    binding.setVariable('libraryResource', { String name -> '# offline resource' })
    ['writeFile', 'checkout', 'archiveArtifacts'].each { name ->
        binding.setVariable(name, { Map value -> calls << name })
    }
    ['usernamePassword', 'file', 'text'].each { name ->
        binding.setVariable(name, { Map value -> value })
    }
    ['dir', 'withEnv', 'withCredentials'].each { name ->
        binding.setVariable(name, { Object value, Closure body -> calls << name; body() })
    }
    binding.setVariable('stage', { String name, Closure body ->
        calls << name
        if (name == options.failStage) { throw new IllegalStateException('offline failed stage') }
        body()
    })
    binding.setVariable('sh', { Object script -> calls << script.toString() })
    binding.setVariable('readFile', { String path ->
        if (path.endsWith('revalidation-identity.json')) { return JsonOutput.toJson([original: [prod_build: 210]]) }
        if (path.endsWith('scan-identity.json')) { return JsonOutput.toJson([commit: 'c' * 40,
            version: '1.1.16', image_digest: 'sha256:' + 'd' * 64,
            immutable_image: 'registry/app@sha256:' + 'd' * 64, image_ref: 'registry/app:210']) }
        '{}'
    })
    binding.setVariable('build', { Map value ->
        assert value.job == owner && value.wait == true && value.propagate == false
        calls << 'owner-deploy'
        [number: 91, result: options.ownerFailure ? 'FAILURE' : 'SUCCESS']
    })
    boolean failed = false
    try {
        def pipeline = new GroovyShell(binding).parse(new File(root, 'vars/finalizedImageRevalidation.groovy'))
        pipeline.call(config, [root: '/fake/workspace', control: '/fake/workspace/control',
            coordinator: '193', owner: '90', sourceBuild: 357, sourceCommit: 'b' * 40, approver: 'operator'])
    } catch (IllegalStateException expected) { failed = true }
    [calls: calls, failed: failed]
}
int count = 0
def success = simulate()
assert !success.failed && success.calls.count('owner-deploy') == 1
assert success.calls.indexOf('Authorize Fresh Same Image Deployment') < success.calls.indexOf('owner-deploy')
assert success.calls[-1] == 'archiveArtifacts'
assert !success.calls.any { it.toString().contains('docker build') || it.toString().contains('git tag') }
count++
for (String phase in ['Authenticate Original Finalized Failed Deployment', 'Fresh Source Secret Build Vet and Tests',
        'Fresh Finalized Source Contracts', 'Verify Original Immutable Image in Isolation',
        'Fresh All Severity Scans of Original Image', 'Authorize Fresh Same Image Deployment']) {
    def rejected = simulate([failStage: phase])
    assert rejected.failed && !rejected.calls.contains('owner-deploy')
    assert rejected.calls[-1] == 'archiveArtifacts'
    count++
}
for (Map options in [[foreignJob: true], [foreignUser: true], [replay: true]]) {
    def rejected = simulate(options)
    assert rejected.failed && !rejected.calls.contains('withCredentials') && !rejected.calls.contains('owner-deploy')
    count++
}
def failedOwner = simulate([ownerFailure: true])
assert failedOwner.failed && failedOwner.calls.count('owner-deploy') == 1
assert failedOwner.calls[-1] == 'archiveArtifacts'
count++
println "PASS: ${count} same-image revalidation flow cases; no runtime actions"
