import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonSlurperClassic

@NonCPS
def parseReleaseJson(String raw) {
    new JsonSlurperClassic().parseText(raw)
}

// Centrally managed, restricted macOS job. Never load this from product SCM.
def call(Map config = [:]) {
    def product = 'shiba-go-ditch-api-project'
    def releaseFolder = 'shiba-release-automation'
    if (config.enabled != true) { error('PROD deployment is not enabled') }
    ['nodeLabel', 'runtimeRoot', 'stateDirectory', 'dockerEngineId', 'receiptKeyCredentials', 'scmCredentials', 'libraryRevision'].each { key ->
        if (!config[key]?.toString()?.trim()) { error("Missing trusted deployment configuration: ${key}") }
    }
    if (!(config.libraryRevision ==~ /[0-9a-f]{40}/)) { error('Pin the trusted deployment library revision') }
    if (env.JOB_NAME != "${releaseFolder}/${product}-prod-deploy") { error('Deployment job identity mismatch') }
    properties([disableConcurrentBuilds(), parameters([text(name: 'SIGNED_RELEASE_REQUEST', defaultValue: '')])])
    // Pipeline build() uses BuildUpstreamCause while an upstream trigger uses
    // UpstreamCause. Inspect every cause so the trusted handoff cannot be
    // rejected because of subtype serialization or hidden among extra causes.
    def causes = currentBuild.getBuildCauses()
    if (causes.size() != 1 || causes[0].upstreamProject != "${releaseFolder}/${product}-auto-release") {
        error('Deployment requires the trusted coordinator upstream cause')
    }
    if (!params.SIGNED_RELEASE_REQUEST?.trim() || params.SIGNED_RELEASE_REQUEST.size() > 65536) {
        error('Signed deployment request is missing or oversized')
    }
    // This hint chooses only a read-only verifier. No runtime action is chosen
    // until that verifier has authenticated the complete signed request.
    def bindingRepair = parseReleaseJson(params.SIGNED_RELEASE_REQUEST)?.payload?.kind == 'app-binding-repair'
    timeout(time: 45, unit: 'MINUTES') {
        node(config.nodeLabel.toString()) {
            dir("controlled-prod-${env.BUILD_NUMBER}") {
                def root = pwd()
                try {
                    stage('Verify Signed Deployment Request') {
                        ['release-gate.py', 'release-promotion.py', 'release-deploy.py', 'release-askpass.sh', 'release-rebuild.py',
                         'release-recovery.py', 'release-evidence.py',
                         'release-finalization.py', 'release-app-binding.py', 'release-binding-repair.py'].each { name ->
                            writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                        }
                        writeFile file: 'request.json', text: params.SIGNED_RELEASE_REQUEST
                        withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                            if (bindingRepair) {
                                withEnv(["REPAIR_LIBRARY=${config.libraryRevision}"]) {
                                    sh 'PYTHONDONTWRITEBYTECODE=1 python3 control/release-binding-repair.py inspect --request request.json --key-file "$RECEIPT_KEY_FILE" --library-revision "$REPAIR_LIBRARY" --output identity.json'
                                }
                            } else {
                                sh 'PYTHONDONTWRITEBYTECODE=1 python3 control/release-deploy.py inspect --request request.json --key-file "$RECEIPT_KEY_FILE" --output identity.json'
                            }
                        }
                    }
                    def identity = parseReleaseJson(readFile('identity.json'))
                    if (bindingRepair) {
                        if (identity.kind != 'app-binding-repair') { error('Verified binding repair identity required') }
                        if (identity.coordinator_build != causes[0].upstreamBuild) { error('Binding repair coordinator handoff mismatch') }
                        stage('Locked Native App Binding Maintenance') {
                            withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                                withEnv(["RELEASE_RUNTIME_ROOT=${config.runtimeRoot}", "RELEASE_STATE=${config.stateDirectory}",
                                         "RELEASE_ENGINE=${config.dockerEngineId}", "REPAIR_LIBRARY=${config.libraryRevision}",
                                         'PYTHONDONTWRITEBYTECODE=1']) {
                                    sh '''python3 control/release-binding-repair.py repair --request request.json \
    --key-file "$RECEIPT_KEY_FILE" --library-revision "$REPAIR_LIBRARY" \
    --runtime-root "$RELEASE_RUNTIME_ROOT" --state-directory "$RELEASE_STATE" \
    --docker-engine-id "$RELEASE_ENGINE" --output runtime-receipt.json'''
                                }
                            }
                        }
                        def repaired = parseReleaseJson(readFile('runtime-receipt.json'))
                        if (repaired.payload.kind != 'app-binding-maintenance' || repaired.payload.status != 'SUCCESS') {
                            error('Native App binding maintenance receipt is not SUCCESS')
                        }
                        currentBuild.description = "App binding maintenance ${identity.version} ${identity.commit.take(12)}"
                        return
                    }
                    if (identity.library_revision != config.libraryRevision) { error('Deployment library handoff mismatch') }
                    stage('Checkout Exact Promoted Source') {
                        dir('source') {
                            checkout([$class: 'GitSCM', branches: [[name: identity.commit]],
                                userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git',
                                    credentialsId: config.scmCredentials]]])
                        }
                    }
                    stage('Locked PROD Deployment and Runtime Verification') {
                        withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE'),
                            usernamePassword(credentialsId: config.scmCredentials,
                                usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD')]) {
                            withEnv(["RELEASE_RUNTIME_ROOT=${config.runtimeRoot}", "RELEASE_STATE=${config.stateDirectory}",
                                     "RELEASE_ENGINE=${config.dockerEngineId}", "GIT_ASKPASS=${root}/control/release-askpass.sh",
                                     'GIT_TERMINAL_PROMPT=0', 'PYTHONDONTWRITEBYTECODE=1']) {
                                sh '''chmod 700 "$GIT_ASKPASS"
python3 control/release-deploy.py deploy --request request.json --key-file "$RECEIPT_KEY_FILE" \
    --source source --runtime-root "$RELEASE_RUNTIME_ROOT" --state-directory "$RELEASE_STATE" \
    --docker-engine-id "$RELEASE_ENGINE" --output runtime-receipt.json'''
                            }
                        }
                    }
                    def receipt = parseReleaseJson(readFile('runtime-receipt.json'))
                    if (receipt.payload.status != 'SUCCESS') { error('Runtime receipt is not SUCCESS') }
                    currentBuild.description = "PROD ${identity.version} ${identity.commit.take(12)} ${identity.digest}"
                } finally {
                    archiveArtifacts artifacts: 'request.json,identity.json,runtime-receipt.json,app-binding-failure.json', allowEmptyArchive: true
                }
            }
        }
    }
}
