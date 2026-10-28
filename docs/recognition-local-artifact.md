# LOCAL Recognition artifact-only owner lane

This is a centrally owned standalone job definition, not a new branch-discovery
rule. Installing/enabling/running it remains a separate authorized action.
It never calls `ciPipeline`, the Recognition branch Jenkinsfile, a release
coordinator, PROD deployment, registry push, Git push/tag, Factory or inference.

The sole source is Recognition commit
`9d0400d003de68be827524d21543200bf78bfdfd`, version
`0.1.11-local-autorecovery-20261028`. A different runtime source requires a
reviewed definition change. The job is
`shiba-local-artifacts/recognition-autorecovery`, one approved manual user cause,
no parameters, no triggers and no concurrent runs. It must load an immutable
reviewed Shared Library commit. There are no release-writer, Harbor, signing or
PROD credentials; checkout uses only existing `github-scm-readonly`.

## Agents and tools

The first phase uses existing `ci-untrusted`: Linux arm64, nonroot, no Docker
socket/remote Docker endpoint and no host controller/product mounts. The central
SDK helper is byte-identical to the already reviewed Recognition 0.1.21 official
SDK installer: Go 1.26.10, linux-arm64 archive SHA
`3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797`.
It uses a private workspace cache, checks official HTTPS archive bytes and tool
hashes, and disables automatic toolchain switching. The source needs Go 1.26.6.
Module downloads use only the existing Nexus `go-group` URL, with checksum
verification and no direct fallback. Missing repository access fails the build.
Private GOCACHE/GOMODCACHE/TMPDIR are separate from the clean source checkout.

Only the five explicit package patterns in the builder are tested. They cover
the seven runtime/transport/adapter packages; no benchmark/Factory package is
compiled or run. Tests are bounded synthetic backend/private-file contracts.
Build is CGO-disabled Linux arm64, trimpath, with embedded exact Git revision and
`vcs.modified=false`. A dirty source, SDK drift or failed test emits no successful
receipt. The producer claims no PROD vulnerability/release qualification.

The second phase uses only existing `ci-image-builder`, with an explicitly
reviewed Docker Engine UUID. It receives only the ELF archive/manifest/receipt,
bound by the first phase's raw receipt SHA, through Jenkins stash. It does not
checkout or execute product source. The central helper is loaded again from the
same pinned library. Docker credentials use a fresh empty private DOCKER_CONFIG.

The Dockerfile is a fixed central constant, not `source/Dockerfile`: the same
distroless nonroot base pinned at
`sha256:1b7b9f0f0e0a1d2155f531db587cc48ec26aaf97ab64364225f5bf18a054e66a`,
one COPY of the verified ELF, fixed entrypoint/healthcheck and source/library/
LOCAL scope labels. Build uses `--network none --pull=false`; the approved arm64
base must already be cached. There is no automatic infrastructure repair,
pull, cache prune, image run or deployment. A missing base is an activation/build
prerequisite blocker, not permission to obtain or replace one.

The helper checks the base and target RootFS prefix, nonroot/entrypoint/platform/
labels, immutable image ID and unchanged Engine UUID, then performs `docker save`
once. It verifies the saved config's raw SHA equals ImageID, all layer diff IDs,
and that the new COPY layer contains exactly the bound Recognition binary.
ImageID is a config digest, not a registry manifest/repository digest. There is
no registry push or claim of a registry digest.

## Artifacts and trusted readback

The first phase archives `artifact/recognition-local.tar`, its manifest/receipt,
SHA256SUMS and contracts log. The final phase archives
`image-artifact/recognition-local-image.tar`, `image-receipt.json` and SHA256SUMS.
Jenkins fingerprints them. A partial first phase is not a successful final build.

The image receipt has schema 1, scope `LOCAL_RECOGNITION_DOCKER_IMAGE`, exact
`identity` (job/build/source/library/user), version/runtime_version, raw archive
SHA/size, image ID/config SHA/platform/nonroot/entrypoint/labels/base reference,
the ELF archive/receipt/manifest/binary SHA chain and explicit false deployment,
registry_push and prod_downstream fields. It has no new HMAC/signing service.

Use the central `recognition-local-artifact.py collect` on the authorized owner
with the existing `JENKINS_API_USER` and `JENKINS_API_TOKEN` in process memory.
The collector only GETs fixed paths for one numeric build on the existing local
Jenkins origin. Redirects/proxies are disabled. It requires completed `SUCCESS`,
the exact source checkout BuildData, exact Library repository/SHA BuildData plus
one trusted `jenkins-pipeline` LibrariesAction at that same SHA, approved manual
cause and all five workflow
stages `SUCCESS`, before accepting its artifact whitelist. `lastBuild`, arbitrary
job paths and a self-authored receipt are not accepted authorities.

