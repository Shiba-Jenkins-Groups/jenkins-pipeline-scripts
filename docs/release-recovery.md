# App release recovery

The normal path remains develop branch CI, trusted coordinator verification,
prod merge and CI, then the signed PROD owner deployment job.

The macOS deployment agent must be managed by launchd, not by a terminal owned
by a conversation. `resources/scripts/common/install-prod-owner-agent.py` renders
the configuration by default; `--install` loads it for the existing logged-in
user. Supply the existing `--java` executable and `--agent-root`. It preserves
the existing private JNLP, refuses differing installed configuration, and does
not change Jenkins node identity or credentials. This is a user LaunchAgent:
logout stops it; a deployment requires that this user be logged in.

For an authorized retry after an infrastructure issue, submit SOURCE_BUILD and
EXPECTED_COMMIT to the fixed auto-release job. Both fields are required together.
Only a configured approver with a UserIdCause may use this path. The coordinator
loads the exact completed develop build and rejects a differing commit before
promotion. It still rechecks evidence freshness, every stage, scanners, approvals,
release state and signed deployment receipts. It never promotes an arbitrary
commit supplied by the caller, and does not turn failed CI into success.

The fixed job must expose these two String parameters before its first recovery
run; the Pipeline maintains them thereafter. Update the trusted library pin in
the managed job configuration when deploying this change. Normal upstream
triggers leave both parameters empty.

Verification: run release-pipelines.test.groovy with the existing Groovy runtime
and CPS annotation jar; run each release-*.test.py directly (their dotted names
are not discovered by unittest discovery). Confirm launchd and Jenkins agree
that the single deployment node is running and online across separate sessions.
