"""Verify fixed Jenkins evidence for the interrupted PROD owner #115 binding."""

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat


def load(filename):
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("release-deploy")
adapter = load("release-evidence")
control, gate, require = deploy.control, deploy.gate, deploy.require

JENKINS_URL = "http://127.0.0.1:9090"
PRODUCT = "shiba-go-ditch-api-project"
COORDINATOR = "shiba-release-automation/" + PRODUCT + "-auto-release"
OWNER = "shiba-release-automation/" + PRODUCT + "-prod-deploy"
COORDINATOR_BUILD = 257
OWNER_BUILD = 115
DEVELOP_BUILD = 406
PROD_BUILD = 238
LIBRARY = "6b799683623af09719aecf11d8f37929c04956ee"
DEVELOP_COMMIT = "5311565c40eebf320f3262018b03754030859b90"
COMMIT = "84c2b0a6eae1fb74944af0083859450d321dbcbd"
VERSION = "1.1.39"
DIGEST = "sha256:8a83cd3ea683caf29e3c44952061970618e53c2fdb1da595e85f5393eb9fde89"
REQUEST_SHA256 = "7c70fa604e37979ef9241abbd78c7f5e19420f034467b401ee2c471429681705"
REQUEST_PAYLOAD_SHA256 = "dbe45501af1cbea6e9ceeab293a38468d871b64a0a5051df45d52325214b41b9"
FAILED_SHA256 = "655d8944a99e8cee367e39505fc7bb72c5cb014d19d616dcd5808b23e97173ed"
FINALIZATION_SHA256 = "aaaaa24a637563052bf856be1a1218c5c510cae3846fd566ec2a89777c4188f0"
PROMOTION_SHA256 = "ffbfc78931a3fa7a34af78d0da6d188c6a4f68ae42d43e40502674241f43e4a0"
PREVIOUS_CONTAINER_ID = "1828f5def8c53dcf6797fa5f112cb6260e4db968e418f74434a04c14cf851ca8"
PREVIOUS_IMAGE_ID = "sha256:856bad5166318ae0b7f5326e0bf478eabea4c5ef65af73651bacd806a95f7596"

