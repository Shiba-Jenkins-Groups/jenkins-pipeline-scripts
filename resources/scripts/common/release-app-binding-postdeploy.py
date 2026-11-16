#!/usr/bin/env python3
"""Complete the exact committed App binding interrupted at PROD owner #101."""
import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import time


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = load("release-deploy")
E = load("release-evidence")
R = load("release-binding-repair")
O = load("release-app-binding-postdeploy-omlx")
S = load("release-app-binding-postdeploy-state")
C = load("release-app-binding-postdeploy-close")
B = S.B
require = B.require
OLD_LIBRARY = "d630558e3450557d85a9decaed82e46035630824"
OLD_OWNER_BUILD = 101
COORDINATOR_BUILD = 226
REQUEST_HASH = "1526338006f02c2d33991c8351da26c6408d5bc2f903a37694a93490a3de7539"
FAILED_HASH = "3275f9ea2be7a4863869a136c503c05e0291a5e6c7e6bffd8fe22807bfe31a19"
TX = "f52ecd166595666ecd7a248f100583da"
JOB = "shiba-release-automation/shiba-go-ditch-api-project-prod-binding-recover-101"
CLAIM = "postdeploy-recovery-101"
CONTINUATION = "after-app-management"
PRIOR_APPLY = 2
PRIOR_LIBRARY = "e6c5ff7fb795baea15925f9ccc143714c56383d4"
PRIOR_PLAN_HASH = "6b3bafa68cfe27e2f8eae2c466ef8e23389792982f99cf9381faa2a54549ed37"
PRIOR_APPLY_HASH = "50511fea8a2ac1f5b96a0439f978e20f6614dead3eb12710e8f03397d8e5cbb4"
CONTINUATION_LIBRARY = "ddde8dd5f66d48af2d586be2e9f5d8f972b5076a"
CONTINUATION_PLAN_HASH = "7b761f6e4a654b6cb9fceac62fb6cdd994de984614491ee5c8a5d56497206406"
CONTINUATION_APPLY_HASH = "da0bdfcc9e6584a6466900b67e390ade0fe636d66e4ae1e60571468f1bc6ca44"
CONTINUATION_APPLY = 4


def exact_lineage(url, key, get=E.jenkins_get):
    owner = R.prefix(R.OWNER, OLD_OWNER_BUILD)
    coordinator = R.prefix(R.COORDINATOR, COORDINATOR_BUILD)
    query = "api/json?tree=number,building,result,actions[causes[_class,upstreamBuild,upstreamProject]]"
    owner_build = json.loads(get(url, owner + query))
    coordinator_build = json.loads(get(url, coordinator + query))
    require(all(item.get("number") == number and item.get("building") is False and item.get("result") == "FAILURE"
                for item, number in ((owner_build, OLD_OWNER_BUILD), (coordinator_build, COORDINATOR_BUILD))),
            "archived_incident_build_mismatch")
    R.upstream(owner_build, R.COORDINATOR, COORDINATOR_BUILD)
    request_raw = get(url, owner + "artifact/request.json")
    failed_raw = get(url, owner + "artifact/runtime-receipt.json")
    require(B.sha(request_raw) == REQUEST_HASH and B.sha(failed_raw) == FAILED_HASH
            and get(url, coordinator + "artifact/deployment-request.json") == request_raw,
            "archived_incident_artifact_mismatch")
    signed = json.loads(request_raw, object_pairs_hook=B.pairs)
    failed = R.signed_payload(json.loads(failed_raw, object_pairs_hook=B.pairs), key)
    request = D.request_identity(signed, key, D.gate.timestamp(failed["started_at"]))
    require(request["library_revision"] == failed["library_revision"] == OLD_LIBRARY
            and request["prod_build"] == 224 and "recovery" not in request
            and failed["request_sha256"] == REQUEST_HASH and failed["status"] == "FAILED"
            and failed["kind"] == "runtime-deployment" and failed["product"] == D.gate.PRODUCT
            and all(request[field] == failed[field] for field in ("commit", "version", "digest"))
            and failed["app_binding"]["transaction_id"] == TX
            and failed["app_binding"]["code"] == "owner_resume_unconfirmed"
            and failed["app_binding"]["phase"] == "committed-installation.json",
            "incident_release_or_phase_mismatch")
    public_failure = json.loads(get(url, owner + "artifact/app-binding-failure.json"), object_pairs_hook=B.pairs)
    require(public_failure == failed["app_binding"], "incident_failure_artifacts_disagree")
    return request, failed, failed_raw


