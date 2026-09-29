#!/usr/bin/env python3
"""Refresh the two release scanner databases through one managed entrypoint."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys

HARBOR_IMAGE = "goharbor/trivy-adapter-photon:v2.15.2"
HARBOR_CACHE = "/home/scanner/.cache/trivy"


def run(argv):
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"managed scanner command failed: {argv[0]}")
    return result.stdout


def refresh(cache, prefix=()):
    command = [*prefix, "trivy", "--cache-dir", str(cache), "image",
               "--download-db-only", "--no-progress"]
    run(command)
    raw = run([*prefix, "trivy", "--cache-dir", str(cache), "--version", "--format", "json"])
    value = json.loads(raw)
    db = value.get("VulnerabilityDB") or {}
    if not value.get("Version") or not db.get("UpdatedAt") or not db.get("NextUpdate"):
        raise RuntimeError("scanner database provenance unavailable")
    return {"scanner_version": value["Version"], "updated_at": db["UpdatedAt"],
            "next_update": db["NextUpdate"]}


def harbor_container():
    raw = run(["docker", "ps", "--filter", "label=com.docker.compose.service=trivy-adapter",
               "--filter", "status=running", "--format", "{{json .}}"])
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(rows) != 1:
        raise RuntimeError("exactly one running Harbor Trivy adapter is required")
    container = rows[0].get("ID", "")
    if not re.fullmatch(r"[0-9a-f]{12,64}", container) or rows[0].get("Image") != HARBOR_IMAGE:
        raise RuntimeError("Harbor Trivy adapter identity changed")
    return container


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"schema_version": 1, "manager": "trusted-release-coordinator",
              "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        report["coordinator"] = refresh(args.local_cache)
        container = harbor_container()
        report["harbor"] = refresh(HARBOR_CACHE,
            ("docker", "exec", "--user", "scanner", container))
        report["harbor"]["container"] = container
        report["harbor"]["image"] = HARBOR_IMAGE
        report["status"] = "PASS"
        result = 0
    except Exception as exc:
        report["status"] = "BLOCKED"
        report["reason"] = str(exc)
        print(f"[scanner-db-manager] BLOCKED: {exc}", file=sys.stderr)
        result = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    return result


if __name__ == "__main__":
    sys.exit(main())
