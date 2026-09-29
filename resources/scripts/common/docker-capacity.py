#!/usr/bin/env python3
"""Fail-closed Docker Engine capacity report for Compose-only delivery."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys

GIB = 1024 ** 3


def run(argv):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)


def filesystem_from_df(raw):
    rows = [line.split() for line in raw.splitlines() if line.strip()]
    if len(rows) != 2 or len(rows[1]) < 6 or not all(value.isdigit() for value in rows[1][1:4]):
        raise RuntimeError("unexpected Docker filesystem capacity output")
    blocks, used, available = (int(value) * 1024 for value in rows[1][1:4])
    used_percent = round(used / blocks * 100, 2) if blocks else 100.0
    return {"capacity_bytes": blocks, "used_bytes": used,
            "available_bytes": available, "used_percent": used_percent}


def evaluate(storage, warn_gib, block_gib, warn_percent, block_percent):
    status, reasons = "OK", []
    if storage["available_bytes"] < block_gib * GIB or storage["used_percent"] > block_percent:
        status = "BLOCKED"
        reasons.append("Docker Engine storage reached a critical capacity threshold")
    elif storage["available_bytes"] < warn_gib * GIB or storage["used_percent"] > warn_percent:
        status = "WARNING"
        reasons.append("Docker Engine storage reached a warning threshold")
    return {"status": status, "storage": {**storage, "status": status}, "reasons": reasons}


def observe_storage():
    container = os.environ.get("HOSTNAME", "")
    if not re.fullmatch(r"[0-9a-f]{12,64}", container):
        raise RuntimeError("trusted Jenkins agent container identity unavailable")
    inspected = run(["docker", "container", "inspect", "--format", "{{.Image}}", container])
    if inspected.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", inspected.stdout.strip()):
        raise RuntimeError("cannot resolve immutable Jenkins agent image")
    # The helper uses the already-running agent's immutable image and mounts the
    # Engine root read-only. It neither starts K3D nor selects cleanup targets.
    command = ["docker", "run", "--rm", "--network", "none", "--read-only",
               "--entrypoint", "/bin/df", "--mount",
               "type=bind,src=/var/lib/docker,dst=/docker-root,readonly",
               inspected.stdout.strip(), "-Pk", "/docker-root"]
    result = run(command)
    if result.returncode:
        raise RuntimeError("Docker Engine filesystem capacity probe failed")
    return filesystem_from_df(result.stdout), inspected.stdout.strip()


def docker_inventory():
    result = run(["docker", "system", "df", "--format", "{{json .}}"])
    if result.returncode:
        raise RuntimeError("Docker inventory unavailable")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["preflight", "monitor"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage-file", type=Path)
    parser.add_argument("--docker-file", type=Path)
    parser.add_argument("--warn-free-gib", type=int, default=20)
    parser.add_argument("--block-free-gib", type=int, default=12)
    parser.add_argument("--warn-used-percent", type=float, default=80)
    parser.add_argument("--block-used-percent", type=float, default=90)
    args = parser.parse_args()
    if args.block_free_gib >= args.warn_free_gib or args.warn_used_percent >= args.block_used_percent:
        parser.error("warning thresholds must precede blocking thresholds")
    image = None
    try:
        if args.storage_file:
            storage = json.loads(args.storage_file.read_text(encoding="utf-8"))
        else:
            storage, image = observe_storage()
        inventory = (json.loads(args.docker_file.read_text(encoding="utf-8"))
                     if args.docker_file else docker_inventory())
        report = evaluate(storage, args.warn_free_gib, args.block_free_gib,
                          args.warn_used_percent, args.block_used_percent)
        report["docker"] = inventory
        report["probe_image"] = image
    except Exception as exc:
        report = {"status": "BLOCKED", "storage": {}, "docker": {},
                  "reasons": [f"capacity observation failed: {type(exc).__name__}"]}
    report.update({"schema_version": 1, "profile": "compose", "mode": args.mode,
                   "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "thresholds": {"warn_free_gib": args.warn_free_gib,
                                  "block_free_gib": args.block_free_gib,
                                  "warn_used_percent": args.warn_used_percent,
                                  "block_used_percent": args.block_used_percent}})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    if report["status"] == "BLOCKED":
        print("[docker-capacity] BLOCKED " + "; ".join(report["reasons"]), file=sys.stderr)
        return 2
    if report["status"] == "WARNING" and args.mode == "monitor":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
