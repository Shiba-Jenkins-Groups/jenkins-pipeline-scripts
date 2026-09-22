#!/usr/bin/env python3
"""Check bound release credentials without printing values, paths, or key digests."""
import os
from pathlib import Path
import sys


PASSWORD_CREDENTIALS = {
    "Jenkins read": ("JENKINS_API_USER", "JENKINS_API_TOKEN"),
    "Harbor": ("PREFLIGHT_HARBOR_USER", "PREFLIGHT_HARBOR_PASSWORD"),
    "SCM read": ("PREFLIGHT_SCM_USER", "PREFLIGHT_SCM_PASSWORD"),
    "promotion writer": ("PREFLIGHT_MERGE_USER", "PREFLIGHT_MERGE_PASSWORD"),
    "finalization writer": ("PREFLIGHT_WRITER_USER", "PREFLIGHT_WRITER_PASSWORD"),
    "Nexus": ("PREFLIGHT_NEXUS_USER", "PREFLIGHT_NEXUS_PASSWORD"),
}
KEY_CREDENTIALS = {
    "approval signing key": "APPROVAL_KEY_FILE",
    "receipt signing key": "RECEIPT_KEY_FILE",
}


def check(environ):
    errors = []
    for name, variables in PASSWORD_CREDENTIALS.items():
        if any(not environ.get(variable) for variable in variables):
            errors.append(name + ": empty username/password binding")
    for name, variable in KEY_CREDENTIALS.items():
        try:
            with Path(environ[variable]).open("rb") as stream:
                valid = len(stream.read(32)) == 32
        except (KeyError, OSError, ValueError):
            valid = False
        if not valid:
            errors.append(name + ": unreadable or shorter than 32 bytes")
    return errors


def main():
    errors = check(os.environ)
    if errors:
        print("BLOCKED: release credential preflight: " + "; ".join(errors), file=sys.stderr)
        return 1
    print("PASS: all release credentials are bound; signing keys meet the minimum length")
    return 0


if __name__ == "__main__":
    sys.exit(main())
