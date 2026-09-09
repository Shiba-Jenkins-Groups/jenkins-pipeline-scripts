#!/usr/bin/env python3
"""Fail-closed capacity preflight and read-only K3D capacity report."""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

GIB = 1024 ** 3


def load_json(path, command):
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(subprocess.check_output(command, text=True))


def size_bytes(value):
    match = re.fullmatch(r"([0-9.]+)([kMGT]?B)", value or "0B")
    if not match:
        return None
    factors = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}
    return int(float(match.group(1)) * factors[match.group(2)])


def docker_summary(raw):
    result = {}
    for category in ("Images", "Containers", "Volumes", "BuildCache"):
        items = raw.get(category, [])
        result[category.lower()] = {
            "count": len(items),
            "size_bytes": sum(value for value in (size_bytes(item.get("Size")) for item in items) if value is not None),
            "reclaimable_bytes": sum(value for value in (size_bytes(item.get("Reclaimable")) for item in items) if value is not None),
        }
    return result


def evaluate(nodes, summaries, warn_gib, block_gib, warn_percent, block_percent):
    results = []
    overall = "OK"
    for node in nodes.get("items", []):
        name = node["metadata"]["name"]
        summary = summaries[name]
        fs = summary["node"]["fs"]
        capacity = int(fs["capacityBytes"])
        available = int(fs["availableBytes"])
        used_percent = round((1 - available / capacity) * 100, 2) if capacity else 100.0
        conditions = {item["type"]: item["status"] for item in node.get("status", {}).get("conditions", [])}
        taints = node.get("spec", {}).get("taints") or []
        disk_taint = any(item.get("key") == "node.kubernetes.io/disk-pressure" for item in taints)
        reasons = []
        status = "OK"
        if conditions.get("DiskPressure") == "True" or disk_taint or available < block_gib * GIB or used_percent > block_percent:
            status = "BLOCKED"
            reasons.append("disk pressure or critical capacity threshold reached")
        elif available < warn_gib * GIB or used_percent > warn_percent:
            status = "WARNING"
            reasons.append("capacity warning threshold reached")
        if status == "BLOCKED":
            overall = "BLOCKED"
        elif status == "WARNING" and overall == "OK":
            overall = "WARNING"
        results.append({
            "node": name, "status": status, "available_bytes": available,
            "capacity_bytes": capacity, "used_percent": used_percent,
            "disk_pressure": conditions.get("DiskPressure"), "disk_pressure_taint": disk_taint,
            "reasons": reasons,
        })
    if not results:
        return {"status": "BLOCKED", "nodes": [], "reasons": ["no K3D nodes found"]}
    return {"status": overall, "nodes": results, "reasons": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["preflight", "monitor"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nodes-file", type=Path)
    parser.add_argument("--summaries-file", type=Path)
    parser.add_argument("--docker-file", type=Path)
    parser.add_argument("--warn-free-gib", type=int, default=20)
    parser.add_argument("--block-free-gib", type=int, default=12)
    parser.add_argument("--warn-used-percent", type=float, default=80)
    parser.add_argument("--block-used-percent", type=float, default=90)
    args = parser.parse_args()
    if args.block_free_gib >= args.warn_free_gib or args.warn_used_percent >= args.block_used_percent:
        parser.error("warning thresholds must precede blocking thresholds")
    try:
        nodes = load_json(args.nodes_file, ["kubectl", "get", "nodes", "-o", "json"])
        if args.summaries_file:
            summaries = json.loads(args.summaries_file.read_text(encoding="utf-8"))
        else:
            summaries = {
                item["metadata"]["name"]: load_json(None, ["kubectl", "get", "--raw", f"/api/v1/nodes/{item['metadata']['name']}/proxy/stats/summary"])
                for item in nodes.get("items", [])
            }
        docker_raw = load_json(args.docker_file, ["docker", "system", "df", "--format", "{{json .}}"])
        report = evaluate(nodes, summaries, args.warn_free_gib, args.block_free_gib,
                          args.warn_used_percent, args.block_used_percent)
        report["docker"] = docker_summary(docker_raw)
    except Exception as exc:
        report = {"status": "BLOCKED", "nodes": [], "docker": {},
                  "reasons": [f"capacity observation failed: {type(exc).__name__}"]}
    report.update({
        "schema_version": 1, "mode": args.mode,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "thresholds": {"warn_free_gib": args.warn_free_gib, "block_free_gib": args.block_free_gib,
                       "warn_used_percent": args.warn_used_percent, "block_used_percent": args.block_used_percent},
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    for item in report["nodes"]:
        print(f"[k3d-capacity] {item['node']}: {item['status']} used={item['used_percent']}% available={item['available_bytes'] // GIB}GiB diskPressure={item['disk_pressure']} taint={item['disk_pressure_taint']}")
    if report["status"] == "BLOCKED":
        for reason in report["reasons"]:
            print(f"[k3d-capacity] BLOCKED {reason}", file=sys.stderr)
        return 2
    if report["status"] == "WARNING" and args.mode == "monitor":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
