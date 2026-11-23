#!/usr/bin/env python3
"""Restricted Jenkins dispatcher for read-only observation and committed owner #105."""
import argparse
import base64
from contextlib import contextmanager
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


entry = load("committed_job_entry", "release-app-binding-committed.py")
repair = load("committed_job_existing_repair", "release-binding-repair.py")
require, gate = entry.require, entry.gate
DRIVER = "release-app-binding-committed-native.py"
RUNTIME = Path("/Users/surpend/Developer/01_project/shiba-go-ditch-api-project")


def clock():
    return dt.datetime.now(dt.timezone.utc)


def configured_admission(revision, driver_sha):
    admission = entry.Admission(revision, driver_sha)
    entry.check_admission(admission)
    path = Path(__file__).with_name(DRIVER)
    require(not path.is_symlink() and path.is_file() and entry.digest(path.read_bytes()) == driver_sha,
            "reviewed native adapter SHA does not match the pinned Library resource")
    return admission


def principal_list(value):
    require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], str)
            and bool(value[0].strip()) and value[0] == value[0].strip(),
            "exact approved incident operator required")
    return value


def coordinator_policy(path, admission):
    policy = repair.read(path)
    require(policy.get("library_revision") == admission.library_revision and policy.get("product") == gate.PRODUCT,
            "committed incident policy is for a different Library or product")
    return principal_list(policy.get("approvers"))


def completed(base, job, number, result):
    prefix = repair.prefix(job, number)
    build = json.loads(repair.adapter.jenkins_get(base, prefix + "api/json"), object_pairs_hook=repair.pairs)
    require(build.get("number") == number and build.get("building") is False and build.get("result") == result,
            "exact completed Jenkins build or result differs")
    return prefix, build


def fetch_originals(base, key):
    receipts = []
    for coordinator, owner, result in ((238, 105, "FAILURE"), (231, 103, "SUCCESS")):
        completed(base, entry.COORDINATOR, coordinator, result)
        prefix, build = completed(base, entry.OWNER, owner, result)
        repair.upstream(build, entry.COORDINATOR, coordinator)
        if owner == 105:
            require(build.get("queueId") == 15298, "owner #105 queue identity differs")
        receipts.append(repair.adapter.jenkins_get(base, prefix + "artifact/runtime-receipt.json"))
    entry.historical(*receipts, key)
    return receipts


def history_record(receipts):
    return {name: base64.b64encode(raw).decode("ascii")
            for name, raw in zip(("failed", "original"), receipts)}


def decode_history(value, key):
    require(isinstance(value, dict) and set(value) == {"failed", "original"}
            and all(isinstance(raw, str) and 0 < len(raw) < 65536 for raw in value.values()),
            "exact bounded committed incident archives required")
    receipts = [base64.b64decode(value[name], validate=True) for name in ("failed", "original")]
    entry.historical(*receipts, key)
    return receipts


def verify_result(request, envelope, *, admission, api_approvers, upstream_build, owner_build, key):
    issued = gate.timestamp(entry.signed_payload(request, key)["created_at"])
    grant = entry.request_identity(request, admitted=admission, approvers=api_approvers,
                                   upstream_build=upstream_build, key=key, now=issued)
    result = entry.signed_payload(envelope, key)
    require(type(owner_build) is int and owner_build > 105
            and result.get("coordinator_build") == upstream_build and result.get("owner_build") == owner_build
            and result.get("request_sha256") == entry.digest_json(request)
            and result.get("transaction") == entry.TRANSACTION and result.get("failed_owner_build") == 105,
            "committed binding receipt provenance differs")
    finished = gate.timestamp(result["finished_at"])
    require(issued <= finished < gate.timestamp(grant["expires_at"]), "owner receipt outside signed window")
    if grant["kind"] == entry.OBSERVE:
        observation_scope(result, admission, grant["author"])
    else:
        require(result.get("kind") == entry.KIND + "/receipt" and result.get("status") == "SUCCESS"
                and result.get("scope_sha256") == grant["scope_sha256"] and result.get("app_container") == entry.APP_ID
                and result.get("app_version") == "1.1.27" and result.get("app_digest") == entry.APP_DIGEST
                and type(result.get("owner_pid")) is int and result["owner_pid"] > 0 and result["owner_pid"] != 864,
                "exact committed binding success receipt required")
    return result