def job_path():
    return "/" + "/".join("job/" + item for item in JOB.split("/"))


def earlier_attempt(url, key, get=E.jenkins_get):
    path = job_path()
    build = json.loads(get(url, path + "/2/api/json?tree=number,building,result"))
    require(build.get("number") == PRIOR_APPLY and build.get("building") is False
            and build.get("result") == "FAILURE", "previous_recovery_build_mismatch")
    plan_raw = get(url, path + "/1/artifact/recovery-plan.json")
    failed_raw = get(url, path + "/2/artifact/recovery-receipt.json")
    require(B.sha(plan_raw) == PRIOR_PLAN_HASH and B.sha(failed_raw) == PRIOR_APPLY_HASH,
            "previous_recovery_artifact_mismatch")
    plan = R.signed_payload(json.loads(plan_raw, object_pairs_hook=B.pairs), key)
    failed = R.signed_payload(json.loads(failed_raw, object_pairs_hook=B.pairs), key)
    require(plan["status"] == "QUALIFIED" and plan["transaction_id"] == failed["transaction_id"] == TX
            and plan["library_revision"] == failed["library_revision"] == PRIOR_LIBRARY
            and plan["plan_sha256"] == failed["plan_sha256"] == B.sha(B.encoded(
                {field: value for field, value in plan.items() if field not in {"status", "plan_sha256"}}))
            and failed["status"] == "FAILED" and failed["build_number"] == PRIOR_APPLY
            and failed["jenkins_job"] == JOB and failed["phase"] == "qualify-omlx-and-candidate"
            and failed["failure_code"] == "PermissionError"
            and failed["failed_registration_unloaded"] is True and failed["released_fences"] == []
            and not any(field in failed for field in ("omlx", "owner", "admission_released")),
            "previous_recovery_did_not_stop_before_commit")
    return plan, failed_raw


def qualify_continuation(tx, prior, key, io, before, after, omlx):
    plan, failed_raw = prior
    previous = B.directory(tx.backup / CLAIM)
    info = previous.lstat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
            and {p.name for p in previous.iterdir()} ==
            {"claim.json", "failure.json"} | {"before-" + p.name for p in before},
            "previous_recovery_has_additional_progress")
    claim = R.signed_payload(B.document(previous / "claim.json"), key)
    require(claim["status"] == "CLAIMED" and claim["build_number"] == PRIOR_APPLY
            and claim["transaction_id"] == TX and claim["library_revision"] == PRIOR_LIBRARY
            and claim["plan_sha256"] == plan["plan_sha256"]
            and B.read(previous / "failure.json") == failed_raw
            and all(B.read(previous / ("before-" + path.name)) == raw for path, raw in before.items()),
            "previous_recovery_claim_mismatch")
    expected_omlx = dict(plan["omlx"], program=dict(plan["omlx"]["program"], mode=0o755))
    require(plan["before"] == pins(before) and plan["after"] == pins(after) and omlx == expected_omlx,
            "post_app_management_identity_or_config_changed")
    require(io.owner(tx) is None, "previous_failed_owner_registration_reappeared")
    io.verify_no_listener(tx)
    return previous / CONTINUATION


def check_config(transaction, candidate):
    result = subprocess.run([transaction.config["executable"], "--config", str(candidate), "--check-config"],
                            capture_output=True, timeout=20)
    require(result.returncode == 0 and result.stdout.strip() == b"configuration_valid",
            "reconciled_owner_config_check_failed")


