import com.cloudbees.groovy.cps.NonCPS
import groovy.json.JsonSlurperClassic

@NonCPS
def decode(String text) {
    new JsonSlurperClassic().parseText(text)
}

def call(Map config = [:]) {
    def job = 'shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-115'
    if (config.enabled != true || env.JOB_NAME != job || !(config.libraryRevision ==~ /[0-9a-f]{40}/)) {
        error('Owner #115 requires the fixed enabled recovery job and exact Library revision')
    }
    def causes = currentBuild.getBuildCauses()
    if (causes.size() != 1 || causes[0]._class != 'hudson.model.Cause$UserIdCause' ||
        causes[0].userId != 'claude-code-use') {
        error('Owner #115 recovery requires a direct build by the release operator')
    }
    def closureOnly = config.closureOnly == true
    if (params || env.BUILD_NUMBER != (closureOnly ? '2' : '1')) {
        error('Owner #115 permits only the exact scoped build with no parameters')
    }

    def originalControl = ['release-gate.py', 'release-evidence.py', 'release-promotion.py',
                           'release-deploy.py', 'release-app-binding.py', 'release-recovery.py',
                           'release-rebuild.py']
    def recovery = ['release-app-binding-115-evidence.py', 'release-app-binding-115-recovery.py',
                    'release-app-binding-115-runtime.py', 'release-app-binding-admitted-115.py']
    if (closureOnly) { recovery.add('release-app-binding-115-closure.py') }
    def commit = '84c2b0a6eae1fb74944af0083859450d321dbcbd'
    def scmUrl = 'https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git'

    timeout(time: 15, unit: 'MINUTES') {
        node('shiba-prod-owner-macos') {
            dir("app-binding-115-${env.BUILD_NUMBER}") {
                try {
                    stage('Load Exact Recovery and App Sources') {
                        (originalControl + recovery).each { name ->
                            writeFile file: "control/${name}", text: libraryResource("scripts/common/${name}")
                        }
                        writeFile file: 'reviewed-library.txt', text: config.libraryRevision + '\n'
                        dir('source') {
                            checkout([$class: 'GitSCM', branches: [[name: commit]],
                                      userRemoteConfigs: [[url: scmUrl, credentialsId: 'github-scm-readonly']]])
                        }
                    }

                    withCredentials([file(credentialsId: 'shiba-auto-release-receipt-key', variable: 'RELEASE_SIGNING_KEY'),
                        usernamePassword(credentialsId: 'shiba-auto-release-jenkins-read',
                                         usernameVariable: 'JENKINS_API_USER', passwordVariable: 'JENKINS_API_TOKEN')]) {
                        withEnv(['RELEASE_JENKINS_URL=http://127.0.0.1:9090', 'PYTHONDONTWRITEBYTECODE=1',
                                 "RECOVERY_LIBRARY_REVISION=${config.libraryRevision}"]) {
                            if (closureOnly) {
                                stage('Verify Completed #115 Binding Without Applying') {
                                    sh '''python3 control/release-app-binding-115-recovery.py --mode close \
    --source "$PWD/source" --control "$PWD/control" --receipt "$PWD/closure-receipt.json"'''
                                }
                            } else {
                                stage('Verify Signed #115 and Running App') {
                                    sh '''python3 control/release-app-binding-115-recovery.py --mode verify \
    --source "$PWD/source" --control "$PWD/control" --receipt "$PWD/verify-receipt.json"'''
                                }
                                stage('Complete Existing Owner #115 Binding') {
                                    sh '''python3 control/release-app-binding-115-recovery.py --mode apply \
    --source "$PWD/source" --control "$PWD/control" --receipt "$PWD/recovery-receipt.json"'''
                                }
                            }
                        }
                    }
                    def result = decode(readFile(closureOnly ? 'closure-receipt.json' : 'recovery-receipt.json')).payload
                    if (result.status != 'SUCCESS') { error('Owner #115 signed recovery did not complete') }
                    currentBuild.description = closureOnly ? 'Read-only #115 binding closeout for v1.1.39' : 'PROD owner #115 binding recovery for v1.1.39'
                } finally {
                    archiveArtifacts artifacts: 'reviewed-library.txt,verify-receipt.json,recovery-receipt.json,closure-receipt.json',
                                     allowEmptyArchive: true
                }
            }
        }
    }
}
