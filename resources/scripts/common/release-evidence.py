#!/usr/bin/env python3
"""Trusted coordinator adapters for completed Jenkins builds and native scans."""
import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


gate = module("release_gate", "release-gate.py")
require = gate.require
RELEASE_FOLDER = "shiba-release-automation"
STAGES = ["Checkout", "Load Scripts", "Detect", "Secret Scan", "Build", "Test",
          "Fast Contract Test", "Dependency Scan", "Package / Publish / Tag", "Docker Build",
          "Image Scan", "Harbor Push", "Harbor Vulnerability Report", "Smoke Test",
          "Deployment Verification — k3s", "Declarative: Post Actions"]
FINALIZE = "Release Finalization — Artifact / Git Tag"
PACKAGE_GRAPHS = [
    {"name": "linux-arm64-nodynamic-tests", "tags": "nodynamic", "test": True, "target": "./..."},
    {"name": "linux-arm64-devseed-nodynamic-tests", "tags": "devseed,nodynamic", "test": True, "target": "./..."},
    {"name": "linux-arm64-nodynamic-server", "tags": "nodynamic", "test": False, "target": "./cmd/app/server"},
]


def policy(approvers):
    return {"schema_version": 1, "product": gate.PRODUCT,
            "jobs": {"promotion": gate.PRODUCT + "/develop", "deployment": gate.PRODUCT + "/prod"},
            "required_stages": {"promotion": STAGES, "deployment": STAGES + [FINALIZE]},
            "required_scanners": ["trivy", "govulncheck", "harbor"],
            "not_applicable_advisories": [{"id": "GO-2026-5932",
                "affected_package_prefix": "golang.org/x/crypto/openpgp",
                "required_package_graphs": [item["name"] for item in PACKAGE_GRAPHS],
                "require_no_govuln_affected_package_finding": True}],
            "max_evidence_age_seconds": 3600, "max_exception_seconds": 900,
            "approvers": approvers, "revoked_approval_ids": []}


def key_values(raw):
    values = {}
    for line in raw.splitlines():
        require("=" in line, "invalid artifact metadata line")
        key, value = line.split("=", 1)
        require(key not in values, "duplicate artifact metadata key")
        values[key] = value
    return values


