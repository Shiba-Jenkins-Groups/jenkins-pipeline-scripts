// Offline fixed-job flow: failures must still archive owner evidence.
def root=new File(args[0])
def source=new File(root,'vars/ocrNativeDeploymentPipeline.groovy').text
def cfg=[enabled:true,libraryRevision:'a'*40,sourceCommit:'b'*40,approvers:['operator'],
         plan:'/Users/surpend/review/plan.json',planSHA256:'c'*64,
         ciReceipt:'/Users/surpend/review/ci.json',ciReceiptSHA256:'d'*64,
         artifactDirectory:'/Users/surpend/review/artifacts']
def runCase={ boolean failMutation ->
 def calls=[]
 def binding=new Binding([env:[JOB_NAME:'shiba-release-automation/apple-vision-native-deploy',BUILD_NUMBER:'1'],
 currentBuild:[getBuildCauses:{[[_class:'hudson.model.Cause$UserIdCause',userId:'operator']]}],params:[:]])
 binding.setVariable('error',{message->throw new IllegalStateException(message)})
 ['properties','disableConcurrentBuilds','buildDiscarder','logRotator','pipelineTriggers','deleteDir','checkout','archiveArtifacts','writeFile'].each{name->binding.setVariable(name,{Object... values->calls<<[name,values.toList()];[:]})}
 binding.setVariable('sh',{Object... values->calls<<['sh',values.toList()];if(failMutation)throw new IllegalStateException('owner mutation unknown');[:]})
 ['timeout','node','dir','stage'].each{name->binding.setVariable(name,{value,Closure body->calls<<[name,value];body()})}
 def failure=null
 try{new GroovyShell(binding).parse(source).call(cfg)}catch(IllegalStateException error){failure=error.message}
 [calls:calls,failure:failure]
}
def success=runCase(false)
assert success.failure==null
assert success.calls.findAll{it[0]=='archiveArtifacts'}.size()==1
assert source.contains("receipt['state']=='succeeded'")&&source.contains("receipt['owner_reactivated'] is True")
def unknown=runCase(true)
assert unknown.failure=='owner mutation unknown'
assert unknown.calls.findAll{it[0]=='archiveArtifacts'}.size()==1
assert unknown.calls.find{it[0]=='archiveArtifacts'}[1][0].allowEmptyArchive==false
println 'PASS deployment owner failures retain archival and cannot turn into success'

if (args.length > 1) {
 def configuration = new org.codehaus.groovy.control.CompilerConfiguration()
 configuration.addCompilationCustomizers(new com.cloudbees.groovy.cps.CpsTransformer())
 new GroovyClassLoader(this.class.classLoader, configuration).parseClass(source, 'ocrNativeDeploymentPipeline.groovy')
 println 'PASS actual deployment Pipeline source compiled with Jenkins CPS'
}
