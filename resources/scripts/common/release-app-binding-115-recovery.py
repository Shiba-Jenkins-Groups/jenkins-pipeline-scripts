#!/usr/bin/env python3
"""Complete the exact owner #115 transaction from the dedicated Jenkins owner job."""

import argparse
import datetime as dt
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

JOB = "shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-115"
NODE = "shiba-prod-owner-macos"
RUNTIME = Path("/Users/surpend/Developer/01_project/shiba-go-ditch-api-project")
DEPLOYMENT_STATE = Path("/Users/surpend/Developer/jenkins/builds/release-state/deployment")
TRANSACTION = "ad99ceab78822c71b8fe99bebb52dd8a"
COMMIT = "84c2b0a6eae1fb74944af0083859450d321dbcbd"
ENGINE = "9f2f05b6-4637-45c7-ab13-cf5a35e2a539"
CONTAINER = "4aee6e553ba3a886e2772240f23d1c2067b9f51b7571150b82f1dba9967606b7"
CANDIDATE_IMAGE_ID = "sha256:861ae9baf79fa6f8021da642143ed720dc9370dc23239fda99ad6f59395acbaf"
CONTROL_SHA256 = {
    "release-app-binding.py": "2857a7c200e97a82d10cc25509317a7d654eb5abccc19832fdec78aee3d2aefe",
    "release-deploy.py": "7b16f46977c38024af1e3afb344b77441f564771e0bf47a84272b1c31b0a4140",
    "release-evidence.py": "ad3c323b4be971f22cec8b93439810775d796696b61fec3639df5ef75fb728fd",
    "release-gate.py": "4293c7c9ced5417f8b6c55621d408e07ae85a6f7fc05f4d03d2d477558ee9589",
    "release-promotion.py": "26e57f5063780670a42eb95bf09ae1e04c6be887349da8ac7caed803c2308048",
    "release-rebuild.py": "8d78ad6f0e6fde861b24f3d8b134eb5891777d5d50200ab936149cef6e24bc72",
    "release-recovery.py": "19dd4192a8fb6c6e4c62c5c17ad0cfb8cf2e17d2a7ac82e3c3e9afe87d34d126",
}


