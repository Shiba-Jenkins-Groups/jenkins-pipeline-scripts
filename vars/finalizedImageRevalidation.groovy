import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic
import com.cloudbees.groovy.cps.NonCPS

@NonCPS
def readRevalidationJson(String raw) {
    new JsonSlurperClassic().parseText(raw)
}

// Called only after the fixed coordinator authenticates its unique operator
// and exact original recovery coordinates. Never rebuilds/pushes an image.
def call(Map config, Map lane) {
    def coordinator = 'shiba-release-automation/shiba-go-ditch-api-project-auto-release'
    def causes = currentBuild.getBuildCauses()
    if (env.JOB_NAME != coordinator || causes.size() != 1 ||
        causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        !config.approvers.contains(causes[0].userId) || lane.approver != causes[0].userId ||
        params.REVALIDATE_FINALIZED_IMAGE?.toString()?.trim() != 'true' ||
        params.SOURCE_BUILD?.toString()?.trim() != lane.sourceBuild.toString() ||
        params.EXPECTED_COMMIT?.toString()?.trim() != lane.sourceCommit ||
        params.RECOVER_COORDINATOR_BUILD?.toString()?.trim() != lane.coordinator.toString() ||
        params.RECOVER_OWNER_BUILD?.toString()?.trim() != lane.owner.toString() ||
        lane.root != pwd() || lane.control != "${pwd()}/control") {
        error('Same-image revalidation requires the fixed trusted coordinator and exact operator request')
    }
    def phase = "${lane.root}/revalidation"
    def source = "${phase}/source"
    withEnv(["REVALIDATION_ROOT=${phase}", "REVALIDATION_SOURCE=${source}",
             "RELEASE_CONTROL=${lane.control}", "REVALIDATION_COORDINATOR=${lane.coordinator}",
             "REVALIDATION_OWNER=${lane.owner}", "REVALIDATION_SOURCE_BUILD=${lane.sourceBuild}",
             "REVALIDATION_SOURCE_COMMIT=${lane.sourceCommit}", "REVALIDATION_APPROVER=${lane.approver}",
             "RELEASE_JENKINS_URL=${config.jenkinsApiUrl}", "RELEASE_HARBOR_URL=${config.harborApiUrl}",
             "NEXUS_BASE_URL=${config.nexusBaseUrl}", 'PYTHONDONTWRITEBYTECODE=1']) {
      try {
        stage('Authenticate Original Finalized Failed Deployment') {
            withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN'),
                file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                sh '''python3 "$RELEASE_CONTROL/release-revalidate.py" collect \
    --root "$REVALIDATION_ROOT" --policy "$RELEASE_CONTROL/policy.json" \
    --receipt-key-file "$RECEIPT_KEY_FILE" --jenkins-url "$RELEASE_JENKINS_URL" \
    --coordinator-build "$REVALIDATION_COORDINATOR" --owner-build "$REVALIDATION_OWNER" \
    --source-build "$REVALIDATION_SOURCE_BUILD" --expected-commit "$REVALIDATION_SOURCE_COMMIT"'''
            }
        }
        def identity = readRevalidationJson(readFile("${phase}/revalidation-identity.json"))
        def seed = readRevalidationJson(readFile("${phase}/scan-identity.json"))
        stage('Checkout Unchanged Finalized Source') {
            dir(source) {
                checkout([$class: 'GitSCM', branches: [[name: seed.commit]],
                    userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git',
                        credentialsId: config.scmCredentials]]])
                // Only known CI output paths are excluded. Tracked files and
                // every other untracked file still have to remain unchanged.
                sh '''printf '\n/reports/\n/gitleaks-report.json\n/coverage.out\n' >> "$(git rev-parse --git-path info/exclude)"'''
            }
            ['common/error-handler.sh', 'common/secret-scan.sh', 'common/branch-policy.sh',
             'common/release-revalidation-checks.sh', 'go/go-env.sh', 'go/go-build.sh', 'go/go-test.sh', 'go/go-archive.sh'].each { name ->
                writeFile file: "${source}/.pipeline/scripts/${name}", text: libraryResource("scripts/${name}")
            }
            writeFile file: "${phase}/runtime/image-ref.txt", text: """APP_NAME=shiba-go-ditch-api-project
BRANCH=prod
BUILD_NUMBER=${identity.original.prod_build}
APP_VERSION=${seed.version}
IMAGE_DIGEST=${seed.image_digest}
IMAGE_REF=${seed.image_ref}
"""
            writeFile file: "${phase}/runtime/context.json", text: JsonOutput.toJson([
                coordinator_job: env.JOB_NAME, coordinator_build: env.BUILD_NUMBER as Integer,
                original_prod_build: identity.original.prod_build, commit: seed.commit,
                version: seed.version, image: seed.immutable_image])
        }
        withEnv(["WORKSPACE=${source}", "GIT_COMMIT=${seed.commit}"]) {
            stage('Fresh Source Secret Build Vet and Tests') {
                sh '''python3 "$RELEASE_CONTROL/release-revalidate.py" check \
    --root "$REVALIDATION_ROOT" --source "$REVALIDATION_SOURCE" --kind test \
    --report-path "$REVALIDATION_SOURCE/reports/junit/go-tests.xml" -- \
    bash "$REVALIDATION_SOURCE/.pipeline/scripts/common/release-revalidation-checks.sh" test'''
            }
            stage('Fresh Finalized Source Contracts') {
                sh '''python3 "$RELEASE_CONTROL/release-revalidate.py" check \
    --root "$REVALIDATION_ROOT" --source "$REVALIDATION_SOURCE" --kind contract -- \
    bash "$REVALIDATION_SOURCE/.pipeline/scripts/common/release-revalidation-checks.sh" contract'''
            }
            stage('Verify Original Immutable Image in Isolation') {
                withEnv(["WORKSPACE=${phase}/runtime"]) {
                    sh '''python3 "$RELEASE_CONTROL/release-revalidate.py" check \
    --root "$REVALIDATION_ROOT" --source "$REVALIDATION_SOURCE" --kind runtime \
    --report-path "$REVALIDATION_ROOT/runtime/reports/runtime-image.json" -- \
    python3 "$RELEASE_CONTROL/runtime-image-verify.py" --revalidation "$REVALIDATION_ROOT/runtime/context.json"'''
                }
            }
            stage('Fresh All Severity Scans of Original Image') {
                sh 'python3 "$RELEASE_CONTROL/scanner-db-manager.py" --local-cache "$HOME/.cache/shiba-release-trivy" --output revalidation/scanner-db-receipt.json'
                withCredentials([usernamePassword(credentialsId: config.harborCredentials,
                    usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASS')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
cd "$REVALIDATION_SOURCE"
. ./go-pipeline.env
python3 "$RELEASE_CONTROL/release-revalidate.py" scan \
    --root "$REVALIDATION_ROOT" --source "$REVALIDATION_SOURCE" \
    --policy "$RELEASE_CONTROL/policy.json" --harbor-url "$RELEASE_HARBOR_URL"'''
                }
            }
        }
        stage('Authorize Fresh Same Image Deployment') {
            withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE'),
                usernamePassword(credentialsId: config.scmCredentials,
                    usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD'),
                usernamePassword(credentialsId: config.nexusCredentials,
                    usernameVariable: 'NEXUS_CRED_USR', passwordVariable: 'NEXUS_CRED_PSW')]) {
                withEnv(["GIT_ASKPASS=${lane.control}/release-askpass.sh", 'GIT_TERMINAL_PROMPT=0']) {
                    sh '''chmod 700 "$GIT_ASKPASS"
python3 "$RELEASE_CONTROL/release-revalidate.py" handoff \
    --root "$REVALIDATION_ROOT" --source "$REVALIDATION_SOURCE" --policy "$RELEASE_CONTROL/policy.json" \
    --receipt-key-file "$RECEIPT_KEY_FILE" --approver "$REVALIDATION_APPROVER" --output deployment-request.json'''
                }
            }
        }
        stage('PROD Same Image Deployment and Binding Handoff') {
            def deployed = build(job: config.deploymentJob, wait: true, propagate: false,
                parameters: [text(name: 'SIGNED_RELEASE_REQUEST', value: readFile('deployment-request.json'))])
            if (deployed.result != 'SUCCESS') { error('Same-image runtime deployment failed; preserve all receipts') }
            currentBuild.description = "revalidated original image ${seed.version}; coordinator #${lane.coordinator}; deploy #${deployed.number}"
        }
      } finally {
        archiveArtifacts artifacts: 'deployment-request.json,revalidation/historical/**,revalidation/fresh/**,revalidation/*.json,revalidation/runtime/**', allowEmptyArchive: true
      }
    }
}
