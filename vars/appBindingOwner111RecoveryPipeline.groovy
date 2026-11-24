def call(Map config = [:]) {
    def job = 'shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-111'
    def causes = currentBuild.getBuildCauses()
    if (config.enabled != true || env.JOB_NAME != job || !(config.libraryRevision ==~ /[0-9a-f]{40}/) ||
        causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        causes[0].userId != 'claude-code-use') {
        error('Owner #111 recovery requires the enabled pinned job and the release operator')
    }

    def action = params.RECOVERY_ACTION?.toString()
    def planBuild = params.RECOVERY_PLAN_BUILD?.toString()?.trim() ?: ''
    def planHash = params.RECOVERY_PLAN_SHA256?.toString()?.trim() ?: ''
    if (!(action in ['rehearsal', 'plan', 'apply']) ||
        (action == 'apply' ? !(planBuild ==~ /[1-9][0-9]{0,7}/ && planHash ==~ /[0-9a-f]{64}/) :
            (planBuild || planHash))) {
        error('Select rehearsal, plan, or apply with the exact plan build and SHA-256')
    }

    timeout(time: 15, unit: 'MINUTES') {
        if (action == 'rehearsal') {
            runOwner111Rehearsal(config.libraryRevision)
        } else {
            runOwner111Action(config, action, planBuild, planHash)
        }
    }
}

def runOwner111Rehearsal(String revision) {
    def suites = ['release-app-binding-owner111', 'release-app-binding-incident', 'release-app-binding', 'release-owner-resume']
    def sources = ['release-app-binding-owner111.py', 'release-app-binding-incident.py', 'release-app-binding.py',
                   'release-promotion.py', 'release-deploy.py', 'release-evidence.py', 'release-gate.py',
                   'release-binding-repair.py'] + suites.collect { it + '.test.py' }
    node('ci-untrusted') {
        dir("owner111-rehearsal-${env.BUILD_NUMBER}") {
            stage('Offline owner #111 recovery contract') {
                sources.each { name ->
                    writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                }
                writeFile file: 'reviewed-library.txt', text: revision + '\n'
                sh '''#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 && -z "${DOCKER_HOST:-}" ]]
[[ ! -S /var/run/docker.sock && ! -S /run/docker.sock ]]
[[ ! -e /var/jenkins_home && ! -e /Users/surpend/Developer ]]
export PYTHONDONTWRITEBYTECODE=1
for suite in release-app-binding-owner111 release-app-binding-incident release-app-binding release-owner-resume; do
    python3 "control/${suite}.test.py"
done
'''
                archiveArtifacts artifacts: 'reviewed-library.txt'
            }
        }
    }
}

def runOwner111Action(Map config, String action, String planBuild, String planHash) {
    ['runtimeRoot', 'stateDirectory', 'receiptKeyCredentials', 'jenkinsReadCredentials', 'scmCredentials'].each { key ->
        if (!config[key]?.toString()?.trim()) { error("Missing owner #111 setting ${key}") }
    }
    node('shiba-prod-owner-macos') {
        def build = action == 'plan' ? env.BUILD_NUMBER : planBuild
        def workspace = "owner111-plan-${build}"
        def result = action == 'plan' ? 'private/recovery-plan.json' : "private/recovery-result-${env.BUILD_NUMBER}.json"
        dir(workspace) {
            try {
                if (action == 'plan') {
                    stage('Check out exact owner #111 recovery source') {
                        dir('source') {
                            checkout([$class: 'GitSCM', branches: [[name: config.libraryRevision]],
                                      userRemoteConfigs: [[
                                          url: 'https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts.git',
                                          credentialsId: config.scmCredentials
                                      ]]])
                        }
                    }
                    stage('Collect the three exact historical receipts') {
                        withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                            usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN')]) {
                            sh '''#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir private
python3 - <<'PY'
import base64
import os
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

class DirectJenkins(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

base = "http://127.0.0.1:9090/job/shiba-release-automation/job/shiba-go-ditch-api-project-prod-deploy/"
auth = base64.b64encode((os.environ["JENKINS_API_USER"] + ":" + os.environ["JENKINS_API_TOKEN"]).encode()).decode()
artifacts = {
    "request-111.json": "111/artifact/request.json",
    "receipt-111.json": "111/artifact/runtime-receipt.json",
    "receipt-110.json": "110/artifact/runtime-receipt.json",
}
for name, path in artifacts.items():
    request = Request(base + path, headers={"Authorization": "Basic " + auth})
    with build_opener(DirectJenkins()).open(request, timeout=10) as response:
        data = response.read(262145)
    if not 0 < len(data) <= 262144:
        raise ValueError("Unexpected historical owner evidence size")
    with (Path("private") / name).open("xb") as stream:
        stream.write(data)
PY
'''
                        }
                    }
                }
                stage(action == 'plan' ? 'Qualify owner #111 recovery' : 'Restore the original owner once') {
                    withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                        withEnv(["RECOVERY_MODE=${action}", "PLAN_SHA=${planHash}", "RESULT_FILE=${result}",
                                 "LIBRARY_REVISION=${config.libraryRevision}", "RUNTIME_ROOT=${config.runtimeRoot}",
                                 "STATE_DIRECTORY=${config.stateDirectory}", 'PYTHONDONTWRITEBYTECODE=1']) {
                            sh '''#!/usr/bin/env bash
set -euo pipefail
umask 077
tool=source/resources/scripts/common/release-app-binding-owner111.py
[[ "$(git -C source rev-parse HEAD)" == "$LIBRARY_REVISION" ]]
if [[ "$RECOVERY_MODE" == plan ]]; then
    python3 "$tool" plan --runtime "$RUNTIME_ROOT" --state-directory "$STATE_DIRECTORY" \
        --metadata "$PWD/private/metadata.json" --collection "$PWD/private/collection.json" \
        --failed-request "$PWD/private/request-111.json" --failed-receipt "$PWD/private/receipt-111.json" \
        --last-good-receipt "$PWD/private/receipt-110.json" --source-revision "$LIBRARY_REVISION" \
        --key-file "$RECEIPT_KEY_FILE" --output "$PWD/$RESULT_FILE"
else
    python3 "$tool" apply --plan "$PWD/private/recovery-plan.json" --expected-plan-sha256 "$PLAN_SHA" \
        --key-file "$RECEIPT_KEY_FILE" --output "$PWD/$RESULT_FILE" --approved-owner111-resume-once
fi
'''
                        }
                    }
                }
            } finally {
                archiveArtifacts artifacts: result, allowEmptyArchive: true
            }
        }
    }
}
