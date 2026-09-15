import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonOutput
import groovy.json.JsonSlurperClassic

@NonCPS
def parseReleaseJson(String raw) {
    new JsonSlurperClassic().parseText(raw)
}

// Called only from the centrally managed product coordinator job, never from a
// branch Jenkinsfile. Installing/enabling the job is a separate release gate.
def call(Map config = [:]) {
    def product = 'shiba-go-ditch-api-project'
    def releaseFolder = 'shiba-release-automation'
    if (config.enabled != true) { error('Automatic release is not enabled') }
    ['jenkinsApiUrl', 'jenkinsReadCredentials', 'harborCredentials', 'harborApiUrl',
     'scmCredentials', 'mergeCredentials', 'approvalKeyCredentials', 'receiptKeyCredentials',
     'stateDirectory', 'builderLabel', 'deploymentJob', 'deploymentNodeLabel', 'libraryRevision',
     'finalizationStateDirectory', 'finalizationWriterCredentials', 'nexusCredentials', 'nexusBaseUrl'].each { key ->
        if (!config[key]?.toString()?.trim()) { error("Missing trusted release configuration: ${key}") }
    }
    if (!(config.approvers instanceof List) || !config.approvers) { error('Explicit approvers are required') }
    if (!(config.libraryRevision ==~ /[0-9a-f]{40}/)) { error('Pin the trusted library revision') }
    if (config.deploymentJob != "${releaseFolder}/${product}-prod-deploy") { error('Unexpected deployment job') }
    if (env.JOB_NAME != "${releaseFolder}/${product}-auto-release") { error('Coordinator job identity mismatch') }
    properties([
        disableConcurrentBuilds(),
        parameters([
            string(name: 'REBUILD_PROD_COMMIT', defaultValue: '', description: 'Authorized disaster rebuild: exact already-published prod commit'),
            string(name: 'PUBLISHED_COORDINATOR_BUILD', defaultValue: '', description: 'Exact successful original publication coordinator'),
            string(name: 'SOURCE_BUILD', defaultValue: '', description: 'Explicit recovery: completed develop build number'),
            string(name: 'EXPECTED_COMMIT', defaultValue: '', description: 'Explicit recovery: full 40-character develop commit'),
            string(name: 'RECOVER_COORDINATOR_BUILD', defaultValue: '', description: 'Finalized deployment recovery: exact failed coordinator'),
            string(name: 'RECOVER_OWNER_BUILD', defaultValue: '', description: 'Finalized deployment recovery: exact failed owner build')
        ]),
        pipelineTriggers([upstream(upstreamProjects: "${product}/develop", threshold: 'SUCCESS')])
    ])
    def causes = currentBuild.getBuildCauses('hudson.model.Cause$UpstreamCause')
    def requestedBuild = params.SOURCE_BUILD?.toString()?.trim() ?: ''
    def requestedCommit = params.EXPECTED_COMMIT?.toString()?.trim() ?: ''
    def recoverCoordinator = params.RECOVER_COORDINATOR_BUILD?.toString()?.trim() ?: ''
    def recoverOwner = params.RECOVER_OWNER_BUILD?.toString()?.trim() ?: ''
    def deploymentRecovery = recoverCoordinator || recoverOwner
    def recoveryApprover = ''
    Integer sourceBuild
    def rebuildCommit = params.REBUILD_PROD_COMMIT?.toString()?.trim() ?: ''
    def publishedBuild = params.PUBLISHED_COORDINATOR_BUILD?.toString()?.trim() ?: ''
    def rebuilding = rebuildCommit || publishedBuild
    def rebuildAuthor = ''
    if (rebuilding) {
        def allCauses = currentBuild.getBuildCauses()
        if (!(rebuildCommit ==~ /[0-9a-f]{40}/) || !(publishedBuild ==~ /[1-9][0-9]{0,8}/) ||
            requestedBuild || requestedCommit || deploymentRecovery || allCauses.size() != 1 ||
            allCauses[0]._class != 'hudson.model.Cause$UserIdCause' || !config.approvers.contains(allCauses[0].userId) ||
            !(config.rebuildDockerEngineId ==~ /[0-9a-f-]{36}/)) {
            error('Published PROD rebuild requires one authorized user, exact publication and fixed restored Docker engine')
        }
        rebuildAuthor = allCauses[0].userId.toString()
    } else {
        if (deploymentRecovery) {
            def allCauses = currentBuild.getBuildCauses()
            if (!(recoverCoordinator ==~ /[1-9][0-9]{0,8}/) || !(recoverOwner ==~ /[1-9][0-9]{0,8}/) ||
                !requestedBuild || !requestedCommit || allCauses.size() != 1 ||
                allCauses[0]._class != 'hudson.model.Cause$UserIdCause' || !config.approvers.contains(allCauses[0].userId)) {
                error('Deployment recovery requires a unique authorized user and four exact recovery fields')
            }
            recoveryApprover = allCauses[0].userId.toString()
        }
        if (requestedBuild || requestedCommit) {
            def users = currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause')
            if (causes || users.size() != 1 || !config.approvers.contains(users[0].userId) ||
                !(requestedBuild ==~ /[1-9][0-9]{0,8}/) || !(requestedCommit ==~ /[0-9a-f]{40}/)) {
                error('Recovery requires an authorized user, exact develop build and full commit')
            }
            sourceBuild = requestedBuild as Integer
        } else {
            if (causes.size() != 1 || causes[0].upstreamProject != "${product}/develop") {
                error('A unique completed develop upstream cause is required')
            }
            sourceBuild = causes[0].upstreamBuild as Integer
        }
        // Pipeline Build Step waits through upstream post actions and retains the
        // exact queue/run relationship across controller restarts.
        def upstreamRun = waitForBuild(runId: "${product}/develop#${sourceBuild}", propagate: false)
        if (upstreamRun.result != 'SUCCESS') { error('Develop build must complete with SUCCESS before promotion') }
    }

    node(config.builderLabel.toString()) {
        dir("auto-release-${env.BUILD_NUMBER}") {
          try {
            def root = pwd()
            def control = "${root}/control"
            stage('Load Trusted Release Controls') {
                ['release-gate.py', 'release-evidence.py', 'release-promotion.py', 'release-preflight.py',
                 'release-finalization.py', 'release-finalize.sh', 'error-handler.sh', 'nexus-upload.sh', 'git-tag.sh',
                 'harbor-vulnerability-report.py', 'release-askpass.sh', 'release-recovery.py', 'release-rebuild.py'].each { name ->
                    writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                }
                def commonStages = ['Checkout', 'Load Scripts', 'Detect', 'Early Capacity Admission', 'Secret Scan', 'Build', 'Test',
                    'Fast Contract Test', 'Dependency Scan', 'Package / Publish / Tag', 'Docker Build',
                    'Image Scan', 'Harbor Push', 'Harbor Vulnerability Report', 'Smoke Test',
                    'Deployment Verification — k3s', 'Declarative: Post Actions']
                def developStages = ['Checkout', 'Load Scripts', 'Detect', 'Secret Scan', 'Build', 'Test',
                    'Package / Publish / Tag', 'Declarative: Post Actions']
                writeFile file: 'control/policy.json', text: JsonOutput.toJson([
                    schema_version: 1, product: product, library_revision: config.libraryRevision,
                    jobs: [promotion: "${product}/develop", deployment: "${product}/prod"],
                    candidate_mode: true, develop_mode: 'lean-success-v1',
                    waivable_stages: ['Test', 'Fast Contract Test', 'Dependency Scan', 'Image Scan'],
                    required_stages: [promotion: developStages, deployment: commonStages],
                    required_scanners: ['trivy', 'govulncheck', 'harbor'], max_evidence_age_seconds: 3600,
                    not_applicable_advisories: [[id: 'GO-2026-5932',
                        affected_package_prefix: 'golang.org/x/crypto/openpgp',
                        required_package_graphs: ['linux-arm64-nodynamic-tests',
                            'linux-arm64-devseed-nodynamic-tests', 'linux-arm64-nodynamic-server'],
                        require_no_govuln_affected_package_finding: true]],
                    max_exception_seconds: 900, approvers: config.approvers,
                    revoked_approval_ids: config.revokedApprovalIds ?: []
                ])
                // Resolve every later credential before scanning or claiming a release.
                // Existence alone is insufficient for Secret File credentials: a
                // malformed XML import can bind successfully to an empty key file.
                withCredentials([
                    usernamePassword(credentialsId: config.jenkinsReadCredentials,
                        usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN'),
                    usernamePassword(credentialsId: config.harborCredentials,
                        usernameVariable: 'PREFLIGHT_HARBOR_USER', passwordVariable: 'PREFLIGHT_HARBOR_PASSWORD'),
                    usernamePassword(credentialsId: config.scmCredentials,
                        usernameVariable: 'PREFLIGHT_SCM_USER', passwordVariable: 'PREFLIGHT_SCM_PASSWORD'),
                    usernamePassword(credentialsId: config.mergeCredentials,
                        usernameVariable: 'PREFLIGHT_MERGE_USER', passwordVariable: 'PREFLIGHT_MERGE_PASSWORD'),
                    usernamePassword(credentialsId: config.finalizationWriterCredentials,
                        usernameVariable: 'PREFLIGHT_WRITER_USER', passwordVariable: 'PREFLIGHT_WRITER_PASSWORD'),
                    usernamePassword(credentialsId: config.nexusCredentials,
                        usernameVariable: 'PREFLIGHT_NEXUS_USER', passwordVariable: 'PREFLIGHT_NEXUS_PASSWORD'),
                    file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE'),
                    file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                    withEnv(["RELEASE_JENKINS_URL=${config.jenkinsApiUrl}", "RELEASE_DEPLOY_LABEL=${config.deploymentNodeLabel}"]) {
                        sh 'python3 control/release-preflight.py'
                        sh 'python3 control/release-evidence.py check-routing --jenkins-url "$RELEASE_JENKINS_URL" --deployment-label "$RELEASE_DEPLOY_LABEL"'
                    }
                }
            }

            if (deploymentRecovery) {
                // A finalized artifact is never rebuilt, re-promoted or re-tagged.
                // Original gates and their original expiration remain in force.
                withEnv(["RECOVERY_COORDINATOR=${recoverCoordinator}", "RECOVERY_OWNER=${recoverOwner}",
                         "RECOVERY_SOURCE_BUILD=${sourceBuild}", "RECOVERY_SOURCE_COMMIT=${requestedCommit}",
                         "RECOVERY_APPROVER=${recoveryApprover}", "RELEASE_JENKINS_URL=${config.jenkinsApiUrl}",
                         "NEXUS_BASE_URL=${config.nexusBaseUrl}", "GIT_ASKPASS=${control}/release-askpass.sh",
                         'GIT_TERMINAL_PROMPT=0', 'PYTHONDONTWRITEBYTECODE=1']) {
                    stage('Revalidate Original Finalized Release') {
                        withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                            usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN'),
                            file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                            sh '''python3 control/release-recovery.py collect --root recovery --policy control/policy.json \
                                --receipt-key-file "$RECEIPT_KEY_FILE" --jenkins-url "$RELEASE_JENKINS_URL" \
                                --coordinator-build "$RECOVERY_COORDINATOR" --owner-build "$RECOVERY_OWNER" \
                                --source-build "$RECOVERY_SOURCE_BUILD" --expected-commit "$RECOVERY_SOURCE_COMMIT"'''
                        }
                    }
                    def recovered = parseReleaseJson(readFile('recovery/recovery-identity.json'))
                    stage('Verify Existing Published Artifact') {
                        dir('recovery-source') {
                            checkout([$class: 'GitSCM', branches: [[name: recovered.commit]],
                                userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git',
                                    credentialsId: config.scmCredentials]]])
                        }
                        withCredentials([file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE'),
                            usernamePassword(credentialsId: config.scmCredentials,
                                usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD'),
                            usernamePassword(credentialsId: config.nexusCredentials,
                                usernameVariable: 'NEXUS_CRED_USR', passwordVariable: 'NEXUS_CRED_PSW')]) {
                            sh '''chmod 700 "$GIT_ASKPASS"
python3 control/release-recovery.py handoff --root recovery --source recovery-source --policy control/policy.json \
    --receipt-key-file "$RECEIPT_KEY_FILE" --approver "$RECOVERY_APPROVER" --output deployment-request.json'''
                        }
                        archiveArtifacts artifacts: 'deployment-request.json,recovery/**/*.json', allowEmptyArchive: false
                    }
                    stage('PROD Runtime Deployment') {
                        def deployed = build(job: config.deploymentJob, wait: true, propagate: false,
                            parameters: [text(name: 'SIGNED_RELEASE_REQUEST', value: readFile('deployment-request.json'))])
                        if (deployed.result != 'SUCCESS') { error('Recovered runtime deployment failed; preserve both receipts') }
                        currentBuild.description = "recovered coordinator #${recoverCoordinator}; prod ${recovered.commit.take(12)}; deploy #${deployed.number}"
                    }
                }
                return
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
                            if (branch == 'develop') {
                                sh '''python3 "$RELEASE_CONTROL/release-evidence.py" inspect \
                                    --jenkins-url "$RELEASE_JENKINS_URL" --branch develop \
                                    --build "$RELEASE_BUILD" --lean-develop --output "$RELEASE_PHASE/identity.json"'''
                            } else {
                                sh '''python3 "$RELEASE_CONTROL/release-evidence.py" inspect \
                                    --jenkins-url "$RELEASE_JENKINS_URL" --branch prod \
                                    --build "$RELEASE_BUILD" --candidate-root "$RELEASE_PHASE/evidence" --output "$RELEASE_PHASE/identity.json"'''
                            }
                        }
                        def identity = parseReleaseJson(readFile("${phase}/identity.json"))
                        if (expectedCommit && identity.commit != expectedCommit) { error('Checkout differs from expected release commit') }
                        dir("${phase}/source") {
                            checkout([$class: 'GitSCM', branches: [[name: identity.commit]],
                                userRemoteConfigs: [[url: 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git',
                                    credentialsId: config.scmCredentials]]])
                        }
                        if (branch == 'develop') {
                            sh '''python3 "$RELEASE_CONTROL/release-evidence.py" bind-source \
                                --identity "$RELEASE_PHASE/identity.json" --source "$RELEASE_PHASE/source" \
                                --output "$RELEASE_PHASE/evidence"'''
                        }
                    }
                    if (branch == 'prod') {
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
                    }
                    stage("${branch}: Release Gate") {
                        sh '''python3 "$RELEASE_CONTROL/release-promotion.py" review \
                            --evidence "$RELEASE_PHASE/evidence/evidence.json" --policy "$RELEASE_CONTROL/policy.json" \
                            --output "$RELEASE_PHASE/review.json"'''
                        archiveArtifacts artifacts: "${branch}/evidence/*.json,${branch}/review.json", allowEmptyArchive: false
                        def review = parseReleaseJson(readFile("${phase}/review.json"))
                        if (branch == 'develop') {
                            if (review.decision != 'PASS') { error('Lean develop SUCCESS gate blocked') }
                        } else if (review.decision == 'NEEDS_APPROVAL') {
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
                return parseReleaseJson(readFile("${phase}/identity.json"))
            }

            if (rebuilding) {
                stage('Load Successful Published PROD Provenance') {
                    withCredentials([usernamePassword(credentialsId: config.jenkinsReadCredentials,
                        usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN'),
                        file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE')]) {
                        withEnv(["REBUILD_JENKINS_URL=${config.jenkinsApiUrl}", "REBUILD_PUBLISHED=${publishedBuild}",
                                 "REBUILD_COMMIT=${rebuildCommit}"]) {
                            sh 'python3 control/release-rebuild.py collect --root published --jenkins-url "$REBUILD_JENKINS_URL" --coordinator-build "$REBUILD_PUBLISHED" --expected-commit "$REBUILD_COMMIT" --receipt-key-file "$RECEIPT_KEY_FILE"'
                        }
                    }
                    archiveArtifacts artifacts: 'published/published.json', allowEmptyArchive: false
                }
                def rebuilt
                stage('Rebuild Published PROD Through Full CI/CD') {
                    rebuilt = build(job: "${product}/prod", wait: true, propagate: false, quietPeriod: 0)
                    if (!(rebuilt.result in ['SUCCESS', 'UNSTABLE'])) { error('Rebuilt PROD CI/CD failed; no deployment') }
                }
                verifyPhase('prod', rebuilt.number as Integer, rebuildCommit)
                stage('Authorize Published PROD Rebuild Handoff') {
                    withCredentials([usernamePassword(credentialsId: config.scmCredentials,
                        usernameVariable: 'RELEASE_GIT_USER', passwordVariable: 'RELEASE_GIT_PASSWORD'),
                        usernamePassword(credentialsId: config.nexusCredentials,
                        usernameVariable: 'NEXUS_CRED_USR', passwordVariable: 'NEXUS_CRED_PSW'),
                        file(credentialsId: config.receiptKeyCredentials, variable: 'RECEIPT_KEY_FILE'),
                        file(credentialsId: config.approvalKeyCredentials, variable: 'APPROVAL_KEY_FILE')]) {
                        withEnv(["REBUILD_APPROVER=${rebuildAuthor}", "REBUILD_ENGINE=${config.rebuildDockerEngineId}",
                                 "NEXUS_BASE_URL=${config.nexusBaseUrl}", "GIT_ASKPASS=${control}/release-askpass.sh",
                                 'GIT_TERMINAL_PROMPT=0']) {
                            sh '''#!/usr/bin/env bash
set -euo pipefail
chmod 700 "$GIT_ASKPASS"
approval=()
if [[ -f prod/approval.json ]]; then approval=(--approval prod/approval.json); fi
python3 control/release-rebuild.py handoff --root published --source prod/source \
    --evidence prod/evidence/evidence.json --policy control/policy.json \
    --receipt-key-file "$RECEIPT_KEY_FILE" --approval-key-file "$APPROVAL_KEY_FILE" \
    --approver "$REBUILD_APPROVER" --engine-id "$REBUILD_ENGINE" "${approval[@]}" --output deployment-request.json
'''
                        }
                    }
                    archiveArtifacts artifacts: 'deployment-request.json', allowEmptyArchive: false
                }
                stage('PROD Runtime Disaster Restore') {
                    def deployed = build(job: config.deploymentJob, wait: true, propagate: false,
                        parameters: [text(name: 'SIGNED_RELEASE_REQUEST', value: readFile('deployment-request.json'))])
                    if (deployed.result != 'SUCCESS') { error('Published PROD restore failed; preserve the new attempt') }
                    currentBuild.description = "rebuilt prod ${rebuildCommit.take(12)}; CI #${rebuilt.number}; deploy #${deployed.number}"
                }
                return
            }

            verifyPhase('develop', sourceBuild, requestedCommit ?: null)
            stage('Promote Verified Commit') {
                def promotionCommand = requestedBuild ? 'recover' : 'promote'
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
python3 control/release-promotion.py ''' + promotionCommand + ''' --source develop/source --state-directory "$RELEASE_STATE" \
    --evidence develop/evidence/evidence.json --policy control/policy.json \
    --approval-key-file "$APPROVAL_KEY_FILE" --receipt-key-file "$RECEIPT_KEY_FILE" \
    "${approval[@]}" --output promotion.json
'''
                    }
                }
                archiveArtifacts artifacts: 'promotion.json', allowEmptyArchive: false
            }
            def promotion = parseReleaseJson(readFile('promotion.json'))
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
          } finally {
            stage('Archive Evidence and Release Workspace') {
                // Preserve only release evidence, never source checkouts, scanner
                // caches or credential files. If archival fails, retain the
                // workspace for diagnosis instead of destroying the only copy.
                archiveArtifacts artifacts: 'control/policy.json,develop/identity.json,prod/identity.json,develop/review.json,prod/review.json,develop/approval.json,prod/approval.json,develop/evidence/*.json,develop/evidence/*.jsonl,prod/evidence/*.json,prod/evidence/*.jsonl,develop/evidence/.pipeline/candidate*,prod/evidence/.pipeline/candidate*,develop/evidence/.pipeline/candidate-stages/*,prod/evidence/.pipeline/candidate-stages/*,develop/evidence/reports/junit/*,prod/evidence/reports/junit/*,promotion.json,finalization.json,deployment-request.json,prod/source/.pipeline/release-manifest.env,recovery/**,published/**', allowEmptyArchive: true
                // This dir is scoped to this exact build, not the agent root,
                // persistent dependency cache, release state or runtime data.
                deleteDir()
            }
          }
        }
    }
}
