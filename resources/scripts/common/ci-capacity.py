#!/usr/bin/env python3
"""App-only early capacity gate with one builder-scoped, age-bounded reclaim."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

GIB = 1024 ** 3
BUILDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PINNED_IMAGE = "moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8"


def run(argv, *, check=True):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} failed with exit {result.returncode}: {result.stderr.strip()[:300]}")
    return result


def observe(capacity_script, output):
    result = run([sys.executable, str(capacity_script), "--mode", "preflight",
                  "--warn-free-gib", "21", "--block-free-gib", "20",
                  "--output", str(output)], check=False)
    if result.returncode not in (0, 2) or not output.exists():
        raise RuntimeError("K3D/Docker capacity observation failed")
    report = json.loads(output.read_text(encoding="utf-8"))
    if not report.get("nodes") or not report.get("docker"):
        raise RuntimeError("K3D/Docker capacity observation incomplete")
    return report


def inventory(builder, output):
    result = run(["docker", "buildx", "du", "--builder", builder,
                  "--format", "{{json .}}"], check=False)
    output.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("dedicated builder cache inventory unavailable")
    return sum(1 for line in result.stdout.splitlines() if line.strip())


def verify_ownership(builder, policy):
    buildx_config = os.environ.get("BUILDX_CONFIG")
    if not buildx_config:
        raise RuntimeError("BUILDX_CONFIG unavailable; refusing prune")
    marker = Path(buildx_config) / f".{builder}.policy-sha256"
    expected = hashlib.sha256(policy.read_bytes()).hexdigest()
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != expected:
        raise RuntimeError("dedicated builder policy marker missing or drifted; refusing prune")
    image = run(["docker", "container", "inspect", "--format", "{{.Config.Image}}",
                 f"buildx_buildkit_{builder}0"]).stdout.strip()
    if image != PINNED_IMAGE:
        raise RuntimeError("dedicated builder image differs from pinned digest; refusing prune")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", required=True)
    parser.add_argument("--capacity-script", type=Path, required=True)
    parser.add_argument("--policy-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not BUILDER_RE.fullmatch(args.builder):
        parser.error("invalid dedicated builder name")
    if args.builder in ("default", "desktop-linux"):
        parser.error("shared Docker builder cannot be reclaimed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prefix = args.output.with_suffix("")
    report = {"schema_version": 1, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "builder": args.builder,
              "threshold_gib": {"k3d_gate": 12, "observed_lower_bound": 2, "provisional_margin": 6,
                                 "early_gate": 20},
              "reclaim": {"attempted": False, "scope": "dedicated-builder-only",
                          "age_filter": "until=24h", "reserved_space_gib": 2}}
    try:
        before_path = Path(f"{prefix}-before.json")
        before = observe(args.capacity_script, before_path)
        report["before"] = before
        if before["status"] == "BLOCKED":
            inspect = run(["docker", "buildx", "inspect", args.builder], check=False)
            if inspect.returncode == 0:
                if not re.search(r"(?m)^Driver:\s+docker-container\s*$", inspect.stdout):
                    raise RuntimeError("named builder is not isolated docker-container driver")
                verify_ownership(args.builder, args.policy_config)
                report["reclaim"]["inventory_before_count"] = inventory(args.builder, Path(f"{prefix}-cache-before.jsonl"))
                command = ["docker", "buildx", "prune", "--builder", args.builder,
                           "--filter", "until=24h", "--min-free-space", str(20 * GIB),
                           "--reserved-space", str(2 * GIB), "--force"]
                report["reclaim"]["attempted"] = True
                report["reclaim"]["command"] = command
                result = run(command, check=False)
                report["reclaim"]["exit_code"] = result.returncode
                Path(f"{prefix}-prune.log").write_text(result.stdout + result.stderr, encoding="utf-8")
                if result.returncode:
                    raise RuntimeError("bounded dedicated-builder reclaim failed")
                report["reclaim"]["inventory_after_count"] = inventory(args.builder, Path(f"{prefix}-cache-after.jsonl"))
            else:
                report["reclaim"]["skipped"] = "dedicated builder not yet provisioned; no shared cache touched"
            after = observe(args.capacity_script, Path(f"{prefix}-after.json"))
            report["after"] = after
            if after["status"] == "BLOCKED":
                raise RuntimeError("insufficient capacity after one bounded reclaim")
        report["status"] = "PASS"
        print(f"[ci-capacity] PASS: K3D free >= 20 GiB and no disk pressure; builder={args.builder}")
        return 0
    except Exception as exc:
        report["status"] = "BLOCKED"
        report["reason"] = str(exc)
        print(f"[ci-capacity] BLOCKED: {exc}", file=sys.stderr)
        return 2
    finally:
        args.output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
