import org.kohsuke.groovy.sandbox.GroovyInterceptor
import org.kohsuke.groovy.sandbox.SandboxTransformer

// Exercise the actual emitted WorkflowScript with the installed groovy-sandbox
// AST transformer, not only a CPS compiler or string comparison. Library source
// is intentionally untransformed, matching the configured trusted Library.
class EntryBoundary extends GroovyInterceptor {
 void onSuperConstructor(GroovyInterceptor.Invoker invoker, Class receiver, Object... arguments) {
  if (receiver == Script && arguments.length == 1 && arguments[0] instanceof Binding) {
   invoker.call(receiver, null, arguments)
   return
  }
  throw new SecurityException('Unapproved WorkflowScript superclass constructor')
 }
 Object onNewInstance(GroovyInterceptor.Invoker invoker, Class receiver, Object... arguments) {
  throw new SecurityException('Unapproved WorkflowScript constructor: ' + receiver.name)
 }
 Object onStaticCall(GroovyInterceptor.Invoker invoker, Class receiver, String method, Object... arguments) {
  throw new SecurityException('Unapproved WorkflowScript static call')
 }
 Object onMethodCall(GroovyInterceptor.Invoker invoker, Object receiver, String method, Object... arguments) {
  if (receiver instanceof Script && method in ['ocrNativeReleasePipeline', 'ocrNativeDeploymentPipeline']) {
   return invoker.call(receiver, method, arguments)
  }
  throw new SecurityException('Unapproved WorkflowScript method: ' + method)
 }
}

def root = new File(args[0])
def temporary = java.nio.file.Files.createTempDirectory('ocr-rendered-entry-').toFile()
try {
 // Reuse the existing nonsecret renderer fixtures and call the actual Python
 // renderer for BOTH jobs. No Jenkins, node, credentials or native calls occur.
 def python = '''import argparse,importlib.util,json,pathlib,sys
root,out=map(pathlib.Path,sys.argv[1:])
spec=importlib.util.spec_from_file_location('fixtures',root/'resources/scripts/common/ocr-native-release.test.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
case=fixtures.NativeReleaseTests();case.setUp()
try:
 identity=out/'identity.json';identity.write_text(json.dumps(case.identity))
 (out/'release.xml').write_text(fixtures.m.render(argparse.Namespace(identity=identity,enable=False)))
 cfg={'libraryRevision':'f'*40,'sourceCommit':'d'*40,'approvers':['claude-code-use'],'plan':'/Users/surpend/private/plan.json','planSHA256':'1'*64,'ciReceipt':'/Users/surpend/private/ci.json','ciReceiptSHA256':'2'*64,'artifactDirectory':'/Users/surpend/private/ci-artifacts'}
 path=out/'deployment.json';path.write_text(json.dumps(cfg))
 (out/'deploy.xml').write_text(fixtures.m.render_deployment(argparse.Namespace(config=path,enable=False)))
finally:case.tearDown()
'''
 def process = new ProcessBuilder('python3', '-B', '-c', python, root.absolutePath, temporary.absolutePath).redirectErrorStream(true).start()
 def result = process.inputStream.getText('UTF-8')
 assert process.waitFor() == 0 : result

 def runSandbox = { String body, Binding binding ->
  def configuration = new org.codehaus.groovy.control.CompilerConfiguration()
  configuration.addCompilationCustomizers(new SandboxTransformer())
  def boundary = new EntryBoundary()
  boundary.register()
  try { new GroovyShell(this.class.classLoader, binding, configuration).parse(body).run() } finally { boundary.unregister() }
 }
 def received = []
 ['release': 'ocrNativeReleasePipeline', 'deploy': 'ocrNativeDeploymentPipeline'].each { name, function ->
  def xml = new XmlParser().parse(new File(temporary, name + '.xml'))
  def rendered = xml.definition.script.text()
  assert rendered.readLines()[0] == "@Library('jenkins-pipeline@${'f' * 40}') _"
  // @Library retrieval is Jenkins-owned; preserve the complete emitted body.
  def body = rendered.substring(rendered.indexOf('\n') + 1)
  assert body ==~ /(?s)ocrNative(Release|Deployment)Pipeline\('[A-Za-z0-9+\/=]+'\)\n/
  assert !body.contains('JsonSlurper') && !body.contains('new String') && !body.contains('decodeBase64')
  def trusted = new GroovyShell().parse(new File(root, 'vars/' + function + '.groovy'))
  def binding = new Binding()
  binding.setVariable(function, { Object input ->
   def decoded = trusted.decodeNativeConfiguration(input)
   assert decoded instanceof Map && decoded.libraryRevision == 'f' * 40
   received << function
  })
  runSandbox(body, binding)
 }
 assert received == ['ocrNativeReleasePipeline', 'ocrNativeDeploymentPipeline']

 // Demonstrate that this fixture catches the exact old constructor failure.
 try {
  runSandbox("new groovy.json.JsonSlurperClassic().parseText('{}')", new Binding())
  assert false : 'old untrusted JSON constructor escaped the sandbox'
 } catch (SecurityException expected) {
  assert expected.message.contains('JsonSlurperClassic')
 }
 println 'PASS both actual rendered job entries under groovy-sandbox: trusted calls pass; previous JsonSlurper constructor is rejected'
} finally {
 assert temporary.deleteDir()
}
