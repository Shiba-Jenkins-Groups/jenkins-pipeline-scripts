import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonSlurperClassic

@NonCPS
def parseJson(String text) {
    new JsonSlurperClassic().parseText(text)
}

def call(Map config = [:]) {
    if (!(config.libraryRevision ==~ /[0-9a-f]{40}/)) { error('Exact recovery Library revision required') }
    def rehearsal = config.rehearsal == true
    def incidentJob = 'shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-101'
    def expectedJob = rehearsal ? 'shiba-go-ditch-api-project-dev-contract-rehearsal' : incidentJob
    if (env.JOB_NAME != expectedJob || config.enabled != true) { error('Unexpected or disabled recovery job') }
    def causes = currentBuild.getBuildCauses()
    if (causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' || causes[0].userId != 'claude-code-use') {
        error('The reviewed recovery job requires a direct build by the release operator')
    }
    def action = rehearsal ? 'rehearsal' : params.RECOVERY_ACTION
    def planHash = rehearsal ? '' : (params.RECOVERY_PLAN_SHA256 ?: '').trim()
    if (!rehearsal && config.closureOnly == true && (action != 'close' || planHash)) {
        error('This route only attests the already released transaction')
    }
    if (!rehearsal && config.closureOnly != true &&
        !(action == 'plan' && !planHash || action == 'apply' && planHash ==~ /[0-9a-f]{64}/)) {
        error('Use plan, or apply with the exact qualified plan SHA256')
    }
    def files = ['release-gate.py', 'release-evidence.py', 'release-promotion.py', 'release-deploy.py',
                 'release-app-binding.py', 'release-binding-repair.py', 'release-app-binding-postdeploy.py',
                 'release-app-binding-postdeploy-state.py', 'release-app-binding-postdeploy-omlx.py',
                 'release-app-binding-postdeploy-close.py']
    def suite = ['release-app-binding-postdeploy', 'release-app-binding-postdeploy-state',
                 'release-app-binding-postdeploy-omlx', 'release-app-binding-postdeploy-close',
                 'release-app-binding', 'release-owner-resume']
    if (rehearsal) {
        files += suite.collect { it + '.test.py' }
    }
    timeout(time: rehearsal ? 8 : 22, unit: 'MINUTES') {
        node(rehearsal ? 'ci-untrusted' : 'shiba-prod-owner-macos') {
            dir("app-binding-101-${env.BUILD_NUMBER}") {
                try {
                    stage('Load Reviewed Recovery Source') {
                        files.each { name ->
                            writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                        }
                        writeFile file: 'reviewed-library.txt', text: config.libraryRevision + '\n'
                    }
                    if (rehearsal) {
                        stage('Offline Recovery Contract') {
                            sh '''#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 ]]
[[ ! -S /var/run/docker.sock && ! -S /run/docker.sock ]]
[[ -z "${DOCKER_HOST:-}" ]]
[[ ! -e /var/jenkins_home && ! -e /Users/surpend/Developer ]]
export PYTHONDONTWRITEBYTECODE=1
mkdir -p reports
for contract in release-app-binding-postdeploy release-app-binding-postdeploy-state \
    release-app-binding-postdeploy-omlx release-app-binding-postdeploy-close release-app-binding release-owner-resume; do
    python3 "control/${contract}.test.py" 2>&1 | tee "reports/${contract}.log"
done
python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path('.')
receipt={'scope':'OFFLINE_APP_BINDING_101','result':'SUCCESS',
         'library_revision':(root/'reviewed-library.txt').read_text().strip(),
         'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'control').glob('*.py'))}}
(root/'reports/contract-receipt.json').write_text(json.dumps(receipt,indent=2)+chr(10))
PY
'''
                        }
                        return
                    }
                    ['dockerEngineId', 'runtimeRoot', 'stateDirectory', 'receiptKeyCredentials', 'jenkinsReadCredentials'].each { key ->
                        if (!config[key]?.toString()?.trim()) { error("Missing trusted recovery setting ${key}") }
                    }
                    stage(action == 'plan' ? 'Attest Existing App Binding' : 'Verify Owner and Recognition Readiness') {
                        def output = action == 'plan' ? 'recovery-plan.json' : 'recovery-receipt.json'
                        withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE'),
                            usernamePassword(credentialsId: config.jenkinsReadCredentials, usernameVariable: 'JENKINS_API_USER',
                                             passwordVariable: 'JENKINS_API_TOKEN')]) {
                            withEnv(["REQUEST_ACTION=${action}", "PLAN_SHA=${planHash}", "RESULT_FILE=${output}",
                                     "LIBRARY_REVISION=${config.libraryRevision}", "RUNTIME_ROOT=${config.runtimeRoot}",
                                     "EARLIER_ATTEMPT=${config.afterAppManagement == true ? 2 : 0}",
                                     "STATE_DIRECTORY=${config.stateDirectory}", "DOCKER_ENGINE_ID=${config.dockerEngineId}",
                                     'PYTHONDONTWRITEBYTECODE=1']) {
                                sh '''python3 control/release-app-binding-postdeploy.py "$REQUEST_ACTION" \
    --jenkins-url http://127.0.0.1:9090 --key-file "$RECEIPT_KEY_FILE" --build-number "$BUILD_NUMBER" \
    --library-revision "$LIBRARY_REVISION" --runtime-root "$RUNTIME_ROOT" --state-directory "$STATE_DIRECTORY" \
    --docker-engine-id "$DOCKER_ENGINE_ID" --plan-sha256 "$PLAN_SHA" \
    --earlier-attempt "$EARLIER_ATTEMPT" --output "$RESULT_FILE"'''
                            }
                        }
                        def result = parseJson(readFile(output)).payload
                        if (result.status != (action == 'plan' ? 'QUALIFIED' : 'SUCCESS')) { error('Recovery evidence status mismatch') }
                    }
                    currentBuild.description = "PROD binding #101 ${action}"
                } finally {
                    archiveArtifacts artifacts: 'reviewed-library.txt,recovery-plan.json,recovery-receipt.json,reports/*', allowEmptyArchive: true
                }
            }
        }
    }
}
