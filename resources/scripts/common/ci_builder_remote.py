#!/usr/bin/env python3
"""Verify and register the App's owner-managed BuildKit daemon."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


BUILDER = "shiba-ci-components-buildkit"
ENDPOINT = f"docker-container://{BUILDER}"
IMAGE = "moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8"
CONFIG_PATH = "/etc/buildkit/buildkitd.toml"
CACHE_PATH = "/var/lib/buildkit"
CACHE_VOLUME = f"{BUILDER}-cache"
CONTAINER_FORMAT = (
    '{{.Id}}\t{{.Name}}\t{{.State.Running}}\t{{.Config.Image}}\t'
    '{{index .Config.Labels "com.docker.compose.project"}}\t'
    '{{index .Config.Labels "com.docker.compose.service"}}\t{{json .Args}}\t{{json .Mounts}}'
)


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=30
    )
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} {args[1]} failed: {result.stderr.strip()[:300]}")
    return result


def inspect_registration(*, allow_unregistered=False):
    result = docker("buildx", "inspect", BUILDER, check=False)
    if result.returncode:
        if (allow_unregistered and result.returncode == 1
                and result.stderr.strip() == f'ERROR: no builder "{BUILDER}" found'):
            return False
        raise RuntimeError(f"Cannot inspect App Buildx registration: {result.stderr.strip()[:300]}")

    header, separator, nodes = result.stdout.partition("\nNodes:\n")
    if (not separator
            or re.findall(r"^Name:[ \t]+(\S+)[ \t]*$", header, re.M) != [BUILDER]
            or re.findall(r"^Driver:[ \t]+(\S+)[ \t]*$", header, re.M) != ["remote"]
            or re.findall(r"^Name:[ \t]+(\S+)[ \t]*$", nodes, re.M) != [f"{BUILDER}0"]
            or re.findall(r"^Endpoint:[ \t]+(\S+)[ \t]*$", nodes, re.M) != [ENDPOINT]
            or re.findall(r"^Status:[ \t]+(\S+)[ \t]*$", nodes, re.M) != ["running"]):
        raise RuntimeError(f"Expected one running remote Buildx node at {ENDPOINT}")
    return True


def verify_owner(policy):
    inspected = docker("container", "inspect", "--format", CONTAINER_FORMAT, BUILDER)
    container_id, name, running, image, project, service, args, mounts_json = inspected.stdout.strip().split("\t", 7)
    if (name != f"/{BUILDER}" or running != "true" or image != IMAGE
            or project != BUILDER or service != "buildkit"
            or not re.fullmatch(r"[a-f0-9]{64}", container_id)
            or json.loads(args) != ["--config", CONFIG_PATH, "--allow-insecure-entitlement=network.host"]):
        raise RuntimeError(f"Expected running owner-managed {BUILDER} with the pinned image and config")

    mounts = {mount["Destination"]: mount for mount in json.loads(mounts_json)}
    cache = mounts.get(CACHE_PATH, {})
    config = mounts.get(CONFIG_PATH, {})
    if (cache.get("Type") != "volume" or cache.get("Name") != CACHE_VOLUME or cache.get("RW") is not True
            or config.get("Type") != "bind" or config.get("RW") is not False):
        raise RuntimeError("Owner-managed BuildKit must mount its dedicated cache read-write and policy read-only")

    expected = hashlib.sha256(policy.read_bytes()).hexdigest()
    actual = docker("exec", container_id, "sha256sum", CONFIG_PATH).stdout.partition(" ")[0]
    if actual != expected:
        raise RuntimeError("Owner-managed BuildKit policy digest differs from the reviewed policy")
    return expected


def verify_registration(policy, *, allow_unregistered=False):
    if not inspect_registration(allow_unregistered=allow_unregistered):
        return False
    verify_owner(policy)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    try:
        if args.register:
            policy_hash = verify_owner(args.policy)
            if not inspect_registration(allow_unregistered=True):
                docker("buildx", "create", "--name", BUILDER, "--driver", "remote", ENDPOINT)
                inspect_registration()
            print(f"[ci-builder] ready: {BUILDER}, config-sha256={policy_hash}, image={IMAGE}")
        else:
            verify_registration(args.policy)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[ci-builder] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