def completed_build(build, workflow, image_text, branch, number, release_text=None, candidate=None):
    require(branch in {"develop", "prod"}, "unsupported branch")
    require(type(number) is int and number > 0 and build.get("number") == number, "wrong build")
    results = {'SUCCESS', 'UNSTABLE'} if candidate is not None else {'SUCCESS'}
    require(build.get("building") is False and build.get("result") in results, "build not complete or eligible")
    require(workflow.get("status") in results and str(workflow.get("id")) == str(number),
            "workflow incomplete or wrong build")
    shas = {revision["SHA1"] for action in build.get("actions", [])
            if (revision := action.get("lastBuiltRevision"))
            for item in revision.get("branch", []) if item.get("name") in {branch, "origin/" + branch}}
    require(len(shas) == 1, "ambiguous product checkout revision")
    commit = shas.pop()
    require(gate.SHA.fullmatch(commit), "invalid product checkout SHA")
    require(not any(action.get("parameters") and any(p.get("name") == "CHANGE_ID" and p.get("value")
                for p in action["parameters"]) for action in build.get("actions", [])), "PR build rejected")
    stages = []
    for stage in workflow.get("stages", []):
        if candidate is not None and stage['name'] == 'Develop Image Verification':
            require(stage['status'] == 'NOT_EXECUTED', 'lean image lane is incompatible with controlled candidates')
            continue  # Full Image Scan and Smoke Test remain mandatory below.
        if (branch == "develop" or candidate is not None) and stage["name"] == FINALIZE and stage["status"] == "NOT_EXECUTED":
            continue  # The only structurally inapplicable stage in this policy.
        if candidate is not None and stage['name'] in {'Prepare（準備）', 'Continuous Integration（持續整合）', 'Continuous Delivery（持續交付）'}:
            require(stage['status'] in results, 'failed parent stage')
            continue  # Aggregate status; every mandatory leaf is checked below.
        require(stage['status'] == 'SUCCESS' or candidate is not None and stage['name'] in gate.WAIVABLE_STAGES and stage['status'] == 'UNSTABLE',
                'non-success stage: ' + stage['name'])
        stages.append({"name": stage["name"], "result": stage["status"]})
    expected = STAGES + ([FINALIZE] if branch == "prod" and candidate is None else [])
    require(candidate is None or FINALIZE not in {s['name'] for s in stages}, 'candidate finalized before release approval')
    require(set(expected).issubset({s["name"] for s in stages}), "required CI stage missing")
    require(len(stages) == len({s["name"] for s in stages}), "duplicate CI stage")
    if candidate is not None:
        groups = {'Prepare（準備）': STAGES[:3], 'Continuous Integration（持續整合）': STAGES[3:9],
                  'Continuous Delivery（持續交付）': STAGES[9:-1]}
        for parent in workflow['stages']:
            if parent['name'] in groups and parent['status'] == 'UNSTABLE':
                require(any(s['name'] in groups[parent['name']] and s['result'] == 'UNSTABLE' for s in stages), 'unexplained parent stage result')
    image = key_values(image_text)
    require(image.get("APP_NAME") == gate.PRODUCT and image.get("BRANCH") == branch,
            "artifact product or branch mismatch")
    require(image.get("BUILD_NUMBER") == str(number), "artifact build mismatch")
    version = image.get("APP_VERSION", "")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "invalid release version")
    ref = f"localhost:9290/{gate.PRODUCT}/{branch}/{version}:{number}"
    require(image.get("IMAGE_REF") == ref and gate.DIGEST.fullmatch(image.get("IMAGE_DIGEST", "")),
            "unexpected artifact reference")
    if branch == "prod" and candidate is None:
        release = key_values(release_text or "")
        require(release.get("GIT_COMMIT") == commit and release.get("RELEASE_TAG") == "v" + version
                and release.get("IMAGE_REF") == ref and release.get("IMAGE_DIGEST") == image["IMAGE_DIGEST"],
                "release finalization receipt mismatch")
        require(release.get("NEXUS_ARTIFACT_URL", "").startswith("http"), "missing release artifact")
    finished = dt.datetime.fromtimestamp((build["timestamp"] + build["duration"]) / 1000, dt.timezone.utc)
    result = {"schema_version": 1, "product": gate.PRODUCT,
            "gate": "promotion" if branch == "develop" else "deployment", "branch": branch,
            "event": "branch", "trusted": True, "job": gate.PRODUCT + "/" + branch,
            "build": number, "commit": commit, "image_ref": ref, "image_digest": image["IMAGE_DIGEST"],
            "version": version, "immutable_image": ref.rsplit(":", 1)[0] + "@" + image["IMAGE_DIGEST"],
            "building": False, "post_complete": True, "result": build['result'],
            "completed_at": finished.isoformat(), "stages": stages, "reports": []}
    if candidate is not None:
        require(candidate.get('schema_version') == 1 and candidate.get('mode') == 'controlled-candidate-v1', 'missing candidate contract')
        for key in ['product', 'commit', 'job', 'build', 'branch', 'version']:
            require(candidate[key] == result[key], 'candidate identity mismatch')
        result.update(mode=candidate['mode'], stage_checks=candidate['stage_checks'], artifact=candidate['artifact'], artifact_name=candidate['artifact_name'])
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Jenkins credential to another endpoint.


def jenkins_get(base, suffix):
    parsed = urllib.parse.urlsplit(base)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username,
            "invalid Jenkins API URL")
    auth = base64.b64encode((os.environ["JENKINS_API_USER"] + ":" + os.environ["JENKINS_API_TOKEN"]).encode()).decode()
    req = urllib.request.Request(base.rstrip("/") + suffix, headers={"Authorization": "Basic " + auth})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
        return response.read()


def inspect(base, branch, number, candidate_root=None):
    require(branch in {"develop", "prod"} and number > 0, "invalid build coordinate")
    prefix = f"/job/{gate.PRODUCT}/job/{branch}/{number}/"
    build = json.loads(jenkins_get(base, prefix + "api/json"))
    workflow = json.loads(jenkins_get(base, prefix + "wfapi/describe"))
    image = jenkins_get(base, prefix + "artifact/image-ref.txt").decode()
    release = jenkins_get(base, prefix + "artifact/.pipeline/release-manifest.env").decode() if branch == "prod" and candidate_root is None else None
    candidate = json.loads(jenkins_get(base, prefix + 'artifact/.pipeline/candidate.json')) if candidate_root is not None else None
    result = completed_build(build, workflow, image, branch, number, release, candidate)
    if candidate is not None:
        def fetch(record):
            path = Path(record['path'])
            require(not path.is_absolute() and '..' not in path.parts and re.fullmatch(r'[A-Za-z0-9_./-]+', str(path)), 'unsafe candidate report path')
            target = candidate_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            require(target.resolve().is_relative_to(candidate_root.resolve()), 'candidate report escapes root')
            raw = jenkins_get(base, prefix + 'artifact/' + str(path))
            require(hashlib.sha256(raw).hexdigest() == record['sha256'], 'candidate artifact checksum mismatch')
            with target.open('xb') as out: out.write(raw)
            return raw
        fetch(result['artifact'])
        for record in result['stage_checks']:
            value = json.loads(fetch(record))
            for report in value['reports']: fetch(report)
    return result