class OwnerIO(B.NativeIO):
    def verify_no_listener(self, tx):
        port = tx.config["listen"].rsplit(":", 1)[1]
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + port, "-sTCP:LISTEN", "-t"],
                                capture_output=True, timeout=5)
        require(result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(),
                "unexpected_service_control_port_listener")

    def unload_failed_registration(self, tx):
        try:
            current = self.owner(tx)
        except B.BindingError as error:
            require(str(error) == "owner_loaded_without_process", "foreign_owner_during_recovery")
        else:
            require(current is None, "unexpected_live_owner_during_recovery")
            return False
        output = self.command(["/bin/launchctl", "print", tx.domain]).decode()
        fields = dict(line.strip().split(" = ", 1) for line in output.splitlines()
                      if line.startswith("\t") and not line.startswith("\t\t") and " = " in line)
        require(fields.get("program") == tx.config["executable"]
                and fields.get("path") in {str(tx.plist), tx.installed["login_agent_path"]}
                and fields.get("state") == "spawn scheduled" and fields.get("last exit code") == "1"
                and fields.get("active count") == "0" and "pid" not in fields,
                "failed_launchd_registration_changed")
        self.command(["/bin/launchctl", "bootout", tx.domain])
        require(self.owner(tx) is None, "failed_launchd_unload_unconfirmed")
        return True

    def prove_running(self, tx, expected):
        deadline = time.monotonic() + 15
        require(self.owner(tx, deadline=deadline) == expected
                and self._resume_ready(tx, expected, deadline), "recovered_owner_identity_changed")
        result = self.command(["/usr/sbin/lsof", "-a", "-p", str(expected["pid"]), "-F", "pftnDi", str(tx.journal)])
        info = tx.journal.stat()
        # lsof fields are split into per-descriptor records. Confirm the same PID
        # holds an FD for the exact journal inode, in addition to the busy lock.
        rows = result.decode().splitlines()
        require(bool(rows) and rows[0] == "p" + str(expected["pid"])
                and not any(row.startswith("p") for row in rows[1:]), "owner_journal_pid_mismatch")
        descriptors = result.decode().split("\nf")[1:]
        require(any({"tREG", "n" + str(tx.journal), "i" + str(info.st_ino)} <= set(row.splitlines())
                    and any(field.startswith("D") and int(field[1:], 16) == info.st_dev for field in row.splitlines())
                    for row in descriptors), "owner_same_pid_journal_handle_required")


def readiness(io, tx, expected):
    # Nine transaction fences must be gone for normal owner recovery to start
    # OCR and Ollama. Read-only polling does not submit any physical operation.
    deadline = time.monotonic() + 720
    last = {}
    while time.monotonic() < deadline:
        io.prove_running(tx, expected)
        for name in ("ocrsvc", "recognition"):
            try:
                status = io.dependency_status(tx, name)
            except (B.BindingError, subprocess.TimeoutExpired):
                continue
            last[name] = {field: status.get(field) for field in
                          ("id", "environment", "desired_state", "process_state", "contract_health", "registry_revision")}
        if all(last.get(name, {}).get("id") == name and last[name].get("environment") == "prod"
               and last[name].get("desired_state") == last[name].get("process_state") == "running"
               and last[name].get("contract_health") == "ready"
               and last[name].get("registry_revision") == tx.config["services"][name]["revision"]
               for name in ("ocrsvc", "recognition")):
            try:
                health = subprocess.run(["/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "4",
                                         "--noproxy", "*", "http://127.0.0.1:8096/readyz"], capture_output=True, timeout=5)
                payload = json.loads(health.stdout) if health.returncode == 0 else None
            except (ValueError, subprocess.TimeoutExpired):
                payload = None
            if (isinstance(payload, dict) and payload.get("ready") is True
                    and payload.get("service") == "receipt-recognition" and payload.get("contract_version") == 1):
                return last
        # A later failure discards former successes, so both statuses are fresh.
        last.clear()
        time.sleep(5)
    raise B.BindingError("recognition_readiness_deadline_exceeded")


def pins(configs):
    return {str(path): B.sha(raw) for path, raw in configs.items()}


