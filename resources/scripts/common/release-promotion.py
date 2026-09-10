#!/usr/bin/env python3
"""Promotion and signed handoff primitives, executed only by the trusted job.

No retry of merge/push is implicit. Persistent state must be on one reliable
locking domain and survive dynamic agents; ambiguous outcomes require review.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

spec = importlib.util.spec_from_file_location("release_gate", Path(__file__).with_name("release-gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
require = gate.require
REMOTE = "https://github.com/ShibaDev2026/shiba-go-ditch-api-project.git"


def sign(payload, key):
    require(isinstance(key, bytes) and len(key) >= 32, "missing signing key")
    return {"payload": payload, "signature": hmac.new(key, gate.canonical(payload), hashlib.sha256).hexdigest()}


def verify(signed, key):
    expected = sign(signed["payload"], key)["signature"]
    require(hmac.compare_digest(expected, signed["signature"]), "invalid receipt signature")
    return signed["payload"]


def findings(evidence, root, policy=None):
    values = {gate.finding_key(item): item for item in gate.stage_findings(evidence, policy or {}, root)}
    for record in evidence["reports"]:
        envelope = gate.verified_report(root, record)
        native = gate.verified_report(root, envelope["native_report"])
        for finding in gate.native_findings(record["scanner"], native, evidence["image_digest"]):
            values[gate.finding_key(finding)] = finding
    return values


def review(evidence, policy, root, now):
    try:
        return gate.evaluate(evidence, policy, root, now)
    except gate.InvalidEvidence as exc:
        if str(exc) != "unapproved vulnerabilities":
            raise
    return {"decision": "NEEDS_APPROVAL", "evidence_sha256": hashlib.sha256(gate.canonical(evidence)).hexdigest(),
            "findings": findings(evidence, root, policy)}


def approve(evidence, policy, root, identity, key, now):
    require(review(evidence, policy, root, now)["decision"] == "NEEDS_APPROVAL", "nothing eligible for exception")
    payload = {"id": identity["id"], "approver": identity["approver"], "reason": identity["reason"],
               "issued_at": now.isoformat(),
               "expires_at": (now + dt.timedelta(seconds=policy["max_exception_seconds"])).isoformat(),
               "evidence_sha256": hashlib.sha256(gate.canonical(evidence)).hexdigest(),
               "policy_sha256": hashlib.sha256(gate.canonical(policy)).hexdigest(),
               "finding_keys": sorted(findings(evidence, root, policy))}
    signed = sign(payload, key)
    gate.evaluate(evidence, policy, root, now, signed, key)
    return signed


class State:
    def __init__(self, root):
        self.root = Path(root)
        require(self.root.is_absolute() and self.root.is_dir(), "persistent state directory not provisioned")

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / "promotion.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "another release owns promotion lock")
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def path(self, source):
        require(gate.SHA.fullmatch(source), "invalid release state key")
        return self.root / (source + ".json")

    def exists(self, source):
        return self.path(source).exists()

    def read(self, source):
        path = self.path(source)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            return json.load(stream)

    def write(self, source, value):
        fd, temp = tempfile.mkstemp(prefix="release-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(gate.canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path(source))
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def git(source, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                             "-c", "user.name=Jenkins Promotion", "-c", "user.email=jenkins-promotion@localhost",
                             *args], cwd=source, capture_output=True, text=True, timeout=120)
    require(result.returncode == 0, "git operation failed: " + args[0])
    return result.stdout.strip()


def heads(source):
    values = dict(line.split()[::-1] for line in git(source, "ls-remote", "--heads", "origin",
                                                   "refs/heads/develop", "refs/heads/prod").splitlines())
    require(set(values) == {"refs/heads/develop", "refs/heads/prod"}, "release branches missing")
    return values["refs/heads/develop"], values["refs/heads/prod"]


def existing_promotion(source, state, evidence, policy, root, receipt_key, now, approval=None, approval_key=None,
                       expected_remote=REMOTE):
    """Reuse only a completed, signed promotion after revalidating all live identities."""
    gate.evaluate(evidence, policy, root, now, approval, approval_key)
    require(evidence["gate"] == "promotion", "not promotion evidence")
    require(git(source, "remote", "get-url", "origin") == expected_remote, "wrong Git remote")
    require(not git(source, "status", "--porcelain"), "promotion workspace is dirty")
    commit = evidence["commit"]
    require(git(source, "rev-parse", "HEAD") == commit, "promotion checkout mismatch")
    with state.lock():
        require(state.exists(commit), "no existing release claim to resume")
        signed = state.read(commit)
        receipt = verify(signed, receipt_key)
        require(receipt.get("schema_version") == 1 and receipt.get("kind") == "promotion"
                and receipt.get("product") == gate.PRODUCT and receipt.get("status") == "MERGED",
                "existing promotion is not safely resumable")
        require(receipt.get("source_commit") == commit
                and receipt.get("develop_job") == evidence["job"]
                and receipt.get("develop_build") == evidence["build"]
                and receipt.get("develop_image_digest") == evidence["image_digest"]
                and receipt.get("version") == evidence["version"],
                "existing promotion does not match recovered candidate")
        merge = receipt.get("merge_commit", "")
        previous = receipt.get("previous_prod_commit", "")
        require(gate.SHA.fullmatch(merge) and gate.SHA.fullmatch(previous), "invalid existing promotion identity")
        develop, prod = heads(source)
        require(develop == commit, "develop candidate superseded")
        require(prod == merge, "PROD moved after existing promotion")
        git(source, "fetch", "--no-tags", "origin", "refs/heads/prod")
        require(git(source, "rev-parse", "FETCH_HEAD") == merge, "PROD changed during recovery")
        require(git(source, "rev-list", "--parents", "-n", "1", merge).split() == [merge, previous, commit],
                "existing promotion merge parents do not match receipt")
        require(git(source, "show", merge + ":VERSION") == evidence["version"],
                "existing promotion version mismatch")
        require(not git(source, "ls-remote", "--tags", "origin", "refs/tags/v" + evidence["version"],
                        "refs/tags/v" + evidence["version"] + "^{}"),
                "release already finalized; deployment recovery requires receipt review")
        return signed


def promote(source, state, evidence, policy, root, receipt_key, now, approval=None, approval_key=None,
            expected_remote=REMOTE, recover=False):
    # expected_remote is injectable only for offline tests; CLI always fixes it.
    decision = gate.evaluate(evidence, policy, root, now, approval, approval_key)
    require(evidence["gate"] == "promotion", "not promotion evidence")
    require(git(source, "remote", "get-url", "origin") == expected_remote, "wrong Git remote")
    require(not git(source, "status", "--porcelain"), "promotion workspace is dirty")
    commit = evidence["commit"]
    require(git(source, "rev-parse", "HEAD") == commit, "promotion checkout mismatch")
    if recover and state.exists(commit):
        return existing_promotion(source, state, evidence, policy, root, receipt_key, now,
                                  approval, approval_key, expected_remote)
    with state.lock():
        require(not state.exists(commit), "release already claimed; reconcile existing receipt")
        develop, prod = heads(source)
        require(develop == commit, "develop candidate superseded")
        git(source, "fetch", "--no-tags", "origin", "refs/heads/prod")
        require(git(source, "rev-parse", "FETCH_HEAD") == prod, "prod moved during fetch")
        ancestor = git(source, "merge-base", prod, commit)
        require(ancestor != commit, "prod already contains candidate; no new promotion")
        record = {"schema_version": 1, "kind": "promotion", "product": gate.PRODUCT,
                  "source_commit": commit, "previous_prod_commit": prod,
                  "develop_job": evidence["job"], "develop_build": evidence["build"],
                  "develop_image_digest": evidence["image_digest"], "decision": decision,
                  "library_revision": policy.get("library_revision"),
                  "policy_sha256": hashlib.sha256(gate.canonical(policy)).hexdigest(),
                  "evidence_sha256": hashlib.sha256(gate.canonical(evidence)).hexdigest(),
                  "created_at": now.isoformat(), "status": "PREPARING"}
        state.write(commit, sign(record, receipt_key))
        git(source, "checkout", "--detach", prod)
        git(source, "merge", "--no-ff", "--no-edit", commit)
        merged = git(source, "rev-parse", "HEAD")
        require(git(source, "rev-list", "--parents", "-n", "1", merged).split() == [merged, prod, commit],
                "unexpected merge parents")
        version = git(source, "show", "HEAD:VERSION")
        require(version == evidence["version"], "merge changed release version")
        require(not git(source, "ls-remote", "--tags", "origin", "refs/tags/v" + version,
                        "refs/tags/v" + version + "^{}"), "immutable version tag already exists")
        require(heads(source) == (commit, prod), "release branch changed before push")
        record.update(merge_commit=merged, version=version, status="PUSHING")
        state.write(commit, sign(record, receipt_key))
        # Normal non-force push: concurrent non-fast-forward changes are rejected.
        # A failure or lost response leaves PUSHING; never retry blindly.
        git(source, "push", "origin", merged + ":refs/heads/prod")
        require(heads(source)[1] == merged, "prod push outcome unknown")
        record["status"] = "MERGED"
        signed = sign(record, receipt_key)
        state.write(commit, signed)
        return signed


def deploy_handoff(promotion, key, evidence, policy, root, now, approval=None, approval_key=None, finalization=None):
    receipt = verify(promotion, key)
    require(receipt.get("kind") == "promotion" and receipt.get("status") == "MERGED"
            and receipt.get("product") == gate.PRODUCT, "invalid promotion receipt")
    require(evidence["gate"] == "deployment" and evidence["commit"] == receipt["merge_commit"]
            and evidence["version"] == receipt["version"], "PROD build does not match promotion")
    require(evidence["immutable_image"] == f"localhost:9290/{gate.PRODUCT}/prod/{evidence['version']}@{evidence['image_digest']}",
            "unexpected deployment image repository")
    decision = gate.evaluate(evidence, policy, root, now, approval, approval_key)
    require(evidence.get('mode') == 'controlled-candidate-v1' and finalization is not None, 'missing controlled finalization receipt')
    finalized = verify(finalization, key)
    require(finalized.get('kind') == 'finalization' and finalized.get('status') == 'SUCCESS'
            and finalized.get('product') == gate.PRODUCT and finalized.get('commit') == evidence['commit']
            and finalized.get('version') == evidence['version'] and finalized.get('digest') == evidence['image_digest']
            and finalized.get('evidence_sha256') == hashlib.sha256(gate.canonical(evidence)).hexdigest(), 'finalization does not match approved evidence')
    expires = now + dt.timedelta(minutes=15)
    if approval is not None:
        expires = min(expires, gate.timestamp(approval["payload"]["expires_at"]))
    return sign({"schema_version": 2, "kind": "deployment", "product": gate.PRODUCT, 'finalization': finalization,
                 "promotion": promotion, "prod_job": evidence["job"], "prod_build": evidence["build"],
                 "commit": evidence["commit"], "version": evidence["version"],
                 "image": evidence["immutable_image"], "digest": evidence["image_digest"],
                 "decision": decision, "created_at": now.isoformat(),
                 "library_revision": policy.get("library_revision"), "deployment_script_revision": evidence["commit"],
                 "expires_at": expires.isoformat(),
                 "evidence_sha256": hashlib.sha256(gate.canonical(evidence)).hexdigest()}, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["review", "approve", "promote", "recover", "handoff"])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--approval-key-file", type=Path)
    parser.add_argument("--receipt-key-file", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--promotion", type=Path)
    parser.add_argument('--finalization', type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--state-directory", type=Path)
    args = parser.parse_args()
    try:
        evidence, policy = json.loads(args.evidence.read_bytes()), json.loads(args.policy.read_bytes())
        root, now = args.evidence.parent, dt.datetime.now(dt.timezone.utc)
        approval = json.loads(args.approval.read_bytes()) if args.approval else None
        approval_key = args.approval_key_file.read_bytes() if args.approval_key_file else None
        receipt_key = args.receipt_key_file.read_bytes() if args.receipt_key_file else None
        if args.command == "review":
            result = review(evidence, policy, root, now)
        elif args.command == "approve":
            result = approve(evidence, policy, root, json.loads(args.identity.read_bytes()), approval_key, now)
        elif args.command in ("promote", "recover"):
            result = promote(args.source, State(args.state_directory), evidence, policy, root, receipt_key, now,
                             approval, approval_key, recover=args.command == "recover")
        else:
            result = deploy_handoff(json.loads(args.promotion.read_bytes()), receipt_key, evidence, policy, root,
                                    now, approval, approval_key,
                                    json.loads(args.finalization.read_bytes()) if args.finalization else None)
        with args.output.open("xb") as output:
            output.write(gate.canonical(result))
    except Exception as exc:
        print("BLOCKED: " + (str(exc) if isinstance(exc, gate.InvalidEvidence) else "release control failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
