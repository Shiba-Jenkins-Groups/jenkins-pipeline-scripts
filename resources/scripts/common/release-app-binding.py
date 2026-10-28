#!/usr/bin/env python3
"""Native-only App identity handoff, imported by the trusted deployment owner.

The caller MUST validate the signed release request before begin(), retain its
App lifecycle EX descriptor, and call finish() only after final_identity(). This
module never deploys a container, opens SQLite, or retries a failed transaction.
All runtime effects belong to that same Jenkins owner invocation. No standalone
mutation CLI is provided: release-deploy.py is the signed public entry point.
"""
import copy
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time
import types
import uuid

SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SERVICES = {"app", "minio", "notify", "ocrsvc", "ollama", "omlx", "recognition"}
CONSUMER = "app-lifecycle-prod"
LABEL = "com.shiba.service-control.prod"
MAX_FILE = 128 << 20


class BindingError(ValueError):
    def __init__(self, code, receipt=None):
        super().__init__(code)
        self.receipt = receipt or {"status": "blocked", "code": code, "platform_mutated": False}


def require(condition, code):
    if not condition:
        raise BindingError(code)


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def encoded(value):
    # Match the installed setup/registry identity digest, including its ASCII
    # escaping of legitimate non-ASCII labels and qualification notes.
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def directory(path):
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)), "invalid_absolute_path")
    for parent in (path, *path.parents):
        info = parent.lstat()
        admin = sys.platform == "darwin" and info.st_uid == 0 and info.st_gid == 80
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.getuid()}
                and not info.st_mode & 0o002 and (not info.st_mode & 0o020 or admin), "unsafe_directory")
    return path


def read(path, mode=0o600):
    path = Path(path)
    directory(path.parent)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
            and stat.S_IMODE(before.st_mode) == mode, "unsafe_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        held = os.fstat(source.fileno())
        require((held.st_dev, held.st_ino) == (before.st_dev, before.st_ino), "file_changed")
        data = source.read(MAX_FILE + 1)
        after = os.fstat(source.fileno())
    require(len(data) <= MAX_FILE and (held.st_size, held.st_mtime_ns) ==
            (after.st_size, after.st_mtime_ns), "file_changed")
    return data


def document(path):
    return json.loads(read(path), object_pairs_hook=pairs)


def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    sync(Path(path).parent)


def replace(path, data):
    # Caller retains the original exact bytes in its private transaction folder.
    read(path)
    temporary = path.with_name(path.name + ".app-binding-" + os.urandom(8).hex())
    create(temporary, data)
    os.replace(temporary, path)
    sync(path.parent)


def resource_keys(service):
    if service.get("docker"):
        return ["docker:" + service["docker"]["engine_id"] + ":" + t["id"] for t in service["docker"]["targets"]]
    native = service["mac"]
    suffix = native["owner_executable"] if native["kind"] == "gui-app" else "port:" + str(native["port"])
    return ["mac:" + str(native["uid"]) + ":" + suffix]


def journal_idle(raw):
    require(not raw or raw.endswith(b"\n"), "partial_owner_journal")
    operations, fences, drains, recovery = {}, {}, {}, {}
    for line in raw.splitlines():
        require(0 < len(line) <= 65536, "invalid_owner_journal")
        record = json.loads(line, object_pairs_hook=pairs)
        kind = record.get("type")
        require(kind in {"operation", "request", "nonce", "intent", "fence", "mutation", "drain",
                         "drain_release", "recognition_self_stopped", "recovery"}, "unknown_journal_record")
        if kind == "operation":
            operation = record["operation"]
            operations[operation["id"]] = operation["state"]
        elif kind == "fence":
            fences[record["resource"]] = record.get("fence", "")
        elif kind == "drain":
            drains[(record["resource"], record["fence"])] = True
        elif kind == "drain_release":
            drains.pop((record["resource"], record["fence"]), None)
        elif kind == "recovery":
            recovery[record["recovery"]["service"]] = record["recovery"]
    require(all(value in {"succeeded", "blocked", "failed"} for value in operations.values())
            and not any(fences.values()) and not drains
            and all(r.get("outcome") in {"ready", "failed"} and not r.get("stop_requested")
                    and not r.get("stop_operation_id") for r in recovery.values()),
            "owner_operation_or_fence_active")