def applied_continuation(url, key, get=E.jenkins_get):
    path = job_path()
    build = json.loads(get(url, path + "/4/api/json?tree=number,building,result"))
    require(build.get("number") == CONTINUATION_APPLY and build.get("building") is False
            and build.get("result") == "FAILURE", "continuation_apply_build_mismatch")
    plan_raw = get(url, path + "/3/artifact/recovery-plan.json")
    applied_raw = get(url, path + "/4/artifact/recovery-receipt.json")
    require(B.sha(plan_raw) == CONTINUATION_PLAN_HASH and B.sha(applied_raw) == CONTINUATION_APPLY_HASH,
            "continuation_apply_artifact_mismatch")
    plan = R.signed_payload(json.loads(plan_raw, object_pairs_hook=B.pairs), key)
    applied = R.signed_payload(json.loads(applied_raw, object_pairs_hook=B.pairs), key)
    require(plan["status"] == "QUALIFIED" and applied["status"] == "FAILED"
            and plan["transaction_id"] == applied["transaction_id"] == TX
            and plan["library_revision"] == applied["library_revision"] == CONTINUATION_LIBRARY
            and applied["jenkins_job"] == JOB and applied["build_number"] == CONTINUATION_APPLY
            and plan["plan_sha256"] == applied["plan_sha256"] == B.sha(B.encoded(
                {field: value for field, value in plan.items() if field not in {"status", "plan_sha256"}}))
            and plan["continued_from_build"] == applied["continued_from_build"] == PRIOR_APPLY
            and plan["previous_recovery_sha256"] == applied["previous_recovery_sha256"] == PRIOR_APPLY_HASH
            and applied["original_failed_receipt_sha256"] == FAILED_HASH
            and applied["phase"] == "release-owned-admission"
            and applied["failure_code"] == "postdeploy_foreign_resource_fence"
            and isinstance(applied["owner"], dict)
            and len(set(applied["released_fences"])) == len(applied["released_fences"]) == 9,
            "continuation_apply_requires_exact_released_transaction")
    return plan, applied, applied_raw