def prod_routing(xml):
    root = ET.fromstring(xml)
    properties = root.findall(".//jenkins.branch.NoTriggerBranchProperty")
    require(len(properties) == 1, "prod automatic SCM triggers are not suppressed")
    prop = properties[0]
    # branch-api 2.1268 uses INDEXING / EVENTS / NONE.  With a regex that
    # matches no branch, the handler suppresses both indexing and event causes
    # before consulting the strategy; NONE is therefore the explicit setting
    # for this installed version.  Keep ALL accepted for older serialized
    # configurations that exposed an all-causes enum.
    require(prop.findtext("strategy", "NONE") in {"NONE", "ALL"}
            and prop.findtext("triggeredBranchesRegex", "^$") in {"", "^$", "(?!)"},
            "prod trigger suppression is incomplete")


def runtime_ready(job, computers, label):
    require(job.get("fullName") == RELEASE_FOLDER + "/" + gate.PRODUCT + "-prod-deploy" and job.get("buildable") is True,
            "trusted deployment job is unavailable")
    nodes = [node for node in computers.get("computer", [])
             if any(item.get("name") == label for item in node.get("assignedLabels", []))]
    require(len(nodes) == 1 and nodes[0].get("offline") is False and nodes[0].get("temporarilyOffline") is False
            and nodes[0].get("numExecutors", 0) > 0, "unique deployment executor is not ready")


def stream_objects(raw):
    decoder, offset, values = json.JSONDecoder(), 0, []
    while offset < len(raw):
        if raw[offset].isspace():
            offset += 1
            continue
        value, offset = decoder.raw_decode(raw, offset)
        require(isinstance(value, dict), "invalid scanner stream record")
        values.append(value)
    require(values, "empty scanner stream")
    return values


def save(root, name, value):
    return save_raw(root, name, gate.canonical(value))


def save_raw(root, name, raw):
    target = root / name
    with target.open("xb") as stream:
        stream.write(raw)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}


def run(command, cwd, env):
    result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=900, check=False)
    require(result.returncode == 0, "scanner command failed: " + command[0])
    return result.stdout.decode()


