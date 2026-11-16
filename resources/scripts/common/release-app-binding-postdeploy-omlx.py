#!/usr/bin/env python3
"""Qualify only the oMLX identity change authorized for App owner build #101.

The post-deployment incident runner owns the locks, backups, signed request,
config writes and owner recovery. This helper has no standalone mutation entry.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys


spec = importlib.util.spec_from_file_location("postdeploy_binding_omlx", Path(__file__).with_name("release-app-binding.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
need = B.require

OWNER_UID = 501
OWNER_GID = 20
BUNDLE = Path("/Applications/oMLX.app")
BUNDLE_ID = "app.omlx"
VERSION = "0.7.3"
BUILD = "3574"
TEAM_ID = "PSK5Q5T46L"
PROGRAM = BUNDLE / "Contents/Resources/Python/cpython-3.11/bin/python3.11"
GUI = BUNDLE / "Contents/MacOS/oMLX"
OLD_PROGRAM_SHA = "27ae417d4ddeba26b8f1c1ce8355d1c39cf7496682901d4fc166bf34fb7141ee"
OLD_GUI_SHA = "3e994a1b94d15a550ffe4f8376487cbdcef775b1bad78af0b159b41ccd891e96"
PROGRAM_SHA = "a59b7416fa2dc0a94e750bb72ff90f03b0fbbc06dbe3763b158f2cf2d19ea6f2"
GUI_SHA = "f331b32de359c3ec029efbfa92a3b6a780fe032938eff21ed91b1af557ad2512"
REVIEWED_AT = "2026-11-16T12:50:32Z"
RELEASE_URL = "https://github.com/jundot/omlx/releases/tag/v0.7.3"

# Raw SHA-256 values recorded in #101's commit-authorized.json after the App
# deployment. In addition to the checks below, require unchanged full scope:
# principal grants, resources, paths and all other service bindings.
COMMITTED = {
    "owner.json": "a4e1ebe30c3605b6e51f7f649781c97b0e0f4470da6f239838c989fd5f5beed9",
    "registry.json": "a033ce6a3c6860377a47eca58e54b835b24f59b0c11066057864f5f51997fe9f",
    "installation.json": "2aefb34b4c8d16e92925afd391b008bbfbf3a9cd0bfd47bb7e91b8f7deb483d4",
    "consumer-admission.json": "78660d4f74339ae4b9e5884391f8a5e51d39f02772f946e04ff9edd0f4bd574a",
}


def _file_identity(path, info):
    return {
        "path": str(path), "device": info.st_dev, "inode": info.st_ino,
        "uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size, "mtime_ns": info.st_mtime_ns,
    }


def _read_identity(path, source, mode, digest):
    before = os.fstat(source.fileno())
    observed = _file_identity(path, before)
    need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == OWNER_UID
         and before.st_gid == OWNER_GID and stat.S_IMODE(before.st_mode) == mode,
         "omlx_executable_permissions_changed")
    need(observed == _file_identity(path, path.lstat()), "omlx_executable_inode_changed")
    sha = hashlib.sha256()
    source.seek(0)
    for block in iter(lambda: source.read(1 << 20), b""):
        sha.update(block)
    need(sha.hexdigest() == digest, "omlx_executable_hash_changed")
    need(observed == _file_identity(path, os.fstat(source.fileno())) == _file_identity(path, path.lstat()),
         "omlx_executable_inode_changed")
    return dict(observed, sha256=digest)


def _open_binary(path):
    B.directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    return os.fdopen(fd, "rb")


def _verify_bundle():
    need(sys.platform == "darwin" and os.getuid() == OWNER_UID, "omlx_darwin_owner_required")
    info_path = BUNDLE / "Contents/Info.plist"
    with _open_binary(info_path) as blob:
        file_info = os.fstat(blob.fileno())
        need(stat.S_ISREG(file_info.st_mode) and file_info.st_uid == OWNER_UID and not file_info.st_mode & 0o022
             and _file_identity(info_path, file_info) == _file_identity(info_path, info_path.lstat()),
             "omlx_bundle_info_permissions_changed")
        raw = blob.read(1 << 20)
    info = plistlib.loads(raw)
    need(info.get("CFBundleIdentifier") == BUNDLE_ID and info.get("CFBundleShortVersionString") == VERSION
         and info.get("CFBundleVersion") == BUILD and info.get("CFBundleExecutable") == GUI.name,
         "omlx_bundle_version_changed")
    requirement = ('=identifier "' + BUNDLE_ID + '" and anchor apple generic and '
                   'certificate leaf[subject.OU] = "' + TEAM_ID + '" and '
                   'certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    # The exact Developer ID requirement is in addition to the complete
    # signature/resource-seal validation, which also covers nested code.
    base = ["/usr/bin/codesign", "--verify", "--strict"]
    for options in (["--deep"], ["-R", requirement]):
        result = subprocess.run(base + options + [str(BUNDLE)], capture_output=True, timeout=90)
        need(result.returncode == 0, "omlx_developer_id_signature_unverified")
    current_info = info_path.lstat()
    need(_file_identity(info_path, file_info) == _file_identity(info_path, current_info),
         "omlx_bundle_info_changed")


def _observed_identity(program_mode):
    with _open_binary(PROGRAM) as program, _open_binary(GUI) as gui:
        program_identity = _read_identity(PROGRAM, program, program_mode, PROGRAM_SHA)
        gui_identity = _read_identity(GUI, gui, 0o755, GUI_SHA)
        _verify_bundle()
        need(program_identity == _read_identity(PROGRAM, program, program_mode, PROGRAM_SHA)
             and gui_identity == _read_identity(GUI, gui, 0o755, GUI_SHA),
             "omlx_executable_changed_while_verifying_signature")
    return {
        "bundle_path": str(BUNDLE), "bundle_id": BUNDLE_ID, "version": VERSION,
        "build": BUILD, "team_id": TEAM_ID, "program": program_identity, "gui": gui_identity,
    }


def plan(configs, *, program_mode=0o775):
    """Return the deterministic candidate config bytes and public file identity."""
    need(type(program_mode) is int and program_mode in (0o775, 0o755), "omlx_unsupported_program_mode")
    paths = {path.name: path for path in configs}
    need(len(paths) == len(configs) and set(paths) == set(COMMITTED), "omlx_config_scope_changed")
    raw = {name: configs[path] for name, path in paths.items()}
    need(all(B.sha(data) == COMMITTED[name] for name, data in raw.items()), "omlx_postdeploy_binding_pin_changed")
    owner, registry, install, consumers = (
        json.loads(raw[name], object_pairs_hook=B.pairs) for name in
        ("owner.json", "registry.json", "installation.json", "consumer-admission.json"))
    need(install["state"] == "ready" and install["identity"]["environment"] == owner["environment"]
         == registry["environment"] == "prod" and install["identity"]["owner_uid"] == OWNER_UID
         and install["owner_config_sha256"] == B.sha(raw["owner.json"])
         and install["identity"]["registry_digest"] == B.sha(B.encoded(registry))
         and install["identity"]["consumer_config_digest"] == B.sha(B.encoded(consumers))
         and registry["registry_revision"] == 15, "omlx_installation_binding_drift")
    owner_service, registry_service = [value["services"]["omlx"] for value in (owner, registry)]
    old_keys = B.resource_keys(owner_service)
    for service in (owner_service, registry_service):
        native = service["mac"]
        need(service["platform"] == "macos_runtime" and service["revision"] == 2 and service["resource_scope"] == "host-shared"
             and service["actions"] == ["start", "status"] and service["dependencies"] == []
             and service["shared"] is True and service["shared_control_authorized"] is True
             and native == owner_service["mac"]
             and native["kind"] == "gui-app" and native["uid"] == OWNER_UID and native["port"] == 8000
             and native["maintenance_managed"] is True and native["bundle_path"] == str(BUNDLE)
             and native["bundle_id"] == BUNDLE_ID and native["program"] == str(PROGRAM)
             and native["owner_executable"] == str(GUI) and native["program_sha256"] == OLD_PROGRAM_SHA
             and native["owner_sha256"] == OLD_GUI_SHA
             and B.resource_keys(service) == old_keys == ["mac:" + str(OWNER_UID) + ":" + str(GUI)],
             "omlx_prior_native_binding_changed")
    identity = _observed_identity(program_mode)
    for service in (owner_service, registry_service):
        service["revision"] += 1
        service["mac"]["program_sha256"] = PROGRAM_SHA
        service["mac"]["owner_sha256"] = GUI_SHA
        need(B.resource_keys(service) == old_keys, "omlx_resource_scope_changed")
    registry["registry_revision"] += 1
    registry["evidence"]["omlx"] = {
        "verified": True, "checked_at": REVIEWED_AT,
        "note": "Native identity reviewed for signed macOS release oMLX 0.7.3 (build 3574), Developer ID team PSK5Q5T46L. "
                "Bundle and both executables pinned for App deployment owner build #101. " + RELEASE_URL,
    }
    owner_raw = B.encoded(owner) + b"\n"
    install["owner_config_sha256"] = B.sha(owner_raw)
    install["identity"]["registry_digest"] = B.sha(B.encoded(registry))
    after = dict(configs)
    after[paths["owner.json"]] = owner_raw
    after[paths["registry.json"]] = B.encoded(registry) + b"\n"
    after[paths["installation.json"]] = B.encoded(install) + b"\n"
    return after, identity


def apply_mode(identity):
    """Use the reviewed inode to remove only the Python executable's group-write bit."""
    need(_observed_identity(0o775) == identity, "omlx_reviewed_bundle_identity_changed")
    with _open_binary(PROGRAM) as source:
        observed = _read_identity(PROGRAM, source, 0o775, PROGRAM_SHA)
        need(observed == identity["program"], "omlx_reviewed_program_inode_changed")
        os.fchmod(source.fileno(), 0o755)
        os.fsync(source.fileno())
        after_program = _read_identity(PROGRAM, source, 0o755, PROGRAM_SHA)
    after = dict(identity, program=dict(identity["program"], mode=0o755))
    need(after_program == after["program"] and _observed_identity(0o755) == after,
         "omlx_hardened_bundle_identity_changed")
    return after
