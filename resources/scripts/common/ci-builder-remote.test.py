import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import ci_builder_remote as builder


POLICY = Path(__file__).with_name("ci-builder.toml")
MOUNTS = [
    {"Destination": builder.CACHE_PATH, "Type": "volume", "Name": builder.CACHE_VOLUME, "RW": True},
    {"Destination": builder.CONFIG_PATH, "Type": "bind", "RW": False},
]
OWNER = "\t".join((
    "a" * 64,
    f"/{builder.BUILDER}",
    "true",
    builder.IMAGE,
    builder.BUILDER,
    "buildkit",
    '["--config","/etc/buildkit/buildkitd.toml","--allow-insecure-entitlement=network.host"]',
    json.dumps(MOUNTS),
))
POLICY_HASH = "865cefd45ed9bdb6c3f04d3c78a45996f345e7cba4b9b5c073b7c80d600938c4"
INSPECT = f"""Name:          {builder.BUILDER}
Driver:        remote

Nodes:
Name:             {builder.BUILDER}0
Endpoint:         {builder.ENDPOINT}
Status:           running
BuildKit version: v0.32.2
Platforms:        linux/arm64
"""
MISSING = f'ERROR: no builder "{builder.BUILDER}" found'


def result(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RemoteBuilderTest(unittest.TestCase):
    def test_verified_owner_uses_container_id_and_reviews_actual_policy(self):
        with mock.patch.object(builder, "docker", side_effect=[
            result(INSPECT), result(OWNER), result(f"{POLICY_HASH}  {builder.CONFIG_PATH}\n")
        ]) as docker:
            self.assertTrue(builder.verify_registration(POLICY))
        self.assertEqual(
            mock.call("exec", "a" * 64, "sha256sum", builder.CONFIG_PATH), docker.call_args_list[-1]
        )

    def test_wrong_owner_stopped_owner_or_changed_policy_are_rejected(self):
        for owner in (
            OWNER.replace(builder.IMAGE, "moby/buildkit:latest"),
            OWNER.replace("\ttrue\t", "\tfalse\t"),
            OWNER.replace("\tbuildkit\t", "\tother\t"),
            OWNER.replace("/etc/buildkit/buildkitd.toml", "/tmp/buildkitd.toml"),
        ):
            with self.subTest(owner=owner), mock.patch.object(builder, "docker", return_value=result(owner)):
                with self.assertRaisesRegex(RuntimeError, "pinned image and config"):
                    builder.verify_owner(POLICY)
        with mock.patch.object(builder, "docker", side_effect=[result(OWNER), result("b" * 64)]):
            with self.assertRaisesRegex(RuntimeError, "policy digest differs"):
                builder.verify_owner(POLICY)

    def test_shared_cache_or_writable_policy_is_rejected_before_exec(self):
        legacy_cache = {**MOUNTS[0], "Name": "buildx_buildkit_shiba-app-ci0_state"}
        read_only_cache = {**MOUNTS[0], "RW": False}
        writable_policy = {**MOUNTS[1], "RW": True}
        for mounts in ([legacy_cache, MOUNTS[1]], [read_only_cache, MOUNTS[1]], [MOUNTS[0], writable_policy]):
            owner = OWNER.replace(json.dumps(MOUNTS), json.dumps(mounts))
            with self.subTest(mounts=mounts), mock.patch.object(builder, "docker", return_value=result(owner)) as docker:
                with self.assertRaisesRegex(RuntimeError, "dedicated cache read-write and policy read-only"):
                    builder.verify_owner(POLICY)
                docker.assert_called_once()

    def test_wrong_or_ambiguous_buildx_registration_is_rejected(self):
        for inspection in (
            INSPECT.replace("remote", "docker-container"),
            INSPECT.replace(builder.ENDPOINT, "tcp://another-buildkit:1234"),
            INSPECT + "Name: another\nEndpoint: tcp://example:1234\nStatus: running\n",
            INSPECT.replace("running", "stopped"),
        ):
            with self.subTest(inspection=inspection), mock.patch.object(
                builder, "docker", return_value=result(inspection)
            ):
                with self.assertRaisesRegex(RuntimeError, "one running remote Buildx node"):
                    builder.verify_registration(POLICY, allow_unregistered=True)

    def test_missing_alias_can_skip_reclaim_but_other_failures_are_fatal(self):
        with mock.patch.object(builder, "docker", return_value=result(stderr=MISSING, returncode=1)):
            self.assertFalse(builder.verify_registration(POLICY, allow_unregistered=True))
            with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                builder.verify_registration(POLICY)
        with mock.patch.object(builder, "docker", return_value=result(stderr="connection refused", returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Cannot inspect"):
                builder.verify_registration(POLICY, allow_unregistered=True)

    def test_register_creates_only_buildx_metadata_after_owner_verification(self):
        with mock.patch.object(sys, "argv", [__file__, str(POLICY), "--register"]), mock.patch.object(
            builder, "docker", side_effect=[
                result(OWNER),
                result(f"{POLICY_HASH}  {builder.CONFIG_PATH}\n"),
                result(stderr=MISSING, returncode=1),
                result(),
                result(INSPECT),
            ],
        ) as docker:
            self.assertEqual(0, builder.main())
        self.assertEqual(
            mock.call("buildx", "create", "--name", builder.BUILDER, "--driver", "remote", builder.ENDPOINT),
            docker.call_args_list[3],
        )
        self.assertEqual(mock.call("buildx", "inspect", builder.BUILDER, check=False), docker.call_args_list[4])


if __name__ == "__main__":
    unittest.main()