def observation_scope(result, admission, author):
    require(result.get("kind") == entry.OBSERVE + "/receipt" and result.get("status") == "OBSERVED"
            and result.get("transaction") == entry.TRANSACTION and result.get("failed_owner_build") == 105
            and result.get("library_revision") == admission.library_revision and result.get("driver_sha256") == admission.driver_sha256
            and result.get("author") == author and entry.hex_digest(result.get("request_sha256")),
            "independently signed #105 observation receipt required")
    scope = result.get("scope")
    entry.check_scope(scope)
    require(entry.digest_json(scope) == result.get("scope_sha256"), "observation scope hash differs")
    return scope


def reviewed_scope(base, review, admission, author, key, now):
    if review == {"intent": "observe"}:
        return None
    result = entry.signed_payload(review, key)
    scope = observation_scope(result, admission, author)
    finished = gate.timestamp(result["finished_at"])
    require(finished <= now < finished + dt.timedelta(minutes=15), "reviewed observation expired")
    coordinator, owner = result.get("coordinator_build"), result.get("owner_build")
    require(type(coordinator) is int and coordinator > 238 and type(owner) is int and owner > 105,
            "invalid managed observation build coordinates")
    cp, _ = completed(base, entry.COORDINATOR, coordinator, "SUCCESS")
    op, owner_metadata = completed(base, entry.OWNER, owner, "SUCCESS")
    repair.upstream(owner_metadata, entry.COORDINATOR, coordinator)
    command = json.loads(repair.adapter.jenkins_get(base, cp + "artifact/deployment-request.json"),
                         object_pairs_hook=repair.pairs)
    require(entry.signed_payload(command, key).get("kind") == entry.OBSERVE, "approval needs original observation request")
    verify_result(command, review, admission=admission, api_approvers=[author], upstream_build=coordinator,
                  owner_build=owner, key=key)
    archive = repair.adapter.jenkins_get(base, op + "artifact/runtime-receipt.json")
    require(gate.canonical(json.loads(archive, object_pairs_hook=repair.pairs)) == gate.canonical(review),
            "review differs from the exact archived owner observation")
    return scope


def coordinator_collect(args, admission, key):
    require(args.job == entry.COORDINATOR and os.environ.get("JOB_NAME") == entry.COORDINATOR
            and os.environ.get("BUILD_NUMBER") == str(args.current_build),
            "committed continuation requires the fixed coordinator")
    approvers = coordinator_policy(args.policy, admission)
    require(args.author in approvers, "author is not the trusted #105 approver")
    history = fetch_originals(args.jenkins_url, key)
    scope = reviewed_scope(args.jenkins_url, repair.read(args.review), admission, args.author, key, clock())
    signed = entry.coordinator_request(job=args.job, build=args.current_build, author=args.author,
                                       approvers=approvers, admitted=admission, scope=scope,
                                       failed_raw=history[0], original_raw=history[1], key=key, now=clock())
    repair.write(args.history_output, history_record(history))
    repair.write(args.output, signed)


@contextmanager
def retained_lifecycle(runtime, state):
    require(sys.platform == "darwin" and os.getuid() == 501 and runtime == RUNTIME and runtime.resolve() == runtime,
            "committed #105 must use the designated macOS owner and runtime")
    location = runtime / "data/locks/prod.lock"
    private = state.root.lstat()
    require(state.root.resolve() == state.root and stat.S_ISDIR(private.st_mode) and private.st_uid == os.getuid()
            and stat.S_IMODE(private.st_mode) == 0o700 and location.parent.resolve() == location.parent,
            "unsafe fixed owner directories")
    fd = os.open(location, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened, current = os.fstat(fd), location.lstat()
        require(stat.S_ISREG(opened.st_mode) and opened.st_uid == os.getuid()
                and stat.S_IMODE(opened.st_mode) == 0o600
                and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino),
                "unsafe fixed lifecycle descriptor")
        yield fd
    finally:
        os.close(fd)


