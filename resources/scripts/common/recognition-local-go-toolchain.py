#!/usr/bin/env python3
"""Install the checksum-pinned official Go SDK in this project's workspace only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

VERSION = '1.26.10'
# https://go.dev/dl/?mode=json&include=all, checked 2026-10-23.
ARCHIVES = {
    'darwin-arm64': ('0b5183492292ada170f9b561a834863808dd02edba599e4ad0e89899330020b3', 64669745),
    'linux-arm64': ('3f4227ce7b206a513e794f1393adb473a07b46e94d18bd15d1c684bbb7319797', 63827526),
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def secure_directory(path):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('canonical directory required')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private workspace tool directory required')


def host_platform():
    system = {'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system())
    machine = {'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine())
    name = f'{system}-{machine}'
    if name not in ARCHIVES:
        raise ValueError('unsupported Go SDK platform')
    return name


def fetch(url, destination, size):
    with urllib.request.urlopen(url, timeout=60) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname not in ('go.dev', 'dl.google.com'):
            raise ValueError('official HTTPS Go download required')
        with destination.open('xb') as output:
            received = 0
            while True:
                chunk = response.read(min(1024 * 1024, size + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > size:
                    raise ValueError('official Go archive size exceeded')
                output.write(chunk)


def check_version(root, name):
    environment = dict(os.environ, GOTOOLCHAIN='local', GOROOT=str(root))
    actual = subprocess.run([str(root / 'bin/go'), 'version'], env=environment,
                            check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    if actual != f'go version go{VERSION} {name.replace("-", "/")}':
        raise ValueError('installed Go version mismatch')


def check_cache(target, name, expected):
    secure_directory(target)
    receipt_file = target / 'receipt.json'
    if receipt_file.is_symlink() or not receipt_file.is_file():
        raise ValueError('SDK receipt required')
    receipt = json.loads(receipt_file.read_text())
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ValueError('SDK receipt mismatch')
    root = target / 'go'
    for relative in ('bin/go', 'pkg/tool/' + name.replace('-', '_') + '/compile'):
        program = root / relative
        if program.resolve(strict=True) != program or not program.is_file() or not os.access(program, os.X_OK):
            raise ValueError('SDK executable path mismatch')
        if digest(program) != receipt.get('executable_sha256', {}).get(relative):
            raise ValueError('SDK executable digest mismatch')
    check_version(root, name)
    return root


def ensure_toolchain(workspace, name=None, archive=None):
    workspace = Path(workspace)
    if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or any(c in str(workspace) for c in '\r\n\0'):
        raise ValueError('canonical workspace required')
    name = host_platform() if name is None else name
    if name not in ARCHIVES:
        raise ValueError('unsupported Go SDK platform')
    sha, size = ARCHIVES[name]
    filename = f'go{VERSION}.{name}.tar.gz'
    expected = {'version': VERSION, 'platform': name, 'archive_sha256': sha,
                'archive_size': size, 'source_url': 'https://go.dev/dl/' + filename}
    cache = workspace / '.ci-tools'
    cache.mkdir(mode=0o700, exist_ok=True)
    secure_directory(cache)
    target = cache / f'go{VERSION}-{name}'
    if target.exists() or target.is_symlink():
        return check_cache(target, name, expected)
    # Preserve a failed preparation for inspection. Never overwrite another SDK.
    staging = Path(tempfile.mkdtemp(prefix=f'.go{VERSION}-{name}-', dir=cache))
    package = staging / filename
    if archive is None:
        fetch(expected['source_url'], package, size)
    else:
        archive = Path(archive)
        if archive.resolve(strict=True) != archive or not archive.is_file():
            raise ValueError('regular archive required')
        shutil.copyfile(archive, package)
    if package.stat().st_size != size or digest(package) != sha:
        raise ValueError('official Go archive checksum or size mismatch')
    with tarfile.open(package, 'r:gz') as source:
        for member in source.getmembers():
            allowed = (member.name == 'go' and member.isdir()) or member.name.startswith('go/')
            if not allowed or '..' in Path(member.name).parts or not (member.isdir() or member.isfile()):
                raise ValueError('unsafe Go archive member')
        source.extractall(staging)
    root = staging / 'go'
    check_version(root, name)
    receipt = dict(expected, executable_sha256={relative: digest(root / relative) for relative in
        ('bin/go', 'pkg/tool/' + name.replace('-', '_') + '/compile')})
    receipt_path = staging / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + '\n')
    receipt_path.chmod(0o600)
    os.rename(staging, target)
    return check_cache(target, name, expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--archive', type=Path, help='Optional already-downloaded official archive; the same pinned checksum is mandatory')
    args = parser.parse_args()
    print(ensure_toolchain(args.workspace, archive=args.archive))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        print('project Go SDK preparation failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
