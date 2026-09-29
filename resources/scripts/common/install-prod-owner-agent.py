#!/usr/bin/env python3
"""Install and control the PROD owner agent under the logged-in user's launchd."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "com.shiba.jenkins.prod-owner"


def configuration(java, root):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(java), "-jar", str(root / "agent.jar"),
                             "-jnlpUrl", (root / "jenkins-agent.jnlp").as_uri(),
                             "-workDir", str(root)],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "launchd.stdout.log"),
        "StandardErrorPath": str(root / "launchd.stderr.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Docker.app/Contents/Resources/bin"},
    }


def validate(java, root):
    for item in (java, root / "agent.jar", root / "jenkins-agent.jnlp"):
        if not item.is_file():
            raise ValueError(f"Missing existing agent dependency: {item}")
    if (root / "jenkins-agent.jnlp").stat().st_mode & 0o077:
        raise ValueError("Existing JNLP must not be group/world accessible")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", required=True, type=Path)
    parser.add_argument("--agent-root", required=True, type=Path)
    parser.add_argument("--action", choices=["render", "install", "status", "start", "stop"], default="render")
    parser.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    action = "install" if args.install else args.action
    java, root = args.java.resolve(), args.agent_root.resolve()
    try:
        validate(java, root)
    except ValueError as exc:
        parser.error(str(exc))
    payload = plistlib.dumps(configuration(java, root))
    if action == "render":
        print(payload.decode(), end="")
        return 0
    if os.getuid() == 0:
        parser.error("Manage as the existing logged-in agent owner, not root")
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0
    if action == "status":
        print(f"{service}: {'loaded' if loaded else 'stopped'}")
        return 0 if loaded else 3
    if action == "stop":
        if loaded:
            subprocess.run(["launchctl", "bootout", service], check=True)
        print(f"Stopped: {service}")
        return 0
    if target.exists() and target.read_bytes() != payload:
        parser.error(f"Existing configuration differs; review before replacing: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with target.open("xb") as output:
            output.write(payload)
        target.chmod(0o600)
    if action == "install" and not loaded:
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    elif action == "start":
        if not loaded:
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
        subprocess.run(["launchctl", "kickstart", "-k", service], check=True)
    print(f"Managed by launchd: {service}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
