import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

// Called only from the centrally managed product coordinator job, never from a
// branch Jenkinsfile. Installing/enabling the job is a separate release gate.
def call(Map config = [:]) {
    def product = 'shiba-go-ditch-api-project'
    if (config.enabled != true) { error('Automatic release is not enabled') }
    ['jenkinsApiUrl', 'jenkinsReadCredentials', 'harborCredentials', 'harborApiUrl',
     'scmCredentials', 'mergeCredentials', 'approvalKeyCredentials', 'receiptKeyCredentials',
     'stateDirectory', 'builderLabel', 'deploymentJob', 'deploymentNodeLabel', 'libraryRevision',
     'finalizationStateDirectory', 'finalizationWriterCredentials', 'nexusCredentials', 'nexusBaseUrl'].each { key ->
        if (!config[key]?.toString()?.trim()) { error("Missing trusted release configuration: ${key}") }
    }
    if (!(config.approvers instanceof List) || !config.approvers) { error('Explicit approvers are required') }
    if (!(config.libraryRevision ==~ /[0-9a-f]{40}/)) { error('Pin the trusted library revision') }
    if (config.deploymentJob != "${product}-prod-deploy") { error('Unexpected deployment job') }
    if (env.JOB_NAME != "${product}-auto-release") { error('Coordinator job identity mismatch') }
    properties([
        disableConcurrentBuilds(),
        pipelineTriggers([upstream(upstreamProjects: "${product}/develop", threshold: 'UNSTABLE')])
    ])
    def causes = currentBuild.getBuildCauses('hudson.model.Cause$UpstreamCause')
    if (causes.size() != 1 || causes[0].upstreamProject != "${product}/develop") {
        error('A unique completed develop upstream cause is required')
    }
    def sourceBuild = causes[0].upstreamBuild as Integer
    // Pipeline Build Step waits through upstream post actions and retains the
    // exact queue/run relationship across controller restarts.
    def upstreamRun = waitForBuild(runId: "${product}/develop#${sourceBuild}", propagate: false)
    if (!(upstreamRun.result in ['SUCCESS', 'UNSTABLE'])) { error('Develop build is not an eligible completed candidate') }

    node(config.builderLabel.toString()) {
        dir("auto-release-${env.BUILD_NUMBER}") {
            def root = pwd()
            def control = "${root}/control"
            def parse = { path -> new JsonSlurperClassic().parseText(readFile(path)) }
            stage('Load Trusted Release Controls') {
                ['release-gate.py', 'release-evidence.py', 'release-promotion.py',
                 'release-finalization.py', 'release-finalize.sh', 'error-handler.sh', 'nexus-upload.sh', 'git-tag.sh',
                 'harbor-vulnerability-report.py', 'release-askpass.sh'].each { name ->
                    writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                }
                def commonStages = ['Checkout', 'Load Scripts', 'Detect', 'Secret Scan', 'Build', 'Test',
                    'Fast Contract Test', 'Dependency Scan', 'Package / Publish / Tag', 'Docker Build',
                    'Image Scan', 'Harbor Push', 'Harbor Vulnerability Report', 'Smoke Test',
                    'Deployment Verification — k3s', 'Declarative: Post Actions']
                writeFile file: 'control/policy.json', text: JsonOutput.toJson([
                    schema_version: 1, product: product, library_revision: config.libraryRevision,
                    jobs: [promotion: "${product}/develop", deployment: "${product}/prod"],
                    candidate_mode: true, waivable_stages: ['Test', 'Fast Contract Test', 'Dependency Scan', 'Image Scan'],
                    required_stages: [promotion: commonStages, deployment: commonStages],
                    required_scanners: ['trivy', 'govulncheck', 'harbor'], max_evidence_age_seconds: 3600,
                    max_exception_seconds: 900, approvers: config.approvers,
                    revoked_approval_ids: config.revokedApprovalIds ?: []
                ])
                withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                    usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN')]) {
                    withEnv(["RELEASE_JENKINS_URL=${config.jenkinsApiUrl}", "RELEASE_DEPLOY_LABEL=${config.deploymentNodeLabel}"]) {
                        sh 'python3 control/release-evidence.py check-routing --jenkins-url "$RELEASE_JENKINS_URL" --deployment-label "$RELEASE_DEPLOY_LABEL"'
                    }
                }
            }

            def verifyPhase = { String branch, Integer number, String expectedCommit ->
                def phase = "${root}/${branch}"
                withEnv(["RELEASE_PHASE=${phase}", "RELEASE_CONTROL=${control}",
                         "RELEASE_BRANCH=${branch}", "RELEASE_BUILD=${number}",
                         "RELEASE_JENKINS_URL=${config.jenkinsApiUrl}", "RELEASE_HARBOR_URL=${config.harborApiUrl}"]) {
                    stage("${branch}: Complete Build Evidence") {
                        sh 'mkdir -p "$RELEASE_PHASE"'
                        withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                            usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN')]) {
                            sh '''python3 "$RELEASE_CONTROL/release-evidence.py" inspect \
                                --jenkins-url "$RELEASE_JENKINS_URL" --branch "$RELEASE_BRANCH" \
                                --build "$RELEASE_BUILD" --candidate-root "$RELEASE_PHASE/evidence" --output "$RELEASE_PHASE/identity.json"'''
                        }
                        def identity = parse("${phase}/identity.json")
                        if (expectedCommit && identity.commit != expectedCommit) { error('PROD checkout differs from promoted commit') }
                        dir("${phase}/source") {
                            checkout([$class: 'GitSCM', branches: [[name: identity.commit]],
                                userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git',
                                    credentialsId: config.scmCredentials]]])
                        }
                    }
                    stage("${branch}: All Severity Scans") {
                        try {
                          withCredentials([usernamePassword(credentialsId: config.harborCredentials,
                            usernameVariable: 'HARBOR_USER', passwordVariable: 'HARBOR_PASS')]) {
                            sh '''python3 "$RELEASE_CONTROL/release-evidence.py" scan \
                                --identity "$RELEASE_PHASE/identity.json" --source "$RELEASE_PHASE/source" \
                                --output "$RELEASE_PHASE/evidence" --harbor-url "$RELEASE_HARBOR_URL"'''
                          }
                        } finally {
                            archiveArtifacts artifacts: "${branch}/identity.json,${branch}/evidence/*.json,${branch}/evidence/*.jsonl,${branch}/evidence/.pipeline/candidate*,${branch}/evidence/.pipeline/candidate-stages/*,${branch}/evidence/reports/junit/*", allowEmptyArchive: true
                        }
                    }
                    stage("${branch}: Release Gate") {
                        sh '''python3 "$RELEASE_CONTROL/release-promotion.py" review \
                            --evidence "$RELEASE_PHASE/evidence/evidence.json" --policy "$RELEASE_CONTROL/policy.json" \
                            --output "$RELEASE_PHASE/review.json"'''
                        archiveArtifacts artifacts: "${branch}/evidence/*.json,${branch}/review.json", allowEmptyArchive: false
                        def review = parse("${phase}/review.json")
                        if (review.decision == 'NEEDS_APPROVAL') {
                            def response
                            timeout(time: 15, unit: 'MINUTES') {
                                response = input(id: "${branch}-cve-exception", submitter: config.approvers.join(','),
                                    submitterParameter: 'APPROVER', ok: 'Approve these exact findings',
                                    message: "${branch} build #${number}: ${review.findings.size()} exact test-stage/CVE findings. Review the archived ${branch}/review.json and native reports; approval applies only to this gate and expires in 15 minutes. Original CI results remain unchanged.",
                                    parameters: [text(name: 'REASON', defaultValue: '', description: 'Reason for accepting the listed findings')])
                            }
                            if (!response.REASON?.trim() || !config.approvers.contains(response.APPROVER)) {
                                error('Exception approval identity/reason is invalid')
                            }
                            writeFile file: "${phase}/approval-identity.json", text: JsonOutput.toJson([
                                id: "${env.JOB_NAME}#${env.BUILD_NUMBER}:${branch}",
                                approver: response.APPROVER, reason: response.REASON
                            ])
                            withCredentials([file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE')]) {
                                sh '''python3 "$RELEASE_CONTROL/release-promotion.py" approve \
                                    --evidence "$RELEASE_PHASE/evidence/evidence.json" --policy "$RELEASE_CONTROL/policy.json" \
                                    --identity "$RELEASE_PHASE/approval-identity.json" --approval-key-file "$APPROVAL_KEY_FILE" \
                                    --output "$RELEASE_PHASE/approval.json"'''
                            }
                            archiveArtifacts artifacts: "${branch}/approval.json", allowEmptyArchive: false
                        } else if (review.decision != 'PASS') { error('Release gate blocked') }
                    }
                }
                return parse("${phase}/identity.json")
            }

            verifyPhase('develop', sourceBuild, null)
            stage('Promote Verified Commit') {
                withCredentials([
                    usernamePassword(credentialsId: config.mergeCredentials,
                        usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD'),
                    file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE'),
                    file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                    withEnv(["RELEASE_ROOT=${root}", "RELEASE_STATE=${config.stateDirectory}",
                             "GIT_ASKPASS=${control}/release-askpass.sh", 'GIT_TERMINAL_PROMPT=0']) {
                        sh '''#!/usr/bin/env bash
set -euo pipefail
chmod 700 "$GIT_ASKPASS"
approval=()
if [[ -f develop/approval.json ]]; then approval=(--approval develop/approval.json); fi
python3 control/release-promotion.py promote --source develop/source --state-directory "$RELEASE_STATE" \
    --evidence develop/evidence/evidence.json --policy control/policy.json \
    --approval-key-file "$APPROVAL_KEY_FILE" --receipt-key-file "$RECEIPT_KEY_FILE" \
    "${approval[@]}" --output promotion.json
'''
                    }
                }
                archiveArtifacts artifacts: 'promotion.json', allowEmptyArchive: false
            }
            def promotion = parse('promotion.json')
            def prodRun
            stage('Run PROD CI/CD') {
                prodRun = build(job: "${product}/prod", wait: true, propagate: false, quietPeriod: 0)
                if (!(prodRun.result in ['SUCCESS', 'UNSTABLE'])) { error('PROD CI/CD failed; no deployment authorized') }
            }
            verifyPhase('prod', prodRun.number as Integer, promotion.payload.merge_commit.toString())
            stage('Finalize Revalidated PROD Candidate') {
              try {
                withCredentials([
                    usernamePassword(credentialsId: config.scmCredentials, usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD'),
                    usernamePassword(credentialsId: config.finalizationWriterCredentials, usernameVariable: 'GITHUB_CREDENTIALS_USR', passwordVariable: 'GITHUB_CREDENTIALS_PSW'),
                    usernamePassword(credentialsId: config.nexusCredentials, usernameVariable: 'NEXUS_CRED_USR', passwordVariable: 'NEXUS_CRED_PSW'),
                    file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE'),
                    file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                    withEnv(["FINALIZATION_STATE=${config.finalizationStateDirectory}", "NEXUS_BASE_URL=${config.nexusBaseUrl}",
                             "GIT_ASKPASS=${control}/release-askpass.sh", 'GIT_TERMINAL_PROMPT=0']) {
                        sh '''#!/usr/bin/env bash
set -euo pipefail
approval=()
if [[ -f prod/approval.json ]]; then approval=(--approval prod/approval.json); fi
python3 control/release-finalization.py --source prod/source --state-directory "$FINALIZATION_STATE" \
    --evidence prod/evidence/evidence.json --policy control/policy.json \
    --approval-key-file "$APPROVAL_KEY_FILE" --receipt-key-file "$RECEIPT_KEY_FILE" \
    "${approval[@]}" --output finalization.json
'''
                    }
                }
              } finally {
                archiveArtifacts artifacts: 'finalization.json,prod/source/.pipeline/release-manifest.env', allowEmptyArchive: true
              }
            }
            stage('Authorize Deployment Handoff') {
                withCredentials([file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE'),
                                 file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
approval=()
if [[ -f prod/approval.json ]]; then approval=(--approval prod/approval.json); fi
python3 control/release-promotion.py handoff --promotion promotion.json --finalization finalization.json \
    --evidence prod/evidence/evidence.json --policy control/policy.json \
    --approval-key-file "$APPROVAL_KEY_FILE" --receipt-key-file "$RECEIPT_KEY_FILE" \
    "${approval[@]}" --output deployment-request.json
'''
                }
                archiveArtifacts artifacts: 'deployment-request.json', allowEmptyArchive: false
            }
            stage('PROD Runtime Deployment') {
                def deployed = build(job: config.deploymentJob, wait: true, propagate: false,
                    parameters: [text(name: 'SIGNED_RELEASE_REQUEST', value: readFile('deployment-request.json'))])
                if (deployed.result != 'SUCCESS') { error('PROD runtime deployment failed; inspect its receipt') }
                currentBuild.description = "prod ${promotion.payload.merge_commit.take(12)}; deploy #${deployed.number}"
            }
        }
    }
}
