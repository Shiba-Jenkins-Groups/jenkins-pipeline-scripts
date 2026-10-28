import com.cloudbees.groovy.cps.CpsTransformer
import org.codehaus.groovy.control.CompilerConfiguration

def root = new File(args ? args[0] : '.').canonicalFile
def file = new File(root, 'vars/recognitionLocalArtifactPipeline.groovy')
def source = file.text
def valid = [enabled: true, libraryRevision: 'a' * 40,
             sourceCommit: '9d0400d003de68be827524d21543200bf78bfdfd', approvers: ['reviewer'],
             builderEngineId: '11111111-1111-1111-1111-111111111111']
def simulate = { Map changes = [:] ->
    def calls = [], written = [:]
    def environment = [JOB_NAME: 'shiba-local-artifacts/recognition-autorecovery', BUILD_NUMBER: '7',
        BUILD_URL: 'http://jenkins.invalid/job/shiba-local-artifacts/job/recognition-autorecovery/7/'] + (changes.env ?: [:])
    def causes = changes.containsKey('causes') ? changes.causes :
        [[_class: 'hudson.model.Cause$UserIdCause', userId: 'reviewer']]
    def binding = new Binding([env: environment, params: changes.params ?: [:],
                              currentBuild: [getBuildCauses: { -> causes }]])
    binding.setVariable('error', { message -> throw new IllegalStateException(message) })
    ['disableConcurrentBuilds','buildDiscarder','logRotator','pipelineTriggers','properties','deleteDir'].each { name ->
        binding.setVariable(name, { Object... values -> calls << [name, values.toList()]; [:] })
    }
    ['timeout','node','dir','stage'].each { name ->
        binding.setVariable(name, { value, Closure body -> calls << [name,value]; body() })
    }
    binding.setVariable('checkout', { Map value -> calls << ['checkout',value] })
    binding.setVariable('writeFile', { Map value -> written[value.file] = value.text })
    binding.setVariable('libraryResource', { path -> new File(root, 'resources/' + path).text })
    binding.setVariable('sh', { value ->
        calls << ['sh',value]
        if (value instanceof Map && value.returnStdout) { return 'c' * 64 + '  artifact/artifact-receipt.json\n' }
        if (changes.failBuild && value.contains('recognition-local-artifact.py build')) {
            throw new IllegalStateException('fixture compiler failed')
        }
        if (changes.failImage && value.contains('recognition-local-artifact.py image')) {
            throw new IllegalStateException('fixture image builder failed')
        }
    })
    binding.setVariable('stash', { Map value -> calls << ['stash',value] })
    binding.setVariable('unstash', { String value -> calls << ['unstash',value] })
    binding.setVariable('withEnv', { List value, Closure body -> calls << ['withEnv',value]; body() })
    binding.setVariable('archiveArtifacts', { Map value -> calls << ['archive',value] })
    try {
        new GroovyShell(binding).parse(source).call(valid + (changes.config ?: [:]))
        [ok:true,calls:calls,written:written]
    } catch (IllegalStateException failure) {
        [ok:false,calls:calls,written:written,error:failure.message]
    }
}
def ok = simulate()
assert ok.ok
assert ok.calls.findAll { it[0] == 'node' } == [['node','ci-untrusted'],['node','ci-image-builder']]
assert ok.calls.findAll { it[0] == 'checkout' }.size() == 1
def checkout = ok.calls.find { it[0] == 'checkout' }[1]
assert checkout.branches == [[name:valid.sourceCommit]]
assert checkout.userRemoteConfigs[0].credentialsId == 'github-scm-readonly'
assert checkout.userRemoteConfigs[0].url == 'https://github.com/ShibaDev2026/shiba-go-ditch-recognition-project.git'
assert ok.calls.findAll { it[0] == 'archive' }.size() == 2
assert ok.calls.find { it[0] == 'archive' }[1].fingerprint == true
assert ok.calls.find { it[0] == 'archive' }[1].allowEmptyArchive == false
assert ok.written.keySet() == ['.ci/identity.json','.ci/recognition-local-artifact.py','.ci/recognition-local-go-toolchain.py'] as Set
assert !source.contains('withCredentials') && !source.contains('build(job:')
assert !source.contains('ciPipeline(') && !source.contains('load(')
assert ok.calls.findAll { it[0] == 'stash' }.size() == 1
assert ok.calls.findAll { it[0] == 'unstash' }.size() == 1
assert ok.calls.find { it[0] == 'stash' }[1].includes.startsWith('artifact/')
assert !ok.calls.find { it[0] == 'stash' }[1].includes.contains('source')
assert ok.calls.findAll { it[0] == 'pipelineTriggers' } == [['pipelineTriggers',[[]]]]

def negatives = [
    [config:[enabled:false]], [config:[sourceCommit:'f' * 40]], [config:[libraryRevision:'main']],
    [config:[approvers:[]]], [config:[scmCredentials:'prod-writer']],
    [config:[builderEngineId:'other']],
    [env:[JOB_NAME:'shiba-go-ditch-recognition-project/prod']], [env:[CHANGE_ID:'1']],
    [env:[BUILD_NUMBER:'7;push']], [params:[SOURCE_COMMIT:valid.sourceCommit]],
    [causes:[]], [causes:[[_class:'hudson.model.Cause$UpstreamCause',upstreamProject:'prod']]],
    [causes:[[_class:'hudson.model.Cause$UserIdCause',userId:'other']]],
    [causes:[[_class:'hudson.model.Cause$UserIdCause',userId:'reviewer'],[_class:'hudson.model.Cause$TimerTriggerCause']]]
]
negatives.each { test ->
    def result = simulate(test)
    assert !result.ok : test
    assert !result.calls.any { it[0] in ['node','checkout','sh','archive'] } : 'Rejected identity entered agent'
}
def failed = simulate([failBuild:true])
assert !failed.ok && !failed.calls.any { it[0] == 'archive' } : 'Failed compiler archived a success'
def failedImage = simulate([failImage:true])
assert !failedImage.ok && failedImage.calls.findAll { it[0] == 'archive' }.size() == 1
assert !failedImage.calls.findAll { it[0] == 'archive' }.any { it[1].artifacts.contains('image-artifact/') }
println "PASS ${negatives.size()+3} LOCAL artifact flow cases; no shell, checkout, credentials or runtime executed"

// Compile with the actual installed CPS transformer, without executing call().
def configuration = new CompilerConfiguration()
configuration.addCompilationCustomizers(new CpsTransformer())
new GroovyClassLoader(this.class.classLoader, configuration).parseClass(file)
println 'PASS actual offline Jenkins CPS compilation of recognitionLocalArtifactPipeline'
