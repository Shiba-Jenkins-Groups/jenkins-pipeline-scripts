// Stage 3a only: real Jenkins execution of OFFLINE release contracts.
// No application checkout, credentials, GitHub push, registry or runtime access.
def call(Map config = [:]) {
    if (config.enabled != true) { error('DEV contract rehearsal is disabled') }
    if (env.JOB_NAME != 'shiba-go-ditch-api-project-dev-contract-rehearsal') {
        error('Unexpected DEV rehearsal job')
    }
    if (!(config.libraryRevision ==~ /[0-9a-f]{40}/)) { error('Reviewed library SHA required') }
    properties([disableConcurrentBuilds(), buildDiscarder(logRotator(numToKeepStr: '10'))])
    timeout(time: 10, unit: 'MINUTES') {
        node('ci-untrusted') {
            dir("dev-contract-${env.BUILD_NUMBER}") {
                try {
                    stage('Verify Nonprivileged DEV Test Agent') {
                        sh '''#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 ]]
[[ ! -S /var/run/docker.sock && ! -S /run/docker.sock ]]
[[ -z "${DOCKER_HOST:-}" ]]
[[ ! -e /var/jenkins_home && ! -e /Users/surpend/Developer ]]
python3 --version
git --version
'''
                    }
                    stage('Load Pinned Offline Contract Suite') {
                        ['release-gate.py', 'release-evidence.py', 'release-promotion.py',
                         'release-finalization.py', 'release-deploy.py', 'release-candidate.py',
                         'release-gate.test.py', 'release-control.test.py',
                         'release-candidate.test.py', 'release-deploy.test.py'].each { name ->
                            writeFile file: "suite/${name}", text: libraryResource("scripts/common/${name}")
                        }
                        writeFile file: 'reviewed-library.txt', text: config.libraryRevision + '\n'
                    }
                    stage('Offline Gates and Mocked Promotion Deployment') {
                        sh '''#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0
mkdir -p reports
for suite in release-gate release-control release-candidate release-deploy; do
  python3 "suite/${suite}.test.py" 2>&1 | tee "reports/${suite}.log"
done
python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path('.')
receipt={'scope':'DEV_CONTRACT_ONLY','runtime_deployed':False,'prod_enabled':False,
         'result':'SUCCESS','library_revision':(root/'reviewed-library.txt').read_text().strip(),
         'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'suite').glob('*.py'))}}
(root/'reports/contract-receipt.json').write_text(json.dumps(receipt,indent=2)+chr(10))
PY
'''
                    }
                    currentBuild.description = 'DEV contracts only; no runtime deployment or PROD enablement'
                } finally {
                    archiveArtifacts artifacts: 'reviewed-library.txt,reports/*', allowEmptyArchive: true
                }
            }
        }
    }
}
