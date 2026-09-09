#!/usr/bin/env python3
"""Install the existing inbound agent under the logged-in macOS user's launchd."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", required=True, type=Path)
    parser.add_argument("--agent-root", required=True, type=Path)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    java, root = args.java.resolve(), args.agent_root.resolve()
    for item in (java, root / "agent.jar", root / "jenkins-agent.jnlp"):
        if not item.is_file():
            parser.error(f"Missing existing agent dependency: {item}")
    if (root / "jenkins-agent.jnlp").stat().st_mode & 0o077:
        parser.error("Existing JNLP must not be group/world accessible")
    payload = plistlib.dumps(configuration(java, root))
    if not args.install:
        print(payload.decode(), end="")
        return
    if os.getuid() == 0:
        parser.error("Install as the existing logged-in agent owner, not root")
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    if target.exists() and target.read_bytes() != payload:
        parser.error(f"Existing configuration differs; review before replacing: {target}")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"
    if subprocess.run(["launchctl", "print", service], capture_output=True).returncode == 0:
        print(f"Already loaded: {service}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with target.open("xb") as output:
            output.write(payload)
        target.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed: {service}")


if __name__ == "__main__":
    main()