BUILD_TREE = "api/json?tree=number,building,result,actions[causes[_class,upstreamBuild,upstreamProject]]"
UPSTREAM_CAUSES = {"hudson.model.Cause$UpstreamCause", "org.jenkinsci.plugins.workflow.support.steps.build.BuildUpstreamCause"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pairs(items):
    result = {}
    for name, value in items:
        require(name not in result, "duplicate Jenkins evidence field")
        result[name] = value
    return result


def decode(raw):
    require(isinstance(raw, bytes) and 0 < len(raw) <= 65536, "unexpected Jenkins recovery evidence size")
    data = json.loads(raw, object_pairs_hook=pairs)
    require(isinstance(data, dict), "unexpected Jenkins evidence format")
    return data


def prefix(job, number):
    return "/" + "/".join("job/" + part for part in job.split("/")) + f"/{number}/"


def completed(read, job, number, expected, upstream=None):
    build = decode(read(prefix(job, number) + BUILD_TREE))
    require(type(build["number"]) is int and build["number"] == number
            and build["building"] is False and build["result"] == expected,
            "unexpected Jenkins recovery build result")
    if upstream is not None:
        causes = [cause for action in build["actions"] for cause in action.get("causes", [])]
        require(len(causes) == 1 and causes[0]["_class"] in UPSTREAM_CAUSES
                and (causes[0]["upstreamProject"], causes[0]["upstreamBuild"]) == upstream,
                "unexpected Jenkins recovery upstream")


def envelope(raw, expected_hash):
    require(sha(raw) == expected_hash, "original Jenkins artifact digest changed")
    data = decode(raw)
    require(set(data) == {"payload", "signature"} and isinstance(data["payload"], dict)
            and isinstance(data["signature"], str) and deploy.SHA256.fullmatch(data["signature"]),
            "invalid signed Jenkins evidence envelope")
    return data


def collect(url, key, get=adapter.jenkins_get, now=None):
    """Return (request, failed_receipt, finalization, lineage); never renew the old grant."""
    require(url == JENKINS_URL, "unexpected recovery Jenkins API")

    def read(path):
        return get(url, path)

    develop_job = PRODUCT + "/develop"
    prod_job = PRODUCT + "/prod"
    completed(read, develop_job, DEVELOP_BUILD, "SUCCESS")
    completed(read, COORDINATOR, COORDINATOR_BUILD, "FAILURE", (develop_job, DEVELOP_BUILD))
    completed(read, prod_job, PROD_BUILD, "SUCCESS", (COORDINATOR, COORDINATOR_BUILD))
    completed(read, OWNER, OWNER_BUILD, "FAILURE", (COORDINATOR, COORDINATOR_BUILD))

    owner = prefix(OWNER, OWNER_BUILD) + "artifact/"
    coordinator = prefix(COORDINATOR, COORDINATOR_BUILD) + "artifact/"
    request_raw = read(owner + "request.json")
    failed_raw = read(owner + "runtime-receipt.json")
    signed_request = envelope(request_raw, REQUEST_SHA256)
    signed_failure = envelope(failed_raw, FAILED_SHA256)
    signed_promotion = envelope(read(coordinator + "promotion.json"), PROMOTION_SHA256)
    signed_finalization = envelope(read(coordinator + "finalization.json"), FINALIZATION_SHA256)
    require(read(coordinator + "deployment-request.json") == request_raw,
            "owner and coordinator request artifacts disagree")
    require(signed_request["payload"]["promotion"] == signed_promotion
            and signed_request["payload"]["finalization"] == signed_finalization,
            "coordinator receipts differ from signed deployment request")

    failure = control.verify(signed_failure, key)
    started = gate.timestamp(failure["started_at"])
    request = deploy.request_identity(signed_request, key, started)
    promotion = control.verify(signed_promotion, key)
    finalization = control.verify(signed_finalization, key)
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    require(started <= gate.timestamp(failure["finished_at"]) <= now,
            "failed deployment chronology changed")
    require(sha(gate.canonical(request)) == REQUEST_PAYLOAD_SHA256
            and request["prod_build"] == PROD_BUILD and request["prod_job"] == prod_job
            and request["library_revision"] == failure["library_revision"] == promotion["library_revision"] == LIBRARY
            and request["deployment_script_revision"] == failure["deployment_script_revision"] == COMMIT
            and "recovery" not in request and "mode" not in request,
            "invalid #115 deployment provenance")
    require(promotion["develop_build"] == DEVELOP_BUILD and promotion["source_commit"] == DEVELOP_COMMIT,
            "incorrect #115 develop source")
    require(all((item["commit"], item["version"], item["digest"], item["product"]) == (COMMIT, VERSION, DIGEST, PRODUCT)
                for item in (request, failure, finalization)),
            "incorrect #115 release identity")
    require(failure["schema_version"] == 1 and failure["kind"] == "runtime-deployment"
            and failure["status"] == "FAILED" and failure["request_sha256"] == REQUEST_SHA256
            and failure["automatic_rollback"] is False and failure["mutation_may_have_started"] is True
            and failure["previous_container_id"] == PREVIOUS_CONTAINER_ID
            and failure["previous_image_id"] == PREVIOUS_IMAGE_ID
            and len(failure["new_backup_and_migration_files"]) == 3
            and not {"recovery", "mode", "app_binding", "observed"}.intersection(failure),
            "incorrect #115 failed deployment phase")

    lineage = {
        "coordinator_build": COORDINATOR_BUILD,
        "owner_build": OWNER_BUILD,
        "develop_build": DEVELOP_BUILD,
        "prod_build": PROD_BUILD,
        "request_sha256": REQUEST_SHA256,
        "request_payload_sha256": REQUEST_PAYLOAD_SHA256,
        "failed_receipt_sha256": FAILED_SHA256,
        "promotion_sha256": PROMOTION_SHA256,
        "finalization_sha256": FINALIZATION_SHA256,
    }
    return request, failure, finalization, lineage


def verify_persistent_failure(state_root, key, failure):
    """Check the original owner receipt while the caller holds the PROD lifecycle lock."""
    require(state_root.is_absolute() and state_root.resolve(strict=True) == state_root,
            "invalid original owner state directory")
    info = state_root.lstat()
    require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid(),
            "unsafe original owner state directory")
    state = control.State(state_root)
    with state.lock(create_missing=False):
        path = state.path(COMMIT)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_uid == os.getuid() and os.path.samestat(info, path.lstat()),
                    "unsafe original owner receipt")
            original = envelope(stream.read(65537), FAILED_SHA256)
        require(control.verify(original, key) == failure, "original owner receipt no longer matches Jenkins")