def verify_by_readiness(args, request, failed, original, key, evidence, io=None):
    """This lane attests a completion after #4; it issues no lifecycle action."""
    io = io or OwnerIO()
    plan, applied, applied_raw = evidence
    backup = Path(failed["app_binding"]["backup"])
    claim = backup / CLAIM / CONTINUATION
    state_path = args.state_directory / (request["commit"] + ".json")
    verify_deployment_state(state_path, original, key)
    require(B.read(claim / "failure.json") == applied_raw, "on_disk_continuation_failure_changed")
    intent = R.signed_payload(B.document(claim / "release-intent.json"), key)
    require(intent["status"] == "INTENT" and intent["plan_sha256"] == plan["plan_sha256"]
            and intent["owner"] == applied["owner"] and intent["transaction_id"] == TX
            and intent["phase"] == applied["phase"] and intent["build_number"] == CONTINUATION_APPLY,
            "on_disk_release_intent_changed")
    tx = C.verify(args.runtime_root, request, failed, plan, applied, io)
    require(io.engine(tx) == args.docker_engine_id, "configured_engine_mismatch")
    before = {Path(path): B.read(claim / ("before-" + Path(path).name)) for path in plan["before"]}
    rebinding, omlx = O.plan(before, program_mode=0o755)
    require(pins(rebinding) == plan["after"] and omlx == plan["omlx"] == applied["omlx"],
            "completed_omlx_binding_identity_changed")
    services = readiness(io, tx, applied["owner"])
    C.verify(args.runtime_root, request, failed, plan, applied, io)
    verify_deployment_state(state_path, original, key)
    result = {"schema_version": 1, "kind": "app-binding-postdeploy-recovery", "status": "SUCCESS",
              "mode": "verification-only", "phase": "completed", "jenkins_job": JOB,
              "build_number": args.build_number, "library_revision": args.library_revision,
              "original_owner_build": OLD_OWNER_BUILD, "original_coordinator_build": COORDINATOR_BUILD,
              "original_failed_receipt_sha256": FAILED_HASH, "request_sha256": REQUEST_HASH,
              "continuation_build": CONTINUATION_APPLY, "continuation_failed_receipt_sha256": CONTINUATION_APPLY_HASH,
              "plan_sha256": plan["plan_sha256"], "transaction_id": TX, "app": plan["app"],
              "commit": request["commit"], "version": request["version"], "digest": request["digest"],
              "owner": applied["owner"], "omlx": omlx, "services": services,
              "released_fences": applied["released_fences"], "admission_released": True,
              "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    B.create(args.output, B.encoded(D.control.sign(result, key)))


def verify_deployment_state(path, original, key):
    data = B.read(path)
    D.control.verify(json.loads(data, object_pairs_hook=B.pairs), key)
    require(data == original, "original_signed_owner_state_changed")


def execute(args, request, failed, failed_raw, key, io=None, prior=None):
    io = io or OwnerIO()
    checkpoint = S.Checkpoint(args.runtime_root, args.state_directory, request, failed, io)
    state_path = args.state_directory / (request["commit"] + ".json")
    receipt = {"schema_version": 1, "kind": "app-binding-postdeploy-recovery", "status": "FAILED",
               "jenkins_job": JOB, "build_number": args.build_number, "library_revision": args.library_revision,
               "transaction_id": TX, "original_owner_build": OLD_OWNER_BUILD, "original_coordinator_build": COORDINATOR_BUILD,
               "request_sha256": REQUEST_HASH, "original_failed_receipt_sha256": FAILED_HASH,
               "commit": request["commit"], "version": request["version"], "digest": request["digest"],
               "automatic_retry": False, "automatic_rollback": False, "phase": "attest-checkpoint"}
    claimed = False
    claim = None
    try:
        with checkpoint.locked():
            tx = checkpoint.transaction
            require(io.engine(tx) == args.docker_engine_id, "configured_engine_mismatch")
            verify_deployment_state(state_path, failed_raw, key)
            before = checkpoint.configs
            if prior is None:
                claim = tx.backup / CLAIM
                after, omlx = O.plan(before)
            else:
                after, omlx = O.plan(before, program_mode=0o755)
                claim = qualify_continuation(tx, prior, key, io, before, after, omlx)
                receipt.update(continued_from_build=PRIOR_APPLY, previous_recovery_sha256=PRIOR_APPLY_HASH)
            plan = {"transaction_id": TX, "original_owner_build": OLD_OWNER_BUILD, "original_coordinator_build": COORDINATOR_BUILD,
                    "library_revision": args.library_revision, "request_sha256": REQUEST_HASH,
                    "original_failed_receipt_sha256": FAILED_HASH, "before": pins(before), "after": pins(after),
                    "omlx": omlx, "app": checkpoint.observed}
            if prior is not None:
                plan.update(continued_from_build=PRIOR_APPLY, previous_recovery_sha256=PRIOR_APPLY_HASH)
            plan_sha = B.sha(B.encoded(plan))
            if args.action == "plan":
                plan.update(status="QUALIFIED", plan_sha256=plan_sha)
                B.create(args.output, B.encoded(D.control.sign(plan, key)))
                return
            require(args.plan_sha256 == plan_sha, "reviewed_recovery_plan_changed")
            claim.mkdir(mode=0o700)
            B.sync(claim.parent)
            claimed = True
            receipt.update(plan_sha256=plan_sha, phase="claimed", app=checkpoint.observed)
            B.create(claim / "claim.json", B.encoded(D.control.sign(dict(receipt, status="CLAIMED"), key)))
            for path, data in before.items():
                B.create(claim / ("before-" + path.name), data)
            receipt["phase"] = "unload-failed-registration"
            if prior is None:
                receipt["failed_registration_unloaded"] = io.unload_failed_registration(tx)
            else:
                require(io.owner(tx) is None, "previous_failed_owner_registration_reappeared")
            receipt["phase"] = "qualify-omlx-and-candidate"
            if prior is None:
                receipt["omlx"] = O.apply_mode(omlx)
            else:
                confirmed_after, confirmed_identity = O.plan(before, program_mode=0o755)
                require(confirmed_after == after and confirmed_identity == omlx,
                        "post_app_management_identity_changed_before_commit")
                receipt["omlx"] = omlx
            candidate = claim / "candidate-owner.json"
            B.create(candidate, after[tx.owner_directory / "owner.json"])
            check_config(tx, candidate)
            checkpoint.check_after(before)
            receipt["phase"] = "reconcile-exact-configs"
            B.create(claim / "commit-intent.json", B.encoded(D.control.sign(
                {"transaction_id": TX, "plan_sha256": plan_sha, "before": pins(before), "after": pins(after)}, key)))
            for path, data in after.items():
                if data != before[path]:
                    require(B.read(path) == before[path], "config_changed_before_reconciliation")
                    B.replace(path, data)
            checkpoint.check_after(after)
            verify_deployment_state(state_path, failed_raw, key)
            tx.config = json.loads(after[tx.owner_directory / "owner.json"], object_pairs_hook=B.pairs)
            check_config(tx, tx.owner_directory / "owner.json")
            receipt["phase"] = "managed-owner-resume"
            archived = claim / "startup-pending.archived.json"
            require(not archived.exists() and B.read(tx.pending) == tx.pending_raw, "startup_marker_changed")
            os.rename(tx.pending, archived)
            B.sync(tx.owner_directory)
            B.sync(claim)
            original_backup, tx.backup = tx.backup, claim
            try:
                checkpoint.release_journal()
                owner = io.resume(tx)
                io.prove_running(tx, owner)
            except BaseException:
                if not os.path.lexists(tx.pending):
                    B.create(tx.pending, tx.pending_raw)
                raise
            finally:
                tx.backup = original_backup
            receipt.update(owner=owner, phase="release-owned-admission")
            # This signed intent precedes the first release, including on interruption.
            B.create(claim / "release-intent.json", B.encoded(D.control.sign(dict(receipt, status="INTENT"), key)))
            checkpoint.check_after(after)
            receipt["released_fences"] = checkpoint.release_fences(owner)
            receipt.update(admission_released=True, phase="recognition-readiness")
        receipt["services"] = readiness(io, tx, owner)
        io.prove_running(tx, owner)
        require(all(B.read(path) == raw for path, raw in after.items()), "config_changed_after_owner_recovery")
        verify_deployment_state(state_path, failed_raw, key)
        receipt.update(status="SUCCESS", phase="completed")
    except BaseException as error:
        receipt["failure_code"] = str(error) if isinstance(error, (B.BindingError, O.B.BindingError)) else type(error).__name__
        if claimed:
            receipt["released_fences"] = [str(path) for path in checkpoint.released_fences]
        raise
    finally:
        if args.action == "apply":
            receipt["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            signed = B.encoded(D.control.sign(receipt, key))
            if claimed:
                B.create(claim / ("completed.json" if receipt["status"] == "SUCCESS" else "failure.json"), signed)
            B.create(args.output, signed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "close"))
    parser.add_argument("--jenkins-url", required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--docker-engine-id", required=True)
    parser.add_argument("--library-revision", required=True)
    parser.add_argument("--build-number", required=True, type=int)
    parser.add_argument("--plan-sha256", default="")
    parser.add_argument("--earlier-attempt", default=0, type=int, choices=(0, PRIOR_APPLY))
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        require(os.environ.get("JOB_NAME") == JOB and args.build_number > 0
                and B.COMMIT.fullmatch(args.library_revision), "incident_owner_job_identity_required")
        require((args.action == "apply") == bool(D.SHA256.fullmatch(args.plan_sha256)),
                "exact_recovery_plan_hash_required")
        key = args.key_file.read_bytes()
        args.output = args.output.absolute()
        request, failed, failed_raw = exact_lineage(args.jenkins_url, key)
        require(args.action != "close" or args.earlier_attempt == PRIOR_APPLY, "wrong_closure_route")
        evidence = applied_continuation(args.jenkins_url, key) if args.action == "close" else None
        prior = earlier_attempt(args.jenkins_url, key) if args.earlier_attempt and evidence is None else None
        os.environ.pop("JENKINS_API_TOKEN", None)
        os.environ.pop("JENKINS_API_USER", None)
        if evidence is None:
            execute(args, request, failed, failed_raw, key, prior=prior)
        else:
            verify_by_readiness(args, request, failed, failed_raw, key, evidence)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        expected_errors = (B.BindingError, O.B.BindingError, C.B.BindingError, D.gate.InvalidEvidence, R.gate.InvalidEvidence)
        parser.exit(1, f"{str(error) if isinstance(error, expected_errors) else type(error).__name__}\n")


if __name__ == "__main__":
    main()