class Blocked(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise Blocked(code)


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def approved_path(path, workspace, exists=True):
    require(path.is_absolute() and path.resolve(strict=exists) == path and path.is_relative_to(workspace),
            "unexpected_owner_workspace_path")
    require(path != workspace, "unexpected_owner_workspace_root")


def validate_workspace(args):
    require(sys.platform == "darwin" and os.environ.get("NODE_NAME") == NODE
            and os.environ.get("JOB_NAME") == JOB and os.environ.get("BUILD_NUMBER") == "1",
            "unapproved_owner_job")
    require(re.fullmatch(r"[0-9a-f]{40}", os.environ.get("RECOVERY_LIBRARY_REVISION", "")),
            "missing_recovery_library_identity")
    workspace = Path(os.environ["WORKSPACE"]).resolve(strict=True)
    approved_path(args.source, workspace)
    approved_path(args.control, workspace)
    approved_path(args.receipt, workspace, exists=False)
    require(args.receipt.name == ("verify-receipt.json" if args.mode == "verify" else "recovery-receipt.json"),
            "unexpected_recovery_receipt_name")
    require(args.receipt.parent.is_dir() and not os.path.lexists(args.receipt), "recovery_receipt_already_exists")
    for name, expected in CONTROL_SHA256.items():
        file = args.control / name
        require(file.is_file() and not file.is_symlink() and hashlib.sha256(file.read_bytes()).hexdigest() == expected,
                "original_control_file_changed")
    deploy = module("owner115_deploy", args.control / "release-deploy.py")
    require(deploy.control.git(args.source, "remote", "get-url", "origin") == deploy.control.REMOTE
            and deploy.control.git(args.source, "rev-parse", "HEAD") == COMMIT
            and not deploy.control.git(args.source, "status", "--porcelain", "--untracked-files=all")
            and (args.source / "VERSION").read_text(encoding="ascii").strip() == "1.1.39",
            "unexpected_app_source")
    require(deploy.run(["docker", "info", "--format", "{{.ID}}"]) == ENGINE, "unexpected_engine")
    return deploy


def emit(deploy, binding, path, data, key):
    receipt = deploy.control.sign(data, key)
    binding.create(path, deploy.gate.canonical(receipt) + b"\n")


def observe(runtime_module, deploy, request):
    observed = runtime_module.observe_app(deploy, request, RUNTIME)
    require(observed["container_id"] == CONTAINER and observed["image_id"] == CANDIDATE_IMAGE_ID,
            "unexpected_runtime_identity")
    return observed


def verify_closed(binding, transaction, observed):
    config = binding.document(transaction.owner_directory / "owner.json")
    registry = binding.document(transaction.owner_directory / "registry.json")
    for service in (config["services"]["app"], registry["services"]["app"]):
        targets = service["docker"]["targets"]
        require(len(targets) == 1 and targets[0]["id"] == observed["container_id"]
                and targets[0]["image_id"] == observed["image_id"] and service["actions"] == ["status"],
                "unexpected_final_binding")
    require(transaction.io.owner(transaction) is not None, "resident_owner_not_observed")
    require(not os.path.lexists(transaction.pending) and not os.path.lexists(transaction.job_path)
            and not any(os.path.lexists(path) and binding.read(path) == (TRANSACTION + "\n").encode()
                        for path in transaction.owned_pending),
            "original_transaction_not_closed")


def verify_prior_observation(deploy, binding, path, payload, key):
    prior = deploy.control.verify(binding.document(path), key)
    require(prior.get("mode") == "verify" and prior.get("status") == "VERIFIED"
            and prior.get("apply_claimed") is False, "exact_preflight_receipt_required")
    fields = ("job", "build", "recovery_library_revision", "transaction_id", "source_commit", "version", "digest",
              "original_evidence_sha256", "observed", "migrations", "retained_notification")
    require(all(prior.get(name) == payload.get(name) for name in fields), "preflight_evidence_changed")
    age = dt.datetime.now(dt.timezone.utc) - deploy.gate.timestamp(prior["finished_at"])
    require(dt.timedelta(0) <= age < dt.timedelta(minutes=10), "preflight_receipt_stale")


def execute(args):
    deploy = validate_workspace(args)
    binding = module("owner115_binding", args.control / "release-app-binding.py")
    here = Path(__file__).resolve().parent
    evidence = module("owner115_evidence", here / "release-app-binding-115-evidence.py")
    attachment = module("owner115_attachment", here / "release-app-binding-admitted-115.py")
    runtime_module = module("owner115_runtime", here / "release-app-binding-115-runtime.py")
    key = Path(os.environ["RELEASE_SIGNING_KEY"]).read_bytes()
    payload = {
        "schema_version": 1, "kind": "app-binding-incident-115-recovery", "product": deploy.gate.PRODUCT,
        "mode": args.mode, "job": JOB, "build": 1, "coordinator_build": 257, "owner_build": 115,
        "recovery_library_revision": os.environ["RECOVERY_LIBRARY_REVISION"],
        "transaction_id": TRANSACTION, "source_commit": COMMIT, "started_at": now(),
        "status": "FAILED", "apply_claimed": False, "native_binding_finished": False,
    }
    transaction = None
    try:
        request, failure, finalization, hashes = evidence.collect(os.environ["RELEASE_JENKINS_URL"], key)
        require(finalization["commit"] == COMMIT and finalization["digest"] == request["digest"],
                "unexpected_recovery_finalization")
        payload.update(version=request["version"], digest=request["digest"], original_evidence_sha256=hashes)
        transaction = attachment.attach(binding, request, failure, RUNTIME)
        evidence.verify_persistent_failure(DEPLOYMENT_STATE, key, failure)
        claim = transaction.owner_directory / "app-binding-incident-115-recovery.claim.json"
        require(not os.path.lexists(claim), "incident_apply_already_claimed")
        runtime_module.verify_activation_inputs(RUNTIME, request, runtime_module.PREVIOUS_IMAGE)
        migrations = runtime_module.verify_migration_evidence(RUNTIME, failure)
        observed = observe(runtime_module, deploy, request)
        redis = runtime_module.verify_retained_redis(args.source, RUNTIME, request, observed, transaction.old_target)
        payload.update(observed=observed, migrations=migrations, retained_notification=redis)
        if args.mode == "verify":
            payload["status"] = "VERIFIED"
        else:
            verify_prior_observation(deploy, binding, args.receipt.with_name("verify-receipt.json"), payload, key)
            attachment.present(binding, transaction)
            claim_data = {"schema_version": 1, "transaction_id": TRANSACTION, "job": JOB, "build": 1,
                          "container_id": CONTAINER, "observed_at": now(), "original_evidence_sha256": hashes}
            binding.create(claim, deploy.gate.canonical(deploy.control.sign(claim_data, key)) + b"\n")
            payload["apply_claimed"] = True
            payload["activation_persistence"] = runtime_module.persist_completed_activation(
                RUNTIME, request, runtime_module.PREVIOUS_IMAGE, TRANSACTION)
            payload["app_binding"] = transaction.finish(observed)
            payload["native_binding_finished"] = True
            verify_closed(binding, transaction, observed)
            require(observe(runtime_module, deploy, request) == observed, "app_changed_during_binding")
            payload["status"] = "SUCCESS"
        return 0
    except Exception as error:
        public = getattr(error, "receipt", None)
        if isinstance(public, dict):
            payload["binding_failure"] = public
        payload["code"] = (str(error) if isinstance(error, Blocked)
                           else public.get("code", type(error).__name__) if isinstance(public, dict)
                           else type(error).__name__)
        print("Owner #115 recovery stopped at " + payload["code"], file=sys.stderr)
        return 1
    finally:
        if transaction is not None:
            transaction.close()
        payload["finished_at"] = now()
        emit(deploy, binding, args.receipt, payload, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("verify", "apply"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        return execute(args)
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Owner #115 recovery entry blocked: " + (str(error) if isinstance(error, Blocked) else type(error).__name__),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