The collector requests explicit exported `remoteUrls`, `lastBuiltRevision.SHA1`
and `libraries.{name,version,trusted}` fields from that historical build API.
Missing or conflicting fields fail closed before artifact download. A compact
archived API response which only contains SHA values without their repositories,
or current job XML changed after the build, does not establish Library identity.
The Library repository must be exactly
`https://github.com/Shiba-Jenkins-Groups/jenkins-pipeline-scripts.git`.

The collector rechecks ELF/image/config/layers/binary bindings and fsyncs a new
private directory containing the artifacts, raw API/workflow evidence and
`trusted-build-evidence.json`. Required evidence keys are schema_version 1,
scope, collect_finished/pipeline_final_success true, result SUCCESS, identity,
image_receipt_sha256, archive_sha256/archive_size, api_sha256/wfapi_sha256 and
image_id. The raw evidence/receipt SHA values become owner plan pins.

`verify` and `verify-image` alone check integrity, not Jenkins authenticity.
They do not turn an arbitrary local JSON file into CI evidence. No auth secret
is persisted or printed. Collection never triggers a build, loads an image or
touches a product database.

## Complete managed consumption chain

1. Publish the reviewed exact runtime and library commits only after separately
   authorized Git scope. Render a disabled job XML with the library commit,
   actual allowed Jenkins user and reviewed builder Engine UUID. The renderer
   contacts no Jenkins. Both XML disabled and pipeline enabled=false remain
   closed until the operator approves the concrete registered job/config hash.
2. Confirm the managed agents, readonly SCM credential, official SDK download,
   Nexus go-group and approved cached base. Enable the reviewed manual-only job
   and perform its authorized single build. Read that exact complete result.
3. Run the authenticated exact-build collector. Keep its raw receipt/evidence/
   archive hashes, source/library/build identity and config ImageID together.
4. Use the separately reviewed API managed owner entry
   `scripts/service-control-local-recognition.py`. Its plan binds the two raw
   CI receipt/evidence hashes and image archive SHA to the exact old LOCAL
   container/Engine, source/version/image ID, owner config/installation/journal,
   current Manifest authority/revision/SHA and all consumer identities. Do not
   hand-copy a binary into a live container or call terminal Compose as a bypass.
5. That executor must hold the shared resource EX/pending and lifecycle/journal
   controls, require the LOCAL owner unloaded, and preserve all accepted-work
   drains plus the separate Recognition self lease. The reviewed initial path
   supports only the known stopped old container; running/unknown state blocks.
   It verifies Docker-save config/layer/binary bindings before its once-only
   load. It verifies the resulting immutable ImageID. The old exact container is
   retired with one restart=no update and preserved rename, then one fixed-name
   create/start, preserving approved topology, credentials, mounts, restart and
   consumer state. Any unknown result retains the operation/drains; no retry or
   implicit rollback. This executor is delivered/reviewed separately; this
   artifact job does not grant or perform those operations.
6. Before releasing the self lease, metadata GETs must prove exact new image/CID
   and preserved current Manifest/profile/authority/revision. A runtime-only
   compatibility handoff must be explicitly applicable; if the LOCAL publication
   contract instead requires a new signed runtime component, that handoff is a
   blocker. Never claim the old certified binary hash identifies the new ELF.
   Commit the owner receipt/rebind against the new target before reopening work.
7. Only the authorized owner releases the self lease after the App/OCR handoff.
   Background preparation can then run under its own activity admission.
   Acceptance is the optional warmup state with explicit completion/positive
   metrics and exact selected 9B digest/positive context; metadata ready or
   resident alone is insufficient. Unknown warmup state remains unresolved and
   nonzero activity. No user image is submitted as a recovery probe.

The job source and fixtures do not install either executor or owner configuration.
No runtime activation is claimed until both CI and the separately managed
handoff prerequisites succeed. Missing credentials, published pins, cached base,
consumer topology, owner executor artifact or applicable Manifest handoff remain
explicit blockers, never permission for manual deployment.

## Offline verification

Python fake-agent/compiler/Docker/archive/collector fixtures and plain Groovy
control-flow fixtures cover failures without network or runtime operations. The
same Groovy fixture also compiles the actual function with installed Jenkins CPS
and sandbox/Guava dependencies. It never executes Pipeline steps in Jenkins.
No ordinary product release/scanner/runtime checks are rerun by these tests.