class NativeIO:
    root = Path.home() / "Library/Application Support/ShibaServiceControl"

    def command(self, argv):
        result = subprocess.run(argv, capture_output=True, timeout=20)
        require(result.returncode == 0, "native_binding_command_failed")
        return result.stdout

    def owner(self, transaction):
        require(sys.platform == "darwin", "native_owner_requires_macos")
        result = subprocess.run(["/bin/launchctl", "print", transaction.domain], capture_output=True, timeout=5)
        if result.returncode:
            # A command failure alone is not proof of an unloaded job.
            text = result.stderr.decode(errors="replace")
            require("Could not find service" in text or "Could not find specified service" in text,
                    "owner_launchd_observation_unknown")
            return None
        text = result.stdout.decode()
        match = re.search(r"^\s*pid = ([0-9]+)$", text, re.M)
        require(match is not None, "owner_loaded_without_process")
        pid = int(match.group(1))
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        buffer = ctypes.create_string_buffer(4096)
        require(library.proc_pidpath(pid, buffer, len(buffer)) > 0, "owner_kernel_path_unavailable")
        executable = buffer.value.decode()
        process = self.command(["/bin/ps", "-p", str(pid), "-o", "uid=,lstart="]).decode().strip()
        require(process.split(maxsplit=1)[0] == str(os.getuid())
                and executable == transaction.config["executable"], "foreign_owner_process")
        return {"pid": pid, "kernel_path": executable, "uid_start": process,
                "binary_sha256": sha(read(Path(executable), 0o700))}

    def pause(self, transaction):
        require(self.owner(transaction) == transaction.owner_before, "owner_changed_before_pause")
        self.command(["/bin/launchctl", "bootout", transaction.domain])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.owner(transaction) is None:
                    return
            except BindingError as error:
                if str(error) != "owner_loaded_without_process":
                    raise
            time.sleep(0.05)
        raise BindingError("owner_pause_unconfirmed")

    def resume(self, transaction):
        require(self.owner(transaction) is None, "foreign_owner_before_resume")
        self.command(["/bin/launchctl", "bootstrap", "gui/" + str(os.getuid()), str(transaction.plist)])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                current = self.owner(transaction)
            except BindingError as error:
                if str(error) != "owner_loaded_without_process":
                    raise
                current = None
            if current is not None:
                require(current["binary_sha256"] == transaction.owner_before["binary_sha256"]
                        and current["pid"] != transaction.owner_before["pid"], "owner_resume_identity_mismatch")
                # Listener + lifetime journal lock proves the same binary passed
                # VerifyStartupConfig and opened its freshly loaded manager.
                result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + transaction.config["listen"].rsplit(":", 1)[1],
                                         "-sTCP:LISTEN", "-t"], capture_output=True, timeout=5)
                listeners = set(result.stdout.decode().split())
                if result.returncode == 0 and listeners == {str(current["pid"])}:
                    fd = os.open(transaction.journal, os.O_RDWR | os.O_NOFOLLOW)
                    try:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return current
                    finally:
                        os.close(fd)
            time.sleep(0.05)
        raise BindingError("owner_resume_unconfirmed")

    def app(self, transaction, container_id):
        target = json.loads(self.command(["docker", "inspect", "--type", "container", container_id]))
        require(isinstance(target, list) and len(target) == 1, "app_container_missing")
        return target[0]

    def inventory_ids(self, transaction):
        # Absence requires a complete, successful exact-ID inventory. An
        # inspect error (including an unavailable Docker daemon) is not proof
        # that a formerly bound container has disappeared.
        raw = self.command(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"])
        values = raw.decode("ascii").splitlines()
        require(all(SHA.fullmatch(value) for value in values) and len(values) == len(set(values)),
                "invalid_docker_inventory")
        return set(values)

    def engine(self, transaction):
        return self.command(["docker", "info", "--format", "{{.ID}}"]).decode().strip()

    def image(self, transaction):
        values = json.loads(self.command(["docker", "image", "inspect", transaction.request["image"]]))
        require(len(values) == 1, "target_image_missing")
        return values[0]["Id"]

    def dependency_status(self, transaction, name):
        # The verified installed native binary owns authentication and its
        # current snapshot contract. Status performs metadata GETs, never OCR,
        # warmup, inference or a physical service-control action. Its legitimate
        # nonce audit append remains in the preserved historical journal.
        require(name in {"ocrsvc", "recognition"}, "unexpected_dependency_status")
        value = json.loads(self.command([transaction.config["executable"], "client", "--root", str(transaction.root),
                                        "--environment", "prod", "--service", name, "--action", "status"]))
        response = value["response"]
        require(response.get("kind") == "status" and len(response.get("services", [])) == 1,
                "invalid_dependency_status")
        return response["services"][0]

    def checkpoint(self, phase, transaction):
        pass  # Fault injection belongs only to LOCAL fixtures.


class AppBinding:
    @classmethod
    def repair(cls, verified_request, observed, runtime, lifecycle_fd, expected_binding_hashes, *, io=None):
        """Repair one ready binding after the trusted runner verifies receipts.

        HMAC, request expiry and the independent one-attempt state belong to
        that runner. This helper consumes its verified identities, performs no
        deployment, and never grants an admitted SH child lease.
        """
        transaction = cls(verified_request, runtime, lifecycle_fd, io or NativeIO())
        try:
            operation = verified_request.get("operation_id", "")
            try:
                identity = uuid.UUID(operation)
            except (ValueError, AttributeError, TypeError):
                raise BindingError("invalid_repair_operation_id") from None
            require(str(identity) == operation and identity.version == 4, "invalid_repair_operation_id")
            require(isinstance(expected_binding_hashes, dict) and set(expected_binding_hashes) ==
                    {"owner.json", "registry.json", "installation.json", "consumer-admission.json"}
                    and all(isinstance(value, str) and SHA.fullmatch(value)
                            for value in expected_binding_hashes.values()), "invalid_repair_binding_hashes")
            require(SHA.fullmatch(verified_request.get("previous_container_id", ""))
                    and DIGEST.fullmatch(verified_request.get("previous_image_id", "")),
                    "invalid_repair_previous_identity")
            transaction.id = operation
            transaction.repair_mode = True
            transaction.binding_hashes = copy.deepcopy(expected_binding_hashes)
            transaction.repair_observed = copy.deepcopy(observed)
            transaction.prepare()
            return transaction.finish(observed)
        except BaseException as error:
            if isinstance(error, BindingError) and error.receipt.get("transaction_id") == transaction.id:
                raise
            public = transaction.failure(error)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise BindingError("app_binding_repair_failed_preserved", public) from None
        finally:
            transaction.close()

    @classmethod
    def begin(cls, request, runtime, lifecycle_fd, *, io=None):
        io = io or NativeIO()
        root = Path(io.root)
        if not (root / "consumer-admission.json").exists():
            # Missing global config never disables a previously activated guard.
            for environment in ("prod", "dev"):
                receipt = root / environment / "installation.json"
                if receipt.exists():
                    installed = document(receipt)
                    require(isinstance(installed, dict) and installed.get("state") in {"prepared", "ready"}
                            and installed.get("identity", {}).get("environment") == environment
                            and installed["identity"].get("owner_uid") == os.getuid(), "invalid_installation_receipt")
                    require(not (installed.get("state") == "ready" and
                                 installed.get("identity", {}).get("consumer_config_digest")),
                            "activated_admission_registry_missing")
            return None
        transaction = cls(request, runtime, lifecycle_fd, io)
        try:
            transaction.prepare()
            return transaction
        except BaseException as error:
            public = transaction.failure(error)
            transaction.close()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise BindingError("app_binding_begin_failed_preserved", public) from None

    def __init__(self, request, runtime, lifecycle_fd, io):
        self.io, self.root = io, Path(io.root)
        self.request, self.runtime, self.lifecycle_fd = copy.deepcopy(request), Path(runtime), lifecycle_fd
        self.owner_directory = self.root / "prod"
        self.plist = self.root / (LABEL + ".plist")
        self.domain = "gui/" + str(os.getuid()) + "/" + LABEL
        self.resources = self.root / "resource-locks"
        self.journal = self.owner_directory / "operations.jsonl"
        self.pending = self.owner_directory / "owner-upgrade.pending.json"
        self.id = os.urandom(16).hex()
        self.backup = None
        self.fds, self.lock_fds, self.owned_pending = {}, [], []
        self.owner_paused = False
        self.owner_pause_requested = False
        self.phase = "preflight"
        self.completed = False
        self.repair_mode = False

    def lock(self, path, create_missing=False):
        directory(path.parent)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create_missing else 0), 0o600)
        self.lock_fds.append(fd)
        held = os.fstat(fd)
        require(stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid()
                and stat.S_IMODE(held.st_mode) == 0o600, "unsafe_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def prefix(self, key):
        return self.resources / sha(key.encode())

    def event(self, phase, **details):
        self.phase = phase
        if self.backup is not None:
            create(self.backup / (phase + ".json"), encoded({"transaction_id": self.id, "at": stamp(), **details}) + b"\n")
        self.io.checkpoint(phase, self)

    def source_pins(self):
        paths = [self.root / "service-consumer-admission.py", self.plist,
                 Path(self.installed["login_agent_path"]),
                 self.owner_directory / "credentials-stage0.json"]
        paths += [self.release / "manifest.json"]
        paths += [self.release / name for name in self.manifest["files"]]
        credentials = self.installed["credential_identity"]
        for location in (self.owner_directory / "credentials", Path(credentials["client_directory"])):
            directory(location)
            paths += list(location.iterdir())
        paths += [self.owner_directory / "drain.token"]
        paths += list((self.owner_directory / "consumers").glob("*.token"))
        result = {}
        for path in sorted(set(paths)):
            mode = (0o700 if path == self.root / "service-consumer-admission.py" else
                    self.artifact_modes[path.name] if path.parent == self.release and path.name != "manifest.json" else 0o600)
            require(mode in {0o600, 0o700}, "protected_file_mode_changed")
            result[str(path)] = sha(read(path, mode))
        return result

    def assert_pending(self):
        owned = set(self.owned_pending)
        for path in self.resources.glob("*.pending"):
            require(path in owned and read(path) == (self.id + "\n").encode(), "foreign_resource_fence")
        require(not list(self.owner_directory.glob("*.lifecycle.pending")), "foreign_lifecycle_fence")
        for path in self.owner_directory.glob("*.pending.json"):
            require(path == self.pending and self.backup is not None and
                    read(path) == self.pending_raw, "foreign_installation_pending")
        for path in (self.resources / "consumer-jobs").iterdir():
            require(getattr(self, "job_path", None) == path and read(path) == self.job_raw,
                    "foreign_consumer_job")

    def validate_target(self, raw, expected, new=False, retired=False):
        require(raw["Id"] == expected["id"] and raw["Image"] == expected["image_id"], "app_docker_identity_mismatch")
        if retired:
            require(raw["State"].get("Running") is False and raw["State"].get("Restarting") is False
                    and not raw["State"].get("Paused"), "previous_app_not_inactive")
        else:
            require(raw["State"].get("Running") is True and not raw["State"].get("Restarting")
                    and raw["State"].get("Health", {}).get("Status") == "healthy", "app_not_healthy")
        labels = raw["Config"].get("Labels") or {}
        require(all(labels.get(key) == value for key, value in expected["labels"].items()), "app_labels_mismatch")
        require(raw["HostConfig"]["RestartPolicy"]["Name"] == expected["restart_policy"] == "unless-stopped",
                "app_restart_policy_changed")
        ports = raw["HostConfig"].get("PortBindings", {}) if retired else raw["NetworkSettings"]["Ports"]
        require(ports.get("8090/tcp") == [{"HostIp": "127.0.0.1", "HostPort": "8090"}],
                "app_port_changed")
        mounts = [m for m in raw["Mounts"] if m["Destination"] == "/app/data/db/app"]
        require(len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["RW"] is True
                and mounts[0]["Source"] == str(self.runtime / "data/db/app"), "app_database_mount_changed")
        if new:
            values = dict(value.split("=", 1) for value in raw["Config"]["Env"])
            require(values.get("APP_VERSION") == self.request["version"] and values.get("BRANCH") == "prod",
                    "app_runtime_version_changed")

    def prepare(self):
        directory(self.root)
        require(stat.S_IMODE(self.root.stat().st_mode) == 0o700, "private_root_required")
        directory(self.runtime)
        require(COMMIT.fullmatch(self.request.get("commit", "")) and DIGEST.fullmatch(self.request.get("digest", ""))
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.request.get("version", ""))
                and self.request.get("image") == "localhost:9290/shiba-go-ditch-api-project/prod/" +
                self.request["version"] + "@" + self.request["digest"], "invalid_verified_deployment_identity")
        held, target = os.fstat(self.lifecycle_fd), (self.runtime / "data/locks/prod.lock").lstat()
        require(self.lifecycle_fd >= 3 and stat.S_ISREG(held.st_mode) and held.st_uid == os.getuid()
                and stat.S_IMODE(held.st_mode) == 0o600 and
                (held.st_dev, held.st_ino) == (target.st_dev, target.st_ino), "invalid_inherited_lifecycle_lock")
        # A separately opened descriptor cannot take SH while the inherited
        # descriptor really owns EX. Never upgrade a caller's unheld/SH lease.
        probe = os.open(self.runtime / "data/locks/prod.lock", os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise BindingError("inherited_lifecycle_ex_required")
        finally:
            os.close(probe)
        self.lock(self.owner_directory / "owner-upgrade.lock", True)
        self.lock(self.root / "consumer-helper-upgrade.lock", True)
        self.assert_binding_hashes()
        self.original = {name: read(self.owner_directory / name) for name in ("owner.json", "registry.json", "installation.json")}
        self.global_raw = read(self.root / "consumer-admission.json")
        self.config, self.registry, self.installed = [json.loads(self.original[name], object_pairs_hook=pairs)
                                                    for name in ("owner.json", "registry.json", "installation.json")]
        self.consumers = json.loads(self.global_raw, object_pairs_hook=pairs)
        require(self.installed.get("state") == "ready" and self.installed["identity"]["environment"] == "prod"
                and self.installed["identity"]["owner_uid"] == os.getuid() and self.config["environment"] == "prod"
                and self.config["enabled"] is True and set(self.config["services"]) == SERVICES,
                "active_prod_installation_required")
        require(self.installed["owner_config_sha256"] == sha(self.original["owner.json"])
                and self.installed["identity"]["registry_digest"] == sha(encoded(self.registry))
                and self.installed["identity"]["consumer_config_digest"] == sha(encoded(self.consumers)),
                "installed_binding_drift")
        self.release = self.root / "releases" / (self.installed["identity"]["version"] + "-" + self.installed["identity"]["source_commit"])
        manifest = self.manifest = document(self.release / "manifest.json")
        require(sha(encoded(manifest)) == self.installed["identity"]["artifact_digest"]
                and manifest.get("source_dirty") is False and manifest["source_commit"] == self.installed["identity"]["source_commit"]
                and manifest["version"] == self.installed["identity"]["version"], "native_artifact_identity_changed")
        self.artifact_modes = {}
        for name, digest in manifest["files"].items():
            require(Path(name).name == name and SHA.fullmatch(digest), "invalid_native_artifact_manifest")
            mode = stat.S_IMODE((self.release / name).lstat().st_mode)
            require(mode in {0o600, 0o700}, "unsafe_native_artifact")
            require(sha(read(self.release / name, mode)) == digest, "native_artifact_changed")
            self.artifact_modes[name] = mode
        require(self.config["executable"] == str(self.release / "owner-darwin-arm64")
                and self.config["driver_directory"] == str(self.release)
                and self.config["journal_file"] == str(self.journal)
                and self.config["resource_lock_directory"] == str(self.resources)
                and self.consumers["resource_lock_directory"] == str(self.resources), "native_owner_layout_changed")
        helper_path = self.root / "service-consumer-admission.py"
        require(sha(read(helper_path, 0o700)) == self.installed["consumer_helper_sha256"] == manifest["files"][helper_path.name],
                "consumer_helper_changed")
        # Execute only already hash-verified installed helper bytes, without
        # importlib's implicit __pycache__ writes to the managed installation.
        self.helper = types.ModuleType("verified_installed_app_admission")
        self.helper.__file__ = str(helper_path)
        exec(compile(read(helper_path, 0o700), str(helper_path), "exec"), self.helper.__dict__)
        self.pins = self.source_pins()
        require(self.pins[str(self.plist)] == self.installed["login_agent_sha256"]
                == self.pins[self.installed["login_agent_path"]], "owner_launchagent_changed")
        plist = plistlib.loads(read(self.plist))
        require(plist["Label"] == LABEL and plist["ProgramArguments"] ==
                [self.config["executable"], "--config", str(self.owner_directory / "owner.json")]
                and plist["RunAtLoad"] is True and plist["KeepAlive"] is True, "owner_launchagent_contract_changed")
        credentials = self.installed["credential_identity"]
        require(credentials["host_root"] == str(self.root) and credentials["owner_directory"] == str(self.owner_directory)
                and credentials["owner_uid"] == os.getuid() and credentials["environment"] == "prod"
                and sha(read(Path(credentials["client_directory"]) / "client.json")) == self.installed["client_config_sha256"]
                and sha(read(self.owner_directory / "credentials-stage0.json")) == self.installed["credential_stage0_receipt_sha256"],
                "credential_binding_changed")
        app = self.config["services"]["app"]
        require(app["platform"] == "docker_container" and app["actions"] == ["status"]
                and len(app["docker"]["targets"]) == 1 and app["lifecycle_lock"] == str(self.runtime / "data/locks/prod.lock"),
                "app_status_only_required")
        require(self.registry["services"]["app"]["docker"] == app["docker"]
                and self.registry["services"]["app"]["revision"] == app["revision"], "app_registry_drift")
        self.old_target = app["docker"]["targets"][0]
        self.old_key = resource_keys(app)[0]
        if self.repair_mode:
            require(self.old_target["id"] == self.request["previous_container_id"]
                    and self.old_target["image_id"] == self.request["previous_image_id"],
                    "repair_previous_binding_mismatch")
        require(self.old_target["labels"].get("com.docker.compose.project") == "shiba-goditch-prod"
                and self.old_target["labels"].get("com.docker.compose.service") == "app", "wrong_app_target")
        self.scope = sorted(self.consumers["consumers"][CONSUMER]["resource_keys"])
        require(self.old_key in self.scope and not self.consumers["consumers"][CONSUMER].get("shared_lifecycle_locks"),
                "unexpected_deploy_admission_scope")
        self.keys = sorted(set(self.scope) | {key for service in self.config["services"].values() for key in resource_keys(service)})
        require(len(self.keys) <= 32, "oversized_resource_scope")
        for key in self.keys:
            self.fds[key] = self.lock(self.prefix(key).with_suffix(".lock"), True)
        self.assert_pending()
        self.states = {}
        for key in self.keys:
            path = self.prefix(key).with_suffix(".state.json")
            self.helper.check_admission(self.consumers, key)
            self.states[str(path)] = sha(read(path))
        journal_idle(read(self.journal))
        if not self.repair_mode:
            self.validate_target(self.io.app(self, self.old_target["id"]), self.old_target)
        require(self.io.engine(self) == app["docker"]["engine_id"], "docker_engine_changed")
        self.expected_image_id = self.io.image(self)
        require(DIGEST.fullmatch(self.expected_image_id), "invalid_target_image_id")
        if self.repair_mode:
            self.assert_repair_runtime()
        self.owner_before = self.io.owner(self)
        require(self.owner_before is not None and self.owner_before["binary_sha256"] == manifest["files"]["owner-darwin-arm64"],
                "installed_owner_not_running")
        self.dependencies = {}
        for name in ("ocrsvc", "recognition"):
            snapshot = self.io.dependency_status(self, name)
            require(snapshot.get("id") == name and snapshot.get("environment") == "prod"
                    and snapshot.get("registry_revision") == self.config["services"][name]["revision"]
                    and snapshot.get("desired_state") == "running" and snapshot.get("process_state") == "running"
                    and snapshot.get("contract_health") == "ready", "deployment_dependency_not_ready")
            self.dependencies[name] = {field: snapshot[field] for field in
                                       ("id", "environment", "registry_revision", "desired_state", "process_state", "contract_health")}
        # Recheck the live audit after status nonce appends, before writing a
        # transaction/pending or pausing the only recovery owner.
        journal_idle(read(self.journal))
        self.assert_binding_hashes()
        self.backup = self.owner_directory / ("app-binding-" + self.id)
        self.backup.mkdir(mode=0o700)
        sync(self.owner_directory)
        for name, data in {**self.original, "consumer-admission.json": self.global_raw,
                           "owner.plist": read(self.plist), "operations-before-pause.jsonl": read(self.journal)}.items():
            create(self.backup / name, data)
        state_backup = self.backup / "resource-states"
        state_backup.mkdir(mode=0o700)
        sync(self.backup)
        for path, digest in self.states.items():
            data = read(Path(path))
            require(sha(data) == digest, "resource_intent_changed_before_backup")
            create(state_backup / Path(path).name, data)
        self.pending_raw = encoded({"schema_version": 1, "kind": "active-app-binding", "transaction_id": self.id,
                                    "request_sha256": sha(encoded(self.request)), "backup": str(self.backup),
                                    "old_app_key": self.old_key, "started_at": stamp()}) + b"\n"
        create(self.pending, self.pending_raw)
        for key in self.keys:
            path = self.prefix(key).with_suffix(".pending")
            create(path, (self.id + "\n").encode())
            self.owned_pending.append(path)
        self.event("authorized", owner_before=self.owner_before, pins=self.pins, state_pins=self.states,
                   dependencies=self.dependencies)
        # Resource EX + pending prevents any new physical mutation. A raced
        # accepted request is detected on both sides of managed owner pause.
        journal_idle(read(self.journal))
        self.assert_sources()
        if self.repair_mode:
            self.assert_repair_runtime()
        self.owner_pause_requested = True
        self.io.pause(self)
        self.owner_paused = True
        self.event("owner-paused")
        self.journal_fd = self.lock(self.journal)
        self.journal_raw = read(self.journal)
        journal_idle(self.journal_raw)
        create(self.backup / "operations-after-pause.jsonl", self.journal_raw)
        self.assert_pending()
        self.assert_sources()
        if self.repair_mode:
            self.assert_repair_runtime()
            self.event("repair-ready")
            return
        self.job_path = self.resources / "consumer-jobs" / (self.id + ".json")
        self.job = {"schema_version": 1, "job_id": self.id, "consumer_id": CONSUMER,
                    "resource_keys": self.scope, "started_at": stamp()}
        self.job_raw = encoded(self.job) + b"\n"
        create(self.job_path, self.job_raw)
        for key in self.scope:
            fcntl.flock(self.fds[key], fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.event("deploy-admitted")

    def assert_sources(self):
        self.assert_binding_hashes()
        require(self.source_pins() == self.pins, "protected_source_or_credential_changed")
        for name, data in self.original.items():
            require(read(self.owner_directory / name) == data, "installed_binding_changed")
        require(read(self.root / "consumer-admission.json") == self.global_raw, "global_admission_changed")
        require(all(sha(read(Path(path))) == value for path, value in self.states.items()), "resource_intent_changed")

    def assert_binding_hashes(self):
        if self.repair_mode:
            for name, digest in self.binding_hashes.items():
                path = self.root / name if name == "consumer-admission.json" else self.owner_directory / name
                require(sha(read(path)) == digest, "repair_binding_hash_mismatch")

    def qualify_observed(self, observed):
        require(isinstance(observed, dict)
                and all(observed.get(name) == self.request[name] for name in ("commit", "version", "digest"))
                and observed.get("image_id") == self.expected_image_id and observed.get("heartbeat") == 200
                and observed.get("compose_project") == "shiba-goditch-prod"
                and SHA.fullmatch(observed.get("container_id", "")), "unqualified_deployment_observation")
        require(observed["container_id"] != self.old_target["id"], "new_app_container_required")

    def target_for(self, container_id):
        target = copy.deepcopy(self.old_target)
        target.update(id=container_id, image_id=self.expected_image_id)
        target["labels"]["org.opencontainers.image.revision"] = self.request["commit"]
        if "org.opencontainers.image.version" in target["labels"]:
            target["labels"]["org.opencontainers.image.version"] = self.request["version"]
        return target

    def assert_repair_runtime(self):
        self.qualify_observed(self.repair_observed)
        new_key = "docker:" + self.config["services"]["app"]["docker"]["engine_id"] + ":" + self.repair_observed["container_id"]
        require(not self.prefix(new_key).with_suffix(".state.json").exists()
                and not self.prefix(new_key).with_suffix(".pending").exists()
                and all(new_key not in entry["resource_keys"] for entry in self.consumers["consumers"].values()),
                "new_resource_already_has_intent")
        ids = self.io.inventory_ids(self)
        require(isinstance(ids, set) and all(isinstance(value, str) and SHA.fullmatch(value) for value in ids)
                and self.repair_observed["container_id"] in ids, "repair_target_inventory_mismatch")
        if self.old_target["id"] in ids:
            self.validate_target(self.io.app(self, self.old_target["id"]), self.old_target, retired=True)
        self.validate_target(self.io.app(self, self.repair_observed["container_id"]),
                             self.target_for(self.repair_observed["container_id"]), True)
        require(self.io.engine(self) == self.config["services"]["app"]["docker"]["engine_id"]
                and self.io.image(self) == self.expected_image_id, "docker_target_changed")

    def env_for_deploy(self, base_env):
        require(self.phase == "deploy-admitted", "binding_not_admitted")
        require(not any(name in base_env for name in ("SHIBA_SERVICE_CONTROL_JOB_ID", "SHIBA_SERVICE_CONTROL_ADMISSION_FDS")),
                "foreign_inherited_admission")
        return dict(base_env, SHIBA_SERVICE_CONTROL_ADMISSION_FILE=str(self.root / "consumer-admission.json"),
                    SHIBA_SERVICE_CONTROL_ADMISSION_FDS=json.dumps({key: self.fds[key] for key in self.scope}),
                    SHIBA_SERVICE_CONTROL_JOB_ID=self.id, SHIBA_SERVICE_CONTROL_CONSUMER_ID=CONSUMER,
                    SHIBA_SERVICE_CONTROL_REEXEC_CONSUMER=CONSUMER, SHIBA_LIFECYCLE_LOCK_FD=str(self.lifecycle_fd),
                    SHIBA_RUNTIME_ROOT=str(self.runtime), PYTHONDONTWRITEBYTECODE="1")

    def pass_fds(self):
        return tuple([self.lifecycle_fd] + [self.fds[key] for key in self.scope])

    def finish(self, observed):
        try:
            require(self.phase == ("repair-ready" if self.repair_mode else "deploy-admitted")
                    and not self.completed, "binding_finish_not_repeatable")
            for fd in self.fds.values():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_pending()
            self.assert_sources()
            require(read(self.journal) == self.journal_raw and self.io.owner(self) is None, "paused_owner_state_changed")
            self.qualify_observed(observed)
            if self.repair_mode:
                require(observed == self.repair_observed, "repair_observation_changed")
                self.assert_repair_runtime()
            new_id = observed["container_id"]
            self.new_key = "docker:" + self.config["services"]["app"]["docker"]["engine_id"] + ":" + new_id
            require(not self.prefix(self.new_key).with_suffix(".state.json").exists()
                    and not self.prefix(self.new_key).with_suffix(".pending").exists(), "new_resource_already_has_intent")
            self.fds[self.new_key] = self.lock(self.prefix(self.new_key).with_suffix(".lock"), True)
            new_target = self.target_for(new_id)
            self.validate_target(self.io.app(self, new_id), new_target, True)
            require(self.io.engine(self) == self.config["services"]["app"]["docker"]["engine_id"]
                    and self.io.image(self) == self.expected_image_id, "docker_target_changed")
            new_pending = self.prefix(self.new_key).with_suffix(".pending")
            create(new_pending, (self.id + "\n").encode())
            self.owned_pending.append(new_pending)
            config, registry, consumers, installed = map(copy.deepcopy, (self.config, self.registry, self.consumers, self.installed))
            for app in (config["services"]["app"], registry["services"]["app"]):
                app["docker"]["targets"] = [new_target]
                app["revision"] += 1
            registry["registry_revision"] += 1
            registry["evidence"]["app"] = {"verified": True, "checked_at": stamp(),
                "note": "Status identity only, trusted owner deployment " + self.request["version"] +
                        " commit " + self.request["commit"] + " digest " + self.request["digest"] +
                        "; active binding transaction " + self.id + ". No App control grant added."}
            for entry in consumers["consumers"].values():
                if self.old_key in entry["resource_keys"]:
                    entry["resource_keys"] = [self.new_key if key == self.old_key else key for key in entry["resource_keys"]]
            installed["owner_config_sha256"] = sha(encoded(config) + b"\n")
            installed["identity"]["registry_digest"] = sha(encoded(registry))
            installed["identity"]["consumer_config_digest"] = sha(encoded(consumers))
            after = {self.owner_directory / "owner.json": encoded(config) + b"\n",
                     self.owner_directory / "registry.json": encoded(registry) + b"\n",
                     self.root / "consumer-admission.json": encoded(consumers) + b"\n",
                     self.owner_directory / "installation.json": encoded(installed) + b"\n"}
            self.event("commit-authorized", observed=observed,
                       after_sha256={str(path): sha(data) for path, data in after.items()})
            state = {"schema_version": 1, "resource_key": self.new_key, "desired_state": "running", "updated_at": stamp()}
            create(self.prefix(self.new_key).with_suffix(".state.json"), encoded(state) + b"\n")
            self.event("new-intent-written")
            if not self.repair_mode:
                self.job["resource_keys"] = sorted(self.new_key if key == self.old_key else key for key in self.scope)
                self.job_raw = encoded(self.job) + b"\n"
                replace(self.job_path, self.job_raw)
            for path, data in after.items():
                replace(path, data)
                self.event("committed-" + path.name)
            require(all(read(path) == data for path, data in after.items())
                    and self.source_pins() == self.pins and read(self.journal) == self.journal_raw,
                    "binding_commit_verification_failed")
            self.assert_pending()
            # Same binary reopens the unchanged historical journal only after
            # every pointer and new intent is committed. Pending resource files
            # still reject all new work until resume has been observed.
            os.close(self.journal_fd)
            self.lock_fds.remove(self.journal_fd)
            os.rename(self.pending, self.backup / "owner-upgrade.pending.committed.json")
            sync(self.owner_directory)
            sync(self.backup)
            try:
                owner_after = self.io.resume(self)
            except BaseException:
                # A partial bootstrap may be live; do not kill it or reopen
                # admission. Startup remains closed if that process exits.
                create(self.pending, self.pending_raw)
                raise
            self.owner_paused = False
            self.event("owner-resumed", owner_after=owner_after)
            require(all(read(path) == data for path, data in after.items()) and self.source_pins() == self.pins,
                    "binding_changed_during_owner_resume")
            if not self.repair_mode:
                require(read(self.job_path) == self.job_raw, "foreign_job_before_release")
                self.job_path.unlink()
                sync(self.job_path.parent)
            for path in self.owned_pending:
                require(read(path) == (self.id + "\n").encode(), "foreign_pending_before_release")
                path.unlink()
            sync(self.resources)
            self.completed = True
            result = {"schema_version": 1, "kind": "active-app-binding", "status": "completed",
                      "transaction_id": self.id, "backup": str(self.backup), "old_app_key": self.old_key,
                      "new_app_key": self.new_key, "owner_binary_unchanged": True,
                      "other_services_unchanged": True, "credentials_unchanged": True,
                      "historical_journal_preserved": True, "app_control": "status-only"}
            if self.repair_mode:
                result["mode"] = "ready-repair"
            create(self.backup / "completed.json", encoded(result) + b"\n")
            return result
        except BaseException as error:
            public = self.failure(error)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise BindingError("app_binding_finish_failed_preserved", public) from None

    def failure(self, error):
        public = {"schema_version": 1, "kind": "active-app-binding",
                  "status": "outcome-unconfirmed" if self.completed else "pending-preserved",
                  "transaction_id": self.id, "phase": self.phase, "owner_paused": self.owner_paused,
                  "owner_pause_requested": self.owner_pause_requested,
                  "owner_state": "paused" if self.owner_paused else "unconfirmed" if self.owner_pause_requested else "unchanged-before-pause",
                  "backup": str(self.backup) if self.backup else None,
                  "code": str(error) if isinstance(error, BindingError) else type(error).__name__,
                  "automatic_retry": False, "automatic_rollback": False}
        if self.completed:
            # The last evidence write can fail after a fully verified commit,
            # managed resume and admission release. Do not claim fences remain
            # or retry a completed physical transaction to recreate a receipt.
            public.update(binding_committed=True, admission_released=True, owner_state="resumed")
        if self.backup is not None:
            try:
                create(self.backup / ("failure-" + os.urandom(4).hex() + ".json"), encoded(public) + b"\n")
            except OSError:
                pass
        return public

    def close(self):
        for fd in reversed(self.lock_fds):
            os.close(fd)
        self.lock_fds = []


if __name__ == "__main__":
    print("Import AppBinding from the signed Jenkins release-deploy owner; no standalone mutation entry point.")
