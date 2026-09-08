#!/usr/bin/env python3
"""Trusted macOS runner: signed request -> locked owner-runtime deploy -> receipt.

No agent/terminal is to invoke this against a live environment. The centrally
managed Jenkins deployment job owns this entry point and its final verification.
SQLite is never opened by this host process.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

spec = importlib.util.spec_from_file_location("release_promotion", Path(__file__).with_name("release-promotion.py"))
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
gate, require = control.gate, control.require
PROJECT = "shiba-goditch-prod"
DB_DESTINATION = "/app/data/db/app"


def request_identity(signed, key, now):
    request = control.verify(signed, key)
    require(request.get("schema_version") == 2 and request.get("kind") == "deployment"
            and request.get("product") == gate.PRODUCT, "wrong deployment request")
    require(gate.SHA.fullmatch(request.get("commit", "")) and gate.DIGEST.fullmatch(request.get("digest", "")),
            "invalid deployment identity")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", request.get("version", "")), "invalid deployment version")
    expected = f"localhost:9290/{gate.PRODUCT}/prod/{request['version']}@{request['digest']}"
    require(request.get("image") == expected, "unexpected immutable image repository")
    require(request.get("prod_job") == gate.PRODUCT + "/prod" and type(request.get("prod_build")) is int
            and request["prod_build"] > 0, "invalid PROD build coordinate")
    issued, expires = gate.timestamp(request["created_at"]), gate.timestamp(request["expires_at"])
    require(issued <= now < expires and 0 < (expires - issued).total_seconds() <= 900, "expired deployment request")
    promotion = control.verify(request["promotion"], key)
    require(promotion.get("kind") == "promotion" and promotion.get("status") == "MERGED"
            and promotion.get("product") == gate.PRODUCT and promotion.get("merge_commit") == request["commit"]
            and promotion.get("version") == request["version"], "missing matching promotion receipt")
    require(promotion.get("develop_job") == gate.PRODUCT + "/develop"
            and gate.SHA.fullmatch(promotion.get("source_commit", "")), "invalid develop provenance")
    require(request.get("decision", {}).get("decision") in {"PASS", "APPROVED_EXCEPTION"}, "deployment not authorized")
    final = control.verify(request['finalization'], key)
    require(final.get('kind') == 'finalization' and final.get('status') == 'SUCCESS' and final.get('product') == gate.PRODUCT
            and final.get('commit') == request['commit'] and final.get('digest') == request['digest']
            and final.get('version') == request['version'] and final.get('evidence_sha256') == request['evidence_sha256'],
            'deployment requires matching verified finalization')
    return request


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, **kwargs)
    require(result.returncode == 0, "runtime command failed: " + command[0])
    return result.stdout.strip()


def docker_json(*args):
    return json.loads(run(["docker", *args]))


def environment(container):
    result = {}
    for entry in container["Config"]["Env"]:
        name, value = entry.split("=", 1)
        require(name not in result, "duplicate container environment")
        result[name] = value
    return result


def target_container(containers, runtime):
    matches = [c for c in containers if c["Config"].get("Labels", {}).get("com.docker.compose.project") == PROJECT
               and c["Config"]["Labels"].get("com.docker.compose.service") == "app"]
    require(len(matches) == 1, "PROD app owner missing or ambiguous; cold start is not automatic")
    target = matches[0]
    require(target["State"].get("Running") is True, "PROD app is not running; recovery requires authorization")
    values = environment(target)
    require(values.get("APP_ENV") == "prod" and values.get("STORAGE_BUCKET") == "shiba-prod", "wrong runtime environment")
    db = (runtime / "data/db/app").resolve()
    mounts = [m for m in target.get("Mounts", []) if m["Destination"] == DB_DESTINATION]
    require(len(mounts) == 1 and mounts[0].get("Type") == "bind" and mounts[0].get("RW") is True
            and Path(mounts[0]["Source"]).resolve() == db, "PROD database mount mismatch")
    for other in containers:
        if other["Id"] == target["Id"] or not other["State"].get("Running"):
            continue
        for mount in other.get("Mounts", []):
            if mount.get("Type") == "bind":
                source = Path(mount["Source"]).resolve()
                require(not (db.is_relative_to(source) or source.is_relative_to(db)), "another container shares PROD database tree")
    return target


def containers():
    ids = run(["docker", "ps", "-aq"]).splitlines()
    require(ids, "Docker has no runtime containers")
    return docker_json("inspect", *ids)


def image_identity(request):
    images = docker_json("image", "inspect", request["image"])
    require(len(images) == 1, "candidate image unavailable; no implicit pull/rebuild")
    image = images[0]
    require(request["image"] in image.get("RepoDigests", []), "candidate registry digest mismatch")
    labels = image["Config"].get("Labels", {})
    require(labels.get("org.opencontainers.image.revision") == request["commit"]
            and labels.get("app.version") == request["version"] and labels.get("app.branch") == "prod"
            and labels.get("app.name") == gate.PRODUCT, "candidate image metadata mismatch")
    return image["Id"]


def no_host_writer(runtime):
    # Inventory open file descriptors only. Never sqlite3/open/read the live DB.
    directory = runtime / "data/db/app"
    require(directory.is_dir(), "PROD DB directory missing")
    result = subprocess.run(["lsof", "-t", "+D", str(directory)], capture_output=True, text=True, timeout=30)
    require(result.returncode in {0, 1} and not result.stderr.strip(), "host file-owner inventory unavailable")
    require(result.returncode == 1 and not result.stdout.strip(), "host process has PROD DB tree open")


def final_identity(request, image_id, runtime):
    target = target_container(containers(), runtime)
    values = environment(target)
    require(target["Image"] == image_id and image_identity(request) == image_id, "runtime image ID/digest mismatch")
    require(values.get("APP_VERSION") == request["version"] and values.get("BRANCH") == "prod", "runtime version mismatch")
    require(target["State"].get("Health", {}).get("Status") == "healthy", "runtime Docker health is not healthy")
    ports = target["NetworkSettings"]["Ports"].get("8090/tcp")
    require(ports == [{"HostIp": "127.0.0.1", "HostPort": "8090"}], "runtime port mapping mismatch")
    with urllib.request.urlopen("http://127.0.0.1:8090/api/heartbeat", timeout=5) as response:
        require(response.status == 200, "runtime heartbeat failed")
        response.read(65536)
    return {"container_id": target["Id"], "image_id": image_id, "commit": request["commit"],
            "version": request["version"], "digest": request["digest"], "compose_project": PROJECT,
            "database_access": "Docker owner locking domain; host file-owner inventory only", "heartbeat": 200}


def execute(signed, key, source, runtime, state, engine_id, now, output):
    request = request_identity(signed, key, now)
    require(sys.platform == "darwin", "runtime runner must be on the configured macOS owner host")
    require(runtime.is_absolute() and runtime.resolve() == runtime and runtime.is_dir(), "invalid fixed runtime root")
    require((runtime / ".env.prod").is_file(), "PROD runtime env file missing")
    require(control.git(source, "remote", "get-url", "origin") == control.REMOTE, "deployment source remote mismatch")
    require(control.git(source, "rev-parse", "HEAD") == request["commit"]
            and not control.git(source, "status", "--porcelain"), "deployment source checkout mismatch or dirty")
    require(run(["docker", "info", "--format", "{{.ID}}"] ) == engine_id and engine_id, "Docker engine identity mismatch")
    lock_dir = runtime / "data/locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / "prod.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise gate.InvalidEvidence("PROD lifecycle lock is busy")
        with state.lock():
            require(not state.exists(request["commit"]), "deployment already claimed; inspect receipt before any retry")
            require(control.heads(source)[1] == request["commit"], "prod branch advanced after promotion")
            # Same runtime lock covers classification, deploy and final verification.
            previous = target_container(containers(), runtime)
            no_host_writer(runtime)
            image_id = image_identity(request)
            request_identity(signed, key, dt.datetime.now(dt.timezone.utc))
            record = {"schema_version": 1, "kind": "runtime-deployment", "product": gate.PRODUCT,
                      "status": "CLAIMED", "request_sha256": hashlib.sha256(gate.canonical(signed)).hexdigest(),
                      "commit": request["commit"], "version": request["version"], "digest": request["digest"],
                      "previous_container_id": previous["Id"], "previous_image_id": previous["Image"],
                      "started_at": now.isoformat(), "automatic_rollback": False}
            backup_root = runtime / "data/backups/prod-predeploy"
            before = {p.name for p in backup_root.iterdir()} if backup_root.is_dir() else set()
            record.update(library_revision=request.get("library_revision"), deployment_script_revision=request["commit"],
                          deploy_journal=str(runtime / "data/logs/prod-deploy-journal.log"), backup_directory=str(backup_root))
            state.write(request["commit"], control.sign(record, key))
            try:
                env = {k: v for k, v in os.environ.items() if not k.startswith(("RELEASE_", "RECEIPT_", "APPROVAL_", "JENKINS_API_"))}
                env.update(SHIBA_CONTROLLED_DEPLOY="true", SHIBA_RUNTIME_ROOT=str(runtime),
                           SHIBA_EXPECTED_COMMIT=request["commit"], SHIBA_EXPECTED_VERSION=request["version"],
                           SHIBA_LIFECYCLE_LOCK_FD=str(fd), PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run(["bash", "scripts/deploy.sh", "prod", "deploy", request["image"]],
                                        cwd=source, env=env, pass_fds=(fd,), timeout=1800)
                require(result.returncode == 0, "controlled deploy failed; no automatic rollback")
                # Bounded read-only retries allow the image HEALTHCHECK to settle.
                deadline = time.monotonic() + 90
                while True:
                    try:
                        observed = final_identity(request, image_id, runtime)
                        break
                    except (gate.InvalidEvidence, OSError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(3)
                record.update(status="SUCCESS", observed=observed)
            except BaseException:
                record.update(status="FAILED", mutation_may_have_started=True,
                              follow_up="Preserve runtime, deploy journal and migration evidence; explicit authorization required")
                raise
            finally:
                record["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                # Only list artifact paths. Do not read a DB file on the host.
                record["new_backup_and_migration_files"] = sorted(str(p) for p in backup_root.iterdir()
                    if p.name not in before) if backup_root.is_dir() else []
                receipt = control.sign(record, key)
                state.write(request["commit"], receipt)
                with output.open("xb") as stream:
                    stream.write(gate.canonical(receipt))
            return receipt
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "deploy"])
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--docker-engine-id")
    args = parser.parse_args()
    try:
        signed, key, now = json.loads(args.request.read_bytes()), args.key_file.read_bytes(), dt.datetime.now(dt.timezone.utc)
        if args.command == "inspect":
            with args.output.open("xb") as output:
                output.write(gate.canonical(request_identity(signed, key, now)))
        else:
            execute(signed, key, args.source, args.runtime_root, control.State(args.state_directory),
                    args.docker_engine_id, now, args.output)
    except Exception as exc:
        print("BLOCKED: " + (str(exc) if isinstance(exc, gate.InvalidEvidence) else "runtime verification/deployment failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