def owner_dispatch(args, request, grant, approvers, history, admission, key):
    run = entry.OwnerRun(args.job, args.owner_build, args.upstream_job, args.upstream_build)
    entry.owner_run(run)
    require(os.environ.get("BUILD_NUMBER") == str(run.build)
            and isinstance(args.docker_engine_id, str) and bool(args.docker_engine_id),
            "fixed native owner engine and build required")
    driver = load("committed_job_native", DRIVER)
    state = entry.control.State(args.state_directory)
    with retained_lifecycle(args.runtime_root, state) as lifecycle:
        native = driver.NativeContinuation(entry, args.runtime_root, lifecycle, admission, engine_id=args.docker_engine_id)
        action = entry.owner_observe if grant["kind"] == entry.OBSERVE else entry.owner_finish
        try:
            result = action(request, run=run, admitted=admission, approvers=approvers, native=native, state=state,
                            failed_raw=history[0], original_raw=history[1], key=key, clock=clock)
        except Exception:
            if grant["kind"] == entry.KIND:
                archive_failure(state, run, request, key, args.output)
            raise
    repair.write(args.output, result)


def archive_failure(state, run, request, key, output):
    """Archive only this build's own durable failure, without changing it."""
    try:
        with state.lock(create_missing=False):
            claim = entry.claim_key()
            if not state.exists(claim):
                return
            signed = state.read(claim)
        record = entry.signed_payload(signed, key)
        if (record.get("status") == "FAILED" and record.get("owner_build") == run.build
                and record.get("request_sha256") == entry.digest_json(request)):
            repair.write(output, signed)
    except (OSError, ValueError, KeyError, TypeError):
        # The original exception remains the build outcome. Durable evidence
        # stays in owner state if this optional exact archive is unavailable.
        pass


def owner_command(args, admission, key):
    require(os.environ.get("JOB_NAME") == entry.OWNER, "committed continuation requires the fixed owner job")
    approvers = principal_list(repair.read(args.approvers))
    request = repair.read(args.request)
    if args.command == "verify-result":
        require(os.environ.get("BUILD_NUMBER") == str(args.owner_build), "owner receipt build differs")
        verify_result(request, repair.read(args.receipt), admission=admission, api_approvers=approvers,
                      upstream_build=args.upstream_build, owner_build=args.owner_build, key=key)
        return
    grant = entry.request_identity(request, admitted=admission, approvers=approvers,
                                   upstream_build=args.upstream_build, key=key, now=clock())
    history = decode_history(repair.read(args.history), key)
    if args.command == "inspect":
        repair.write(args.output, {"kind": grant["kind"], "coordinator_build": grant["coordinator_build"],
                                   "library_revision": grant["library_revision"], "driver_sha256": grant["driver_sha256"],
                                   "transaction": entry.TRANSACTION, "app_container": entry.APP_ID})
        return
    owner_dispatch(args, request, grant, approvers, history, admission, key)


def coordinator_verify(args, admission, key):
    require(os.environ.get("JOB_NAME") == entry.COORDINATOR
            and os.environ.get("BUILD_NUMBER") == str(args.coordinator_build), "verify exact coordinator build")
    approvers = coordinator_policy(args.policy, admission)
    prefix, metadata = completed(args.jenkins_url, entry.OWNER, args.owner_build, "SUCCESS")
    repair.upstream(metadata, entry.COORDINATOR, args.coordinator_build)
    signed = json.loads(repair.adapter.jenkins_get(args.jenkins_url, prefix + "artifact/runtime-receipt.json"),
                        object_pairs_hook=repair.pairs)
    verify_result(repair.read(args.request), signed, admission=admission, api_approvers=approvers,
                  upstream_build=args.coordinator_build, owner_build=args.owner_build, key=key)
    repair.write(args.output, signed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "inspect", "finish", "verify-result", "verify-owner"))
    for name in ("key-file", "request", "history", "history-output", "policy", "approvers", "review", "output", "receipt",
                 "runtime-root", "state-directory"):
        parser.add_argument("--" + name, type=Path, required=name == "key-file")
    for name in ("job", "upstream-job", "author", "library-revision", "driver-sha256", "jenkins-url", "docker-engine-id"):
        parser.add_argument("--" + name, required=name in {"library-revision", "driver-sha256"})
    for name in ("upstream-build", "owner-build", "current-build", "coordinator-build"):
        parser.add_argument("--" + name, type=int)
    args = parser.parse_args(argv)
    try:
        admission = configured_admission(args.library_revision, args.driver_sha256)
        key = args.key_file.read_bytes()
        if args.command == "collect":
            coordinator_collect(args, admission, key)
        elif args.command == "verify-owner":
            coordinator_verify(args, admission, key)
        else:
            owner_command(args, admission, key)
    except Exception as error:
        message = str(error) if isinstance(error, gate.InvalidEvidence) else "committed binding action blocked; preserve owner state"
        print("BLOCKED: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
