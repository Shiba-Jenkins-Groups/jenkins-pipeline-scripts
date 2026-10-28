# App binding owner routing proposal

This is a LOCAL configuration proposal. It has not changed JCasC, a Jenkins
job, credentials, triggers, a runtime, or any published App artifact.

The observed JCasC source is
`/Users/surpend/Developer/jenkins/config/system/jenkins.yaml`. That runtime
directory is outside the library Git repository. The versioned
`app-binding-owner-routing.template.patch` records the narrow proposed change
from the observed `f5c68a5f0276f542754c69b9854f09be8b484011` routing. The separate
`shiba-jenkins-activation.patch` is historical and is not part of this proposal.

Replace all four `REVIEWED_LIBRARY_COMMIT_40_HEX` placeholders with the same
reviewed, published 40-character library commit before a separately authorized
configuration deployment. An unresolved placeholder is not an executable
activation plan. Review the rendered patch against the current configuration;
pin drift must stop the handoff rather than overwrite newer configuration.

The four pins are the coordinator's `@Library` and `libraryRevision`, and the
owner's `@Library` and `libraryRevision`. The latter lets the owner validate the
request against the configured loaded revision. Both central jobs retain their
existing identity, credentials, scopes, node, runtime root and Docker engine.
The coordinator retains its existing develop SUCCESS upstream trigger. This
proposal adds no scheduled or automatic repair trigger.

Both coordinator paths pass the expected revision to the read-only routing
preflight. It fetches the fixed owner job's `config.xml` and requires its literal
`@Library` and `libraryRevision` to match before maintenance or a normal release.
Partial activation, duplicate declarations, dynamic expressions and missing
pins fail closed. Groovy comments are ignored rather than counted as a pin.

The coordinator's three explicit maintenance parameters are
`APP_BINDING_REPAIR_COORDINATOR_BUILD`, `APP_BINDING_REPAIR_OWNER_BUILD` and
`APP_BINDING_REPAIR_PINS`. The first two identify the original successful
release; the text parameter carries the four exact reviewed raw SHA256 pins.
The trusted coordinator and owner implementation remains responsible for a
short-lived signed request, one-attempt claim and verification of the original
signed deployment provenance. A pin or prior receipt is not permission to
redeploy, rescan, rebuild, change models or grant App lifecycle control.

Normal deployment continues to load `release-app-binding.py` before inspecting
the signed request and checking out the exact source. The existing owner
transaction owns admission and App identity handoff across deployment. Missing
resources or an incomplete binding commit fail before a successful owner
receipt. A maintenance route must use its separate binding transaction and skip
normal App checkout and deployment.

LOCAL fixture checks:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-owner-routing.test.py
PYTHONDONTWRITEBYTECODE=1 python3 resources/scripts/common/release-owner-library-routing.test.py
# Plain Groovy 2.4 plus the existing groovy-cps annotation dependency:
# groovy.ui.GroovyMain resources/scripts/common/release-normal-app-binding.test.groovy .
# groovy.ui.GroovyMain resources/scripts/common/release-binding-repair-flow.test.groovy .
```

The Python checks apply the rendered patch only to an inactive private text
fixture. The Groovy checks execute the real owner Pipeline with mocked Jenkins
steps, including the coordinator's explicit maintenance route. These checks do
not access Jenkins, a host service, credentials or a database. The coordinator
mock verifies exact transport of a synthetic collector handoff; Python fixtures
own signature, provenance and actual expiry validation.