def scan(evidence, source, root, harbor_url):
    # No repo-controlled TRIVY_* settings or ignore files may suppress evidence.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRIVY_", "GOVULN", "HARBOR_", "JENKINS_API_"))
           and k not in {"GOFLAGS", "GOWORK"}}
    env.update(GOVULNDB="https://vuln.go.dev", GOWORK="off", GOFLAGS="")
    actual = run(["git", "rev-parse", "HEAD"], source, env).strip()
    require(actual == evidence["commit"], "scanner checkout mismatch")
    require(not run(["git", "status", "--porcelain", "--untracked-files=all"], source, env).strip(),
            "scanner checkout must be clean")
    if evidence.get('mode') == 'controlled-candidate-v1':
        candidate_image(evidence, json.loads(run(['docker', 'image', 'inspect', evidence['immutable_image']], source, env)))
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "trivy-cache"
    raw_trivy = run(["trivy", "--cache-dir", str(cache), "--config", "/dev/null", "image",
                     "--image-src", "docker", "--scanners", "vuln", "--format", "json", "--exit-code", "0",
                     "--severity", ",".join(sorted(gate.SEVERITIES)), "--ignorefile", "/dev/null",
                     "--ignore-unfixed=false", "--ignore-status", "", "--ignore-policy", "",
                     evidence["immutable_image"]], root, env)
    trivy_ref = save_raw(root, "trivy-native.json", raw_trivy.encode())
    version = json.loads(run(["trivy", "--cache-dir", str(cache), "--version", "--format", "json"], root, env))
    trivy = json.loads(raw_trivy)
    raw_go = run(["govulncheck", "-json", "./..."], source, env)
    save_raw(root, "govulncheck-stream.jsonl", raw_go.encode())
    go_messages = stream_objects(raw_go)
    go_ref = save(root, "govulncheck-native.json", {"messages": go_messages})
    configs = [m["config"] for m in go_messages if "config" in m]
    require(len(configs) == 1, "govulncheck config missing or duplicated")
    go_config = configs[0]
    graphs = []
    for spec in PACKAGE_GRAPHS:
        graph_env = dict(env, GOOS="linux", GOARCH="arm64", CGO_ENABLED="0")
        command = ["go", "list", "-mod=readonly", "-deps"]
        if spec["test"]:
            command.append("-test")
        command.extend(["-tags=" + spec["tags"], spec["target"]])
        packages = sorted(set(line for line in run(command, source, graph_env).splitlines() if line))
        require(packages, "empty package graph")
        graphs.append({**spec, "goos": "linux", "goarch": "arm64", "cgo_enabled": "0", "packages": packages})
    evidence["package_graph"] = save(root, "package-graphs.json", {
        "schema_version": 1, "complete": True, "commit": evidence["commit"],
        "go_version": run(["go", "version"], source, env).strip(), "graphs": graphs})
    harbor = module("release_harbor", "harbor-vulnerability-report.py")
    api = harbor.HarborAPI(harbor_url, os.environ["HARBOR_USER"], os.environ["HARBOR_PASS"])
    item = harbor.scan_image(api, evidence["immutable_image"], timeout_seconds=600, poll_seconds=3)
    require(item.digest == evidence["image_digest"], "Harbor resolved different image")
    native_harbor = {"artifact": {"digest": item.digest}, "report": item.report}
    harbor_ref = save(root, "harbor-native.json", native_harbor)
    harbor_report = item.report.get(harbor.VULNERABILITY_MIME, item.report)
    # Harbor API exposes report generation time, not the vulnerability DB hash.
    # Record this limitation explicitly instead of inventing a DB revision.
    provenance = {
        "trivy": (version["Version"], version["VulnerabilityDB"]["UpdatedAt"]),
        "govulncheck": (go_config["scanner_version"], go_config["db_last_modified"]),
        "harbor": (harbor_report["scanner"]["version"], "not-exposed-by-harbor;report=" + harbor_report["generated_at"]),
    }
    for scanner, native in [("trivy", trivy), ("govulncheck", {"messages": go_messages}), ("harbor", native_harbor)]:
        gate.native_findings(scanner, native, evidence["image_digest"])
        native_ref = {"trivy": trivy_ref, "govulncheck": go_ref, "harbor": harbor_ref}[scanner]
        scanner_version, database_revision = provenance[scanner]
        require(scanner_version and database_revision, "missing scan provenance")
        envelope = {"schema_version": 1, "scanner": scanner, "scanner_version": scanner_version,
                    "database_revision": database_revision, "complete": True, "unfiltered": True,
                    "commit": evidence["commit"], "image_digest": evidence["image_digest"],
                    "severities": sorted(gate.SEVERITIES), "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "native_report": native_ref}
        evidence["reports"].append({"scanner": scanner, **save(root, scanner + ".json", envelope)})
    save(root, "evidence.json", evidence)
    return evidence


def candidate_image(evidence, images):
    require(len(images) == 1 and evidence['immutable_image'] in images[0].get('RepoDigests', []), 'candidate registry digest mismatch')
    labels = images[0]['Config'].get('Labels', {})
    require(labels.get('org.opencontainers.image.revision') == evidence['commit']
            and labels.get('app.artifact.sha256') == evidence['artifact']['sha256']
            and labels.get('app.version') == evidence['version'] and labels.get('app.branch') == evidence['branch']
            and labels.get('app.name') == gate.PRODUCT, 'candidate source/binary/image identity mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect")
    read.add_argument("--jenkins-url", required=True)
    read.add_argument("--branch", choices=["develop", "prod"], required=True)
    read.add_argument("--build", type=int, required=True)
    read.add_argument("--output", type=Path, required=True)
    read.add_argument('--candidate-root', type=Path)
    routing = commands.add_parser("check-routing")
    routing.add_argument("--jenkins-url", required=True)
    routing.add_argument("--deployment-label", required=True)
    collect = commands.add_parser("scan")
    collect.add_argument("--identity", type=Path, required=True)
    collect.add_argument("--source", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--harbor-url", required=True)
    args = parser.parse_args()
    try:
        if args.command == "check-routing":
            prod_routing(jenkins_get(args.jenkins_url, f"/job/{gate.PRODUCT}/job/prod/config.xml"))
            runtime_ready(json.loads(jenkins_get(args.jenkins_url, f"/job/{RELEASE_FOLDER}/job/{gate.PRODUCT}-prod-deploy/api/json")),
                          json.loads(jenkins_get(args.jenkins_url, "/computer/api/json?depth=1")), args.deployment_label)
        elif args.command == "inspect":
            args.output.write_bytes(gate.canonical(inspect(args.jenkins_url, args.branch, args.build, args.candidate_root)))
        else:
            scan(json.loads(args.identity.read_bytes()), args.source.resolve(), args.output.resolve(), args.harbor_url)
    except Exception as exc:
        print("BLOCKED: " + (str(exc) if isinstance(exc, gate.InvalidEvidence) else "release evidence collection failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
